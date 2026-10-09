from __future__ import annotations

import re

from bs4 import BeautifulSoup
import trafilatura

from app.models.schemas import EvidenceItem, EvidenceType
from app.utils.text import keyword_overlap_score


class WebEvidenceExtractor:
    def extract(self, *, task_id: str, query: str, url: str, html: str, text: str) -> list[EvidenceItem]:
        extracted = trafilatura.extract(html, include_comments=False, include_tables=True) or text
        extracted = extracted.strip()
        soup = BeautifulSoup(html, "html.parser")

        evidence: list[EvidenceItem] = []

        # Extract main text content — keep much more content for the LLM to work with
        if extracted:
            # Calculate a richer relevance score
            overlap = keyword_overlap_score(query, extracted[:3000])
            # Boost score for longer, more substantive content
            length_bonus = min(len(extracted) / 5000, 0.15)
            confidence = min(0.45 + (0.45 * overlap) + length_bonus, 0.98)

            evidence.append(
                EvidenceItem(
                    task_id=task_id,
                    source_url=url,
                    evidence_type=EvidenceType.text,
                    content=extracted[:20000],
                    excerpt=extracted[:800],
                    confidence=confidence,
                    metadata={"length": len(extracted)},
                )
            )

        # Extract tables as structured data
        for table in soup.find_all("table")[:3]:
            table_html = str(table)
            table_text = " ".join(table.stripped_strings)
            if table_text and len(table_text) > 30:
                evidence.append(
                    EvidenceItem(
                        task_id=task_id,
                        source_url=url,
                        evidence_type=EvidenceType.table,
                        content=table_html[:6000],
                        excerpt=table_text[:500],
                        confidence=0.6,
                        metadata={"raw_text": table_text[:2000]},
                    )
                )

        # Extract images with meaningful alt text or captions
        for img in soup.find_all("img")[:5]:
            src = img.get("src") or ""
            alt = img.get("alt") or ""
            if not src or not alt or len(alt) < 10:
                continue
            # Skip tiny icons, tracking pixels, and decorative images
            width = img.get("width")
            height = img.get("height")
            if width and height:
                try:
                    if int(width) < 100 or int(height) < 100:
                        continue
                except (ValueError, TypeError):
                    pass
            if any(skip in src.lower() for skip in ["icon", "logo", "pixel", "tracking", "avatar", "badge"]):
                continue
            # Make relative URLs absolute
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                from urllib.parse import urlparse
                parsed = urlparse(url)
                src = f"{parsed.scheme}://{parsed.netloc}{src}"

            evidence.append(
                EvidenceItem(
                    task_id=task_id,
                    source_url=url,
                    evidence_type=EvidenceType.image,
                    content=src,
                    excerpt=alt,
                    confidence=0.5,
                    metadata={"alt": alt, "src": src},
                )
            )

        return evidence
