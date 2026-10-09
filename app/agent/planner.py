from __future__ import annotations

import re
from typing import Any

from app.core.config import get_settings
from app.core.llm import llm_client
from app.models.schemas import QueryIntent, ResearchPlan, UserQuery


class ResearchPlanner:
    async def build_plan(self, query: UserQuery, intent: QueryIntent) -> ResearchPlan:
        settings = get_settings()
        llm_result = await llm_client.json_completion(
            model=settings.llm_model_planner,
            system_prompt=(
                "You are an expert research strategist. Create a precise web research plan.\n\n"
                "QUERY GENERATION RULES (CRITICAL):\n"
                "1. Generate exactly 4-6 search queries. Each must be a real search-engine query "
                "(concise, keyword-rich, NO filler words like 'explain' or 'describe').\n"
                "2. Every query MUST directly help answer the user's specific question. "
                "Do NOT generate tangential or generic queries.\n"
                "3. DIVERSIFY angles: include queries for authoritative overviews, technical details, "
                "recent developments, comparisons, and practical applications.\n"
                "4. PREFER query structures that lead to accessible content: blog posts, documentation, "
                "Wikipedia, educational sites, .gov, .edu. AVOID queries likely to hit paywalled or "
                "login-required pages.\n"
                "5. Include the SPECIFIC subject in every query (don't assume context carries over).\n"
                "6. For technical topics, add one query targeting official docs or research papers.\n"
                "7. Generate 2-3 sub-questions that decompose the user's question into answerable parts.\n\n"
                "Return JSON: {objective, search_queries (4-6), video_queries (0-2), "
                "subquestions (2-3), source_limit (8-12), stopping_criteria (array)}."
            ),
            user_prompt=(
                f"User query: {query.text}\n"
                f"Core topic: {intent.topic}\n"
                f"Research mode: {intent.mode}\n"
                f"Subtopics: {', '.join(intent.subtopics) if intent.subtopics else 'none'}\n"
                f"Needs YouTube: {intent.requires_youtube}\n\n"
                "Generate a focused research plan. Each search query must be specific enough that "
                "the top search results would directly help answer the user's question."
            ),
        )
        if llm_result:
            normalized_payload = self._normalize_plan_payload(llm_result)
            normalized_payload["search_queries"] = self._normalize_search_queries(
                normalized_payload.get("search_queries", []),
                query.text,
                intent.topic,
                intent.subtopics,
            )
            return ResearchPlan.model_validate(normalized_payload)

        search_queries = [query.text]
        search_queries.extend(intent.subtopics[:2])
        video_queries = [f"{intent.topic} explained"] if intent.requires_youtube else []
        return ResearchPlan(
            objective=f"Answer the user's research request about {intent.topic}",
            search_queries=self._normalize_search_queries(search_queries, query.text, intent.topic, intent.subtopics),
            video_queries=video_queries,
            subquestions=intent.subtopics[:4],
            source_limit=settings.max_web_sources,
            stopping_criteria=[
                "At least three relevant independent sources reviewed",
                "Key facts supported by more than one source",
                "Low-quality or duplicate pages excluded",
            ],
        )

    def _normalize_plan_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        normalized["search_queries"] = self._ensure_list(normalized.get("search_queries"))
        normalized["video_queries"] = self._ensure_list(normalized.get("video_queries"))
        normalized["subquestions"] = self._ensure_list(normalized.get("subquestions"))
        normalized["stopping_criteria"] = self._ensure_list(normalized.get("stopping_criteria"))
        normalized["source_limit"] = self._normalize_source_limit(normalized.get("source_limit"))

        if not normalized["search_queries"]:
            normalized["search_queries"] = [normalized.get("objective") or "general research"]
        if not normalized["source_limit"]:
            normalized["source_limit"] = get_settings().max_web_sources
        return normalized

    def _ensure_list(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            separators = ["\n", ";", "|"]
            for separator in separators:
                if separator in value:
                    return [part.strip(" -") for part in value.split(separator) if part.strip(" -")]
            return [value.strip()] if value.strip() else []
        return [str(value).strip()] if str(value).strip() else []

    def _normalize_source_limit(self, value: Any) -> int:
        default_limit = get_settings().max_web_sources
        try:
            limit = int(value)
        except (TypeError, ValueError):
            return default_limit
        return max(6, min(limit, 15))

    def _normalize_search_queries(
        self,
        search_queries: list[str],
        query_text: str,
        intent_topic: str,
        intent_subtopics: list[str],
    ) -> list[str]:
        seeds = [*search_queries, query_text, intent_topic, *intent_subtopics[:3]]
        cleaned: list[str] = []
        seen: set[str] = set()
        for seed in seeds:
            compact = self._compact_search_query(seed)
            if not compact:
                continue
            key = compact.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(compact)
            if len(cleaned) >= 8:
                break

        if not cleaned:
            return [query_text]

        # Score and rank queries by estimated quality, discard weak ones
        scored = [(q, self._score_query(q, query_text, [c for c in cleaned if c != q])) for q in cleaned]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        # Keep queries scoring above 0.2, but always keep at least 3
        strong = [q for q, s in scored if s >= 0.20]
        if len(strong) < 3:
            strong = [q for q, _ in scored[:max(3, len(scored))]]
        return strong[:8]

    def _compact_search_query(self, text: str) -> str:
        """Keep the search engine query short but intent-rich."""
        # Lowercase and remove punctuation
        clean = re.sub(r"[^\w\s]", "", text.lower())
        words = clean.split()
        
        # Stopwords to remove (filler only)
        stopwords = {
            "a", "an", "the", "of", "in", "on", "at", "by", "for", "with", "about",
            "is", "are", "was", "were", "be", "been", "being", "it", "that", "this",
            "to", "from", "and", "or", "but", "if", "then", "else", "when", "where",
            "how", "why", "who", "which", "whose", "whom", "can", "could", "would",
            "should", "might", "must", "may", "please", "search", "find", "get", "show"
        }
        
        # Keywords to ALWAYS keep (high intent)
        keep_words = {
            "compare", "comparison", "difference", "versus", "vs", "statistics", 
            "data", "metrics", "chart", "graph", "timeline", "history", "evolution",
            "future", "tutorial", "guide", "documentation", "official", "review"
        }
        
        filtered = [w for w in words if w not in stopwords or w in keep_words]
        
        # Limit to 10 words for search engine efficiency
        return " ".join(filtered[:10])

    def _score_query(self, query: str, user_query: str, existing_queries: list[str]) -> float:
        """Heuristic scoring (0-1) for query quality."""
        score = 0.5
        
        # Specificity: Query should be 4-8 words for best results
        word_count = len(query.split())
        if 4 <= word_count <= 8:
            score += 0.20
        elif word_count < 3:
            score -= 0.30
            
        # Intent words bonus
        intent_words = {"statistics", "data", "comparison", "versus", "vs", "official", "docs"}
        if any(w in query.lower() for w in intent_words):
            score += 0.15
            
        # Diversity check: penalize if too similar to existing queries
        for existing in existing_queries:
            overlap = set(query.lower().split()) & set(existing.lower().split())
            if len(overlap) >= 3:
                score -= 0.20
                
        # Accessibility bonus: queries targeting reference sites
        ref_hints = {"wikipedia", "github", "documentation", "guide", "tutorial"}
        if any(h in query.lower() for h in ref_hints):
            score += 0.10
            
        return max(0.0, min(1.0, score))
