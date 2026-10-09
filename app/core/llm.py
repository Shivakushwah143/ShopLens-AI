from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.llm_api_key)

    async def json_completion(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        provider = self.settings.llm_provider.lower().strip()
        try:
            if provider == "gemini":
                text = await self._gemini_generate(
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    response_mime_type="application/json",
                )
                return _coerce_json(text) if text else None
            logger.warning("Unsupported LLM provider for JSON completion: %s", provider)
            return None
        except Exception as exc:  # pragma: no cover
            logger.warning("LLM JSON completion failed: %s", exc)
            return None

    async def text_completion(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
    ) -> str | None:
        if not self.enabled:
            return None
        provider = self.settings.llm_provider.lower().strip()
        try:
            if provider == "gemini":
                return await self._gemini_generate(
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    response_mime_type="text/plain",
                )
            logger.warning("Unsupported LLM provider for text completion: %s", provider)
            return None
        except Exception as exc:  # pragma: no cover
            logger.warning("LLM text completion failed: %s", exc)
            return None

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1.5, min=2, max=15),
        reraise=True,
    )
    async def _gemini_generate(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        response_mime_type: str,
    ) -> str | None:
        url = (
            f"{self.settings.gemini_api_base}/models/{model}:generateContent"
            f"?key={self.settings.llm_api_key}"
        )

        # Use higher token limit for the final answer model to avoid truncation
        max_output_tokens = 16384 if "pro" in model else 8192

        payload = {
            "system_instruction": {
                "parts": [
                    {
                        "text": system_prompt,
                    }
                ]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": user_prompt,
                        }
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0.3,
                "responseMimeType": response_mime_type,
                "maxOutputTokens": max_output_tokens,
            },
        }

        # Use longer timeout for larger models that need time to compose
        timeout = 120.0 if "pro" in model else 60.0

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
        data = response.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return None
        parts = candidates[0].get("content", {}).get("parts", [])
        texts = [part.get("text", "") for part in parts if part.get("text")]
        combined = "\n".join(texts).strip()
        return combined or None


def _coerce_json(payload: str) -> dict[str, Any]:
    text = payload.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    return json.loads(text)


llm_client = LLMClient()
