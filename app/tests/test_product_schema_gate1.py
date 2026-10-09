"""Gate 1 deterministic checks for the product schema types.

These checks import no test framework so they run under the existing working
interpreter even when pytest is unavailable:

    python -m app.tests.test_product_schema_gate1
"""

from __future__ import annotations

import json
from datetime import datetime

from app.agent.deals_tracker.product_schema import (
    AttributeEvidence,
    MatchAssessment,
    MatchStatus,
    ProductCandidate,
    ProductCategory,
    ProductRole,
    RequirementResult,
    RequirementStatus,
    SearchRequirement,
    candidate_from_scraped,
    category_from_text,
    is_primary_category,
    role_for_category,
    to_scraped_product,
)
from app.agent.deals_tracker.scraper import ScrapedProduct


def _scraped(name="Men Cotton T-Shirt Regular Fit", price=499.0, attrs=None):
    return ScrapedProduct(
        name=name,
        price=price,
        rating=4.3,
        link="https://www.amazon.in/item/dp/B0TEE",
        source="amazon.in",
        image_url="https://www.amazon.in/img/tee.jpg",
        attributes=attrs,
    )


def _requirement_result(attribute, status, required=True, value="x"):
    return RequirementResult(
        requirement=SearchRequirement(attribute=attribute, value=value, required=required),
        status=status,
    )


def test_search_requirement_defaults_and_name_alias():
    req = SearchRequirement(attribute="fabric", value="100% cotton")
    assert req.required is True
    assert req.name == "fabric"
    assert SearchRequirement.preferred("fit", "oversized").required is False
    assert SearchRequirement.mandatory("budget", "10000").required is True


def test_attribute_evidence_captures_timestamp_and_serializes():
    evidence = AttributeEvidence(
        attribute="fabric",
        value="100% cotton",
        source_url="https://www.amazon.in/item/dp/B0TEE",
        source_field="Material Composition",
        excerpt="100% Cotton",
    )
    assert isinstance(evidence.retrieved_at, datetime)
    payload = evidence.to_dict()
    assert payload["attribute"] == "fabric"
    assert payload["source_field"] == "Material Composition"
    assert isinstance(payload["retrieved_at"], str)


def test_candidate_creation_defaults_are_isolated():
    first = ProductCandidate(name="A", price=1.0, rating=None, link="l", source="s")
    second = ProductCandidate(name="B", price=2.0, rating=None, link="l2", source="s")
    assert first.currency == "INR"
    assert first.category is ProductCategory.UNKNOWN
    assert first.role is ProductRole.UNKNOWN
    first.attributes["fit"] = "slim"
    first.evidence.append(
        AttributeEvidence("fit", "slim", "u", "f")
    )
    assert second.attributes == {}
    assert second.evidence == []


def test_candidate_from_scraped_preserves_existing_fields():
    source = _scraped(attrs={"fit": "regular"})
    candidate = candidate_from_scraped(source)
    assert candidate.name == source.name
    assert candidate.price == source.price
    assert candidate.rating == source.rating
    assert candidate.link == source.link
    assert candidate.source == source.source
    assert candidate.image_url == source.image_url
    assert candidate.attributes == {"fit": "regular"}
    assert candidate.category is ProductCategory.T_SHIRT
    assert candidate.is_primary is True


def test_candidate_from_scraped_handles_missing_attributes():
    candidate = candidate_from_scraped(_scraped(attrs=None))
    assert candidate.attributes == {}


def test_candidate_from_scraped_respects_explicit_category():
    candidate = candidate_from_scraped(_scraped(), category=ProductCategory.ACCESSORY)
    assert candidate.category is ProductCategory.ACCESSORY
    assert candidate.role is ProductRole.ACCESSORY


def test_to_scraped_product_roundtrip_preserves_contract():
    source = _scraped(attrs={"fabric": "cotton"})
    restored = to_scraped_product(candidate_from_scraped(source))
    assert restored == source


def test_to_scraped_product_uses_none_for_empty_attributes():
    candidate = ProductCandidate(name="A", price=1.0, rating=None, link="l", source="s")
    assert to_scraped_product(candidate).attributes is None


