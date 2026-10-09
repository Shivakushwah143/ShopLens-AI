from __future__ import annotations

import re
from typing import Any

from app.core.config import get_settings
from app.core.llm import llm_client
from app.models.schemas import QueryIntent, ResearchMode, UserQuery
from app.utils.text import normalize_query


class QueryIntake:
    async def analyze(self, raw_query: str) -> tuple[UserQuery, QueryIntent]:
        query = UserQuery(text=normalize_query(raw_query))
        settings = get_settings()

        llm_result = await llm_client.json_completion(
            model=settings.llm_model_worker,
            system_prompt=(
                "Classify research requests. Return JSON with keys: mode, topic, subtopics, "
                "requires_youtube, answer_format."
            ),
            user_prompt=query.text,
        )
        if llm_result:
            return query, QueryIntent.model_validate(self._normalize_intent_payload(llm_result, query.text))

        lowered = query.text.lower()
        youtube_words = {"video", "youtube", "watch", "tutorial", "explained", "lecture"}
        requires_youtube = any(word in lowered for word in youtube_words)
        topic = re.sub(r"^(what|how|why|who|when|where)\s+", "", lowered, flags=re.IGNORECASE).strip()
        mode = ResearchMode.mixed if requires_youtube else ResearchMode.web
        subtopics = [part.strip() for part in re.split(r"[?.]| and ", query.text) if len(part.strip()) > 12][:4]
        intent = QueryIntent(
            mode=mode,
            topic=topic or query.text,
            subtopics=subtopics,
            requires_youtube=requires_youtube,
            answer_format="direct_answer",
        )
        return query, intent

    def _normalize_intent_payload(self, payload: dict[str, Any], raw_query: str) -> dict[str, Any]:
        normalized = dict(payload)
        normalized["mode"] = self._normalize_mode(normalized.get("mode"), raw_query)
        normalized["topic"] = self._normalize_topic(normalized.get("topic"), raw_query)
        normalized["subtopics"] = self._ensure_list(normalized.get("subtopics"))
        normalized["requires_youtube"] = self._normalize_bool(
            normalized.get("requires_youtube"),
            raw_query,
        )
        normalized["answer_format"] = self._normalize_answer_format(normalized.get("answer_format"))
        return normalized

    def _normalize_mode(self, value: Any, raw_query: str) -> str:
        lowered = str(value or "").strip().lower()
        if lowered in {mode.value for mode in ResearchMode}:
            return lowered

        if lowered in {"comparison", "compare", "analysis", "research", "general", "text"}:
            return ResearchMode.web.value
        if lowered in {"youtube", "video", "videos", "watch", "tutorial"}:
            return ResearchMode.video.value
        if lowered in {"mixed research", "web+video", "web_and_video", "hybrid", "both"}:
            return ResearchMode.mixed.value

        query_lower = raw_query.lower()
        if any(word in query_lower for word in {"youtube", "video", "tutorial", "explainer"}):
            return ResearchMode.mixed.value
        return ResearchMode.web.value

    def _normalize_topic(self, value: Any, raw_query: str) -> str:
        topic = str(value or "").strip()
        return topic if topic else raw_query

    def _normalize_bool(self, value: Any, raw_query: str) -> bool:
        if isinstance(value, bool):
            return value
        lowered = str(value or "").strip().lower()
        if lowered in {"true", "yes", "1", "required"}:
            return True
        if lowered in {"false", "no", "0", "not_required"}:
            return False
        query_lower = raw_query.lower()
        return any(word in query_lower for word in {"youtube", "video", "tutorial", "explainer"})

    def _normalize_answer_format(self, value: Any) -> str:
        answer_format = str(value or "").strip()
        return answer_format if answer_format else "direct_answer"

    def _ensure_list(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            separators = ["\n", ";", "|", ","]
            for separator in separators:
                if separator in value:
                    return [part.strip(" -") for part in value.split(separator) if part.strip(" -")]
            return [value.strip()] if value.strip() else []
        return [str(value).strip()] if str(value).strip() else []
