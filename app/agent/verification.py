from __future__ import annotations

from collections import Counter

from app.models.schemas import ConfidenceScore, EvidenceItem, SourceItem


class Verifier:
    def verify(self, sources: list[SourceItem], evidence: list[EvidenceItem]) -> ConfidenceScore:
        excerpts = [item.excerpt for item in evidence if item.excerpt]
        unique_domains = {source.domain for source in sources if source.domain}
        token_counter = Counter()
        for excerpt in excerpts:
            token_counter.update(word.lower() for word in excerpt.split()[:60])
        top_tokens = [token for token, count in token_counter.items() if count >= 3 and len(token) > 5]
        conflicts: list[str] = []
        overall = (
            0.20
            + (0.09 * min(len(sources), 6))
            + (0.05 * min(len(evidence), 10))
            + (0.08 * min(len(unique_domains), 5))
        )
        overall = min(overall, 0.95)
        if len(sources) < 3:
            conflicts.append("Fewer than three sources were collected.")
            overall = min(overall, 0.72)
        if len(unique_domains) < 2:
            conflicts.append("Most evidence came from a single domain.")
            overall = min(overall, 0.65)
        if len(evidence) < 3:
            conflicts.append("Evidence is thin, so confidence is reduced.")
            overall = min(overall, 0.60)
        rationale = (
            f"Confidence based on {len(sources)} ranked sources, {len(evidence)} evidence items, "
            f"{len(unique_domains)} unique domains, and recurring terms such as "
            f"{', '.join(top_tokens[:5]) or 'limited overlap'}."
        )
        return ConfidenceScore(
            overall=overall,
            evidence_count=len(evidence),
            source_count=len(sources),
            conflicts=conflicts,
            rationale=rationale,
        )