def test_category_and_role_distinctions():
    assert category_from_text("Samsung Galaxy M14 5G Smartphone") is ProductCategory.SMARTPHONE
    assert category_from_text("Lenovo LOQ RTX 3050 Laptop") is ProductCategory.LAPTOP
    assert category_from_text("Cute smart phone charm accessory") is ProductCategory.ACCESSORY
    assert category_from_text("Smart phone case cover") is ProductCategory.ACCESSORY
    assert category_from_text("some made up listing text") is ProductCategory.UNKNOWN
    assert role_for_category(ProductCategory.SMARTPHONE) is ProductRole.PRIMARY
    assert role_for_category(ProductCategory.ACCESSORY) is ProductRole.ACCESSORY
    assert role_for_category(ProductCategory.UNKNOWN) is ProductRole.UNKNOWN
    assert role_for_category("not-a-category") is ProductRole.UNKNOWN
    assert is_primary_category(ProductCategory.LAPTOP) is True
    assert is_primary_category(ProductCategory.ACCESSORY) is False
    assert is_primary_category(ProductCategory.UNKNOWN) is False


def test_assessment_verified_when_required_confirmed():
    results = [
        _requirement_result("category", RequirementStatus.CONFIRMED),
        _requirement_result("budget", RequirementStatus.CONFIRMED),
        _requirement_result("color", RequirementStatus.UNKNOWN, required=False),
    ]
    assessment = MatchAssessment.from_results(results)
    assert assessment.status is MatchStatus.VERIFIED
    assert assessment.eligible is True
    assert assessment.missing_required == []
    assert assessment.contradictions == []


def test_assessment_possible_when_required_unknown():
    results = [
        _requirement_result("category", RequirementStatus.CONFIRMED),
        _requirement_result("fit", RequirementStatus.UNKNOWN),
    ]
    assessment = MatchAssessment.from_results(results)
    assert assessment.status is MatchStatus.POSSIBLE
    assert assessment.eligible is False
    assert assessment.is_possible is True
    assert assessment.missing_required == ["fit"]
    assert "fit" in assessment.explanation


def test_assessment_rejected_on_contradiction():
    results = [
        _requirement_result("fabric", RequirementStatus.CONTRADICTED),
        _requirement_result("fit", RequirementStatus.UNKNOWN),
    ]
    assessment = MatchAssessment.from_results(results)
    assert assessment.status is MatchStatus.REJECTED
    assert assessment.is_rejected is True
    assert assessment.contradictions == ["fabric"]
    assert assessment.eligible is False


def test_assessment_verified_when_no_required_requirements():
    assessment = MatchAssessment.from_results(
        [_requirement_result("color", RequirementStatus.UNKNOWN, required=False)]
    )
    assert assessment.status is MatchStatus.VERIFIED


def test_serialization_is_json_safe():
    evidence = AttributeEvidence("fabric", "100% cotton", "https://x/p", "Material")
    candidate = candidate_from_scraped(_scraped(), evidence=[evidence])
    assessment = MatchAssessment.from_results(
        [_requirement_result("fabric", RequirementStatus.CONFIRMED)],
        explanation="ok",
    )
    payload = {
        "candidate": candidate.to_dict(),
        "assessment": assessment.to_dict(),
    }
    encoded = json.dumps(payload)
    decoded = json.loads(encoded)
    assert decoded["candidate"]["currency"] == "INR"
    assert decoded["candidate"]["category"] == "t-shirt"
    assert decoded["candidate"]["evidence"][0]["attribute"] == "fabric"
    assert decoded["assessment"]["status"] == "VERIFIED"
    assert decoded["assessment"]["attribute_results"][0]["status"] == "CONFIRMED"
    assert decoded["assessment"]["attribute_results"][0]["requirement"]["attribute"] == "fabric"


def _run_all() -> int:
    checks = sorted(
        (name, obj)
        for name, obj in list(globals().items())
        if name.startswith("test_") and callable(obj)
    )
    failures = 0
    for name, check in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - report every failure deterministically
            failures += 1
            print(f"FAIL {name}: {exc!r}")
        else:
            print(f"PASS {name}")
    print(f"\n{len(checks) - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
