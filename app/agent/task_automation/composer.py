from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .extractor import NormalizedItem


@dataclass
class ComposedOutput:
    summary: str
    results: list[dict[str, Any]]
    reasoning: str


class ResultComposer:
    """Turn normalized results into a concise, action-oriented answer."""

    def compose(self, *, user_query: str, items: list[NormalizedItem], top_k: int = 5) -> ComposedOutput:
        items = self._post_filter(items)
        ranked = self._rank(items)[:top_k]

        summary = f"Found {len(items)} candidates. Top {len(ranked)} are listed below."
        results = [
            {
                "name": it.name,
                "price": it.price,
                "rating": it.rating,
                "link": it.link,
                "source_domain": it.source_domain,
                "snippet": (it.raw or {}).get("snippet") or (it.raw or {}).get("text", "")[:180],
            }
            for it in ranked
        ]

        reasoning = (
            "Ranked primarily by availability of a clear price, then by higher rating (if present), "
            "and deduplicated by title/link."
        )

        return ComposedOutput(summary=summary, results=results, reasoning=reasoning)

    def _post_filter(self, items: list[NormalizedItem]) -> list[NormalizedItem]:
        # Deduplicate (name + price + link)
        seen: set[tuple[str, str, str]] = set()
        out: list[NormalizedItem] = []
        for it in items:
            name = (it.name or "").strip()
            if not name:
                continue
            key = (name.lower(), "" if it.price is None else str(int(it.price)), (it.link or "").lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(it)

        # Prefer items with at least price OR rating when possible
        structured = [it for it in out if it.price is not None or it.rating is not None]
        if len(structured) >= 3:
            return structured
        return out

    def _rank(self, items: list[NormalizedItem]) -> list[NormalizedItem]:
        def score(it: NormalizedItem) -> float:
            s = 0.0
            if it.price is not None:
                s += 2.0
            if it.rating is not None:
                s += min(it.rating / 5.0, 1.0)
            if it.link:
                s += 0.15
            return s

        return sorted(items, key=score, reverse=True)

