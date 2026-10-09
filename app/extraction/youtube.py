from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from youtube_transcript_api import YouTubeTranscriptApi

from app.models.schemas import VideoItem
from app.utils.text import keyword_overlap_score


class YouTubeExtractor:
    def enrich(self, *, task_id: str, title: str, url: str, description: str | None = None) -> VideoItem:
        video_id = _extract_video_id(url)
        transcript_excerpt = None
        reasons = []
        if video_id:
            try:
                transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=["en"])
                transcript_excerpt = " ".join(chunk["text"] for chunk in transcript[:30])[:2000]
                reasons.append("Transcript available")
            except Exception:
                reasons.append("Transcript unavailable")

        return VideoItem(
            task_id=task_id,
            title=title,
            url=url,
            description=description,
            transcript_excerpt=transcript_excerpt,
            reasons=reasons,
        )

    def score(self, query: str, item: VideoItem) -> VideoItem:
        text = " ".join(filter(None, [item.title, item.description, item.transcript_excerpt]))
        item.rank_score = (
            (0.6 * keyword_overlap_score(query, text))
            + (0.25 if item.transcript_excerpt else 0.0)
            + (0.15 if "official" in (item.channel or "").lower() else 0.0)
        )
        if item.transcript_excerpt:
            item.reasons.append("Transcript improves evidence quality")
        return item

    def deduplicate(self, videos: list[VideoItem]) -> list[VideoItem]:
        """Remove duplicate videos based on their video ID."""
        seen_ids: set[str] = set()
        seen_titles: set[str] = set()
        unique: list[VideoItem] = []
        for video in videos:
            video_id = _extract_video_id(str(video.url))
            title_key = video.title.strip().lower()

            # Skip if we've seen this exact video ID or very similar title
            if video_id and video_id in seen_ids:
                continue
            if title_key in seen_titles:
                continue

            if video_id:
                seen_ids.add(video_id)
            seen_titles.add(title_key)
            unique.append(video)
        return unique


def _extract_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.netloc.endswith("youtu.be"):
        return parsed.path.strip("/") or None
    if "youtube.com" in parsed.netloc:
        return parse_qs(parsed.query).get("v", [None])[0]
    match = re.search(r"v=([\w-]{6,})", url)
    return match.group(1) if match else None
