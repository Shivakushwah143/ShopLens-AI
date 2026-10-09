from __future__ import annotations

import re
from typing import Any

from app.core.config import get_settings
from app.core.llm import llm_client
from app.models.schemas import ConfidenceScore, EvidenceItem, FinalAnswer, ImageItem, SourceItem, VideoItem
from app.utils.text import keyword_overlap_score


class AnswerComposer:
    async def compose(
        self,
        *,
        query: str,
        sources: list[SourceItem],
        evidence: list[EvidenceItem],
        confidence: ConfidenceScore,
        videos: list[VideoItem] | None = None,
        images: list[ImageItem] | None = None,
    ) -> FinalAnswer:
        videos = videos or []
        images = images or []
        settings = get_settings()

        # Prioritize text evidence by confidence, then tables, then images
        text_evidence = sorted(
            [e for e in evidence if e.evidence_type.value == "text"],
            key=lambda item: -(item.confidence or 0.0),
        )
        table_evidence = [e for e in evidence if e.evidence_type.value == "table"]
        image_evidence = [e for e in evidence if e.evidence_type.value == "image"]

        # Deduplicate evidence: keep best per source URL
        seen_urls: set[str] = set()
        deduped_text: list[EvidenceItem] = []
        for item in text_evidence:
            if item.source_url not in seen_urls:
                seen_urls.add(item.source_url)
                deduped_text.append(item)

        # Filter low-quality evidence (very short or low confidence)
        quality_text = [e for e in deduped_text
                        if (e.confidence or 0) >= 0.30 and len(e.content or "") >= 200]
        if len(quality_text) < 3:
            quality_text = deduped_text

        # ── RELEVANCE GATE ──────────────────────────────────────────────
        # Check if collected evidence actually matches the user's query.
        # If most evidence is off-topic, we tell the LLM to rely on its
        # own knowledge rather than synthesize garbage.
        relevant_count = 0
        for item in quality_text:
            snippet = (item.content or "")[:2000]
            if keyword_overlap_score(query, snippet) >= 0.15:
                relevant_count += 1

        evidence_is_relevant = (
            relevant_count >= 2
            or (quality_text and relevant_count / max(len(quality_text), 1) >= 0.30)
        )

        prioritized_text = quality_text[:12]
        prioritized_tables = table_evidence[:4]
        prioritized_images = image_evidence[:6]

        source_lines = []
        for i, source in enumerate(sources[:10]):
            source_lines.append(
                f"[{i+1}] {source.title} ({source.url})\n"
                f"    Domain: {source.domain} | Relevance: {source.relevance_score:.2f} | "
                f"Authority: {source.authority_score:.2f}"
            )

        evidence_lines = []
        for item in prioritized_text:
            evidence_lines.append(
                f"--- Source: {item.source_url} (confidence: {item.confidence:.2f}) ---\n"
                f"{item.content[:12000]}"
            )

        # Build the relevance instruction
        if evidence_is_relevant:
            relevance_instruction = (
                "The evidence below is relevant to the query. Synthesize it thoroughly. "
                "Combine information from multiple sources. Do NOT just list source contents — "
                "analyze, compare, and explain."
            )
        else:
            relevance_instruction = (
                "WARNING: Most of the collected evidence appears IRRELEVANT to the query. "
                "DO NOT synthesize irrelevant content. Instead, use your own knowledge about "
                f"the topic '{query}' to write a comprehensive, accurate research report. "
                "If any evidence items ARE relevant, incorporate those. Ignore the rest completely."
            )

        llm_result = await llm_client.json_completion(
            model=settings.llm_model_final,
            system_prompt=(
                "You are a world-class research analyst. Your goal is to produce a comprehensive, "
                "structured research report that DIRECTLY answers the user's query.\n\n"
                "CRITICAL: The report MUST be about the EXACT topic the user asked about. "
                "If the evidence does not match the query, use your own knowledge instead.\n\n"
                "MANDATORY JSON STRUCTURE:\n"
                "{\n"
                '  "summary": "Direct answer to the user query (2-3 sentences).",\n'
                '  "analysis": "Deep dive analysis using HTML formatting (<h3>, <h4>, <p>, '
                '<ul>, <li>, <table>, <blockquote>). Explain the how and why. '
                'Cite sources with [1], [2] where applicable. At least 800 words.",\n'
                '  "supporting_points": ["Point 1 with citation [1]", "Point 2 with citation [2]"],\n'
                '  "balanced_view": "Alternative perspectives, counterarguments, or limitations.",\n'
                '  "conclusion": "Final takeaways as a summary paragraph with actionable insights."\n'
                "}\n\n"
                "QUALITY RULES:\n"
                "- DO NOT copy-paste raw text. Rewrite everything in your analytical voice.\n"
                "- Every paragraph must add value. No filler.\n"
                "- Use HTML tables for comparisons. Use <blockquote> for key quotes.\n"
                "- The report should read like an expert wrote it.\n"
                "- Write at least 1500 words of substantive content across all fields."
            ),
            user_prompt=(
                f"RESEARCH QUERY: {query}\n\n"
                f"{'='*60}\n"
                f"{relevance_instruction}\n\n"
                f"{'='*60}\n"
                f"RANKED SOURCES ({len(sources)} total):\n"
                f"{chr(10).join(source_lines)}\n\n"
                f"{'='*60}\n"
                f"EXTRACTED EVIDENCE ({len(prioritized_text)} text items):\n"
                f"{chr(10).join(evidence_lines)}\n\n"
                f"{'='*60}\n"
                f"CONFIDENCE: {confidence.overall:.2f} — {confidence.rationale}\n\n"
                "Now produce the JSON report. Stay on topic. Be thorough and analytical."
            ),
        )

        direct_answer, supporting_points = self._parse_json_answer(llm_result, prioritized_text)
        citations = [str(source.url) for source in sources[:10]]

        # ── Collect Images ────────────────────────────────────────────────
        # Build ImageItem objects from image evidence AND dedicated search results
        image_items: list[ImageItem] = list(images) # Start with dedicated search results
        seen_image_srcs: set[str] = {img.src for img in image_items}
        
        # Build a quick lookup from source_url -> source title
        source_title_map = {str(s.url): s.title for s in sources}
        for img_ev in prioritized_images:
            img_src = img_ev.content  # src URL stored in content field
            if not img_src or img_src in seen_image_srcs:
                continue
            if not img_src.startswith("http"):
                continue
            seen_image_srcs.add(img_src)
            alt_text = img_ev.excerpt or img_ev.metadata.get("alt", "")
            rel_score = keyword_overlap_score(query, alt_text)
            image_items.append(ImageItem(
                task_id=sources[0].task_id if sources else "",
                src=img_src,
                alt=alt_text,
                source_url=img_ev.source_url,
                source_title=source_title_map.get(img_ev.source_url, ""),
                relevance_score=rel_score,
            ))
        
        # Re-score all images if they don't have a score
        for img in image_items:
            if img.relevance_score <= 0:
                img.relevance_score = keyword_overlap_score(query, img.alt)

        # Sort by relevance and keep top 8
        image_items.sort(key=lambda x: -x.relevance_score)
        image_items = image_items[:8]

        return FinalAnswer(
            direct_answer=direct_answer,
            supporting_points=supporting_points,
            citations=citations,
            videos=videos,
            images=image_items,
            confidence=confidence,
        )

    def _parse_json_answer(
        self,
        payload: dict[str, Any] | None,
        evidence: list[EvidenceItem],
    ) -> tuple[str, list[str]]:
        if not payload:
            return self._summarize_from_evidence(evidence) or ("No evidence found.", [])

        # Build a single HTML report string from the JSON fields for the UI
        summary = payload.get("summary", "")
        analysis = payload.get("analysis", "")
        perspective = payload.get("balanced_view", "")
        conclusion = payload.get("conclusion", "")

        report_html = f"""
            <div class="research-summary">
                <h3>Executive Summary</h3>
                <p>{summary}</p>
            </div>
            <div class="research-analysis">
                {analysis}
            </div>
            <div class="research-perspective">
                <h3>Balanced Perspective</h3>
                <p>{perspective}</p>
            </div>
            <div class="research-conclusion">
                <h3>Conclusion</h3>
                <p>{conclusion}</p>
            </div>
        """
        points = payload.get("supporting_points", [])
        return self._clean_answer_html(report_html), points

    def _clean_answer_html(self, value: str) -> str:
        """Clean the answer while preserving HTML formatting."""
        # Clean up excessive whitespace while preserving HTML structure
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    def _summarize_from_evidence(self, evidence: list[EvidenceItem]) -> tuple[str, list[str]] | None:
        text_items = [item for item in evidence if item.evidence_type.value == "text" and (item.content or item.excerpt)]
        if not text_items:
            return None
        corpus = " ".join((item.content or item.excerpt or "")[:2000] for item in text_items[:5])
        sentences = [chunk.strip() for chunk in re.split(r"(?<=[.!?])\s+", corpus) if chunk.strip()]
        sentences = [sentence for sentence in sentences if 25 <= len(sentence) <= 300]
        if not sentences:
            return None
        direct = sentences[0]
        points: list[str] = []
        for sentence in sentences[1:]:
            if sentence not in points:
                points.append(sentence)
            if len(points) >= 6:
                break
        if not points and len(sentences) > 1:
            points = sentences[1:4]
        return direct, points
