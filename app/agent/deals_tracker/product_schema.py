"""Gate 1 typed data foundation for category-aware product retrieval.

This module is additive and intentionally side-effect free. It defines the
normalized contracts (categories, requirements, candidates, evidence and
match assessments) described in the technical design, plus a backward
compatible bridge to the existing ``ScrapedProduct`` scraper output.

Nothing here is wired into search routing, parsing, filtering, ranking,
retailer scraping, API responses, SSE or the frontend. It is a pure data
layer that later gates can consume.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .scraper import ScrapedProduct

__all__ = [
    "ProductCategory",
    "ProductRole",
    "RequirementStatus",
    "MatchStatus",
    "SearchRequirement",
    "AttributeEvidence",
    "ProductCandidate",
    "RequirementResult",
    "MatchAssessment",
    "category_from_text",
    "role_for_category",
    "is_primary_category",
    "candidate_from_scraped",
    "to_scraped_product",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProductCategory(str, Enum):
    """Canonical product categories shared across providers."""

    SMARTPHONE = "smartphone"
    LAPTOP = "laptop"
    TABLET = "tablet"
    TELEVISION = "television"
    HEADPHONES = "headphones"
    CAMERA = "camera"
    SMARTWATCH = "smartwatch"
    T_SHIRT = "t-shirt"
    SHIRT = "shirt"
    JEANS = "jeans"
    KURTA = "kurta"
    DRESS = "dress"
    ACCESSORY = "accessory"
    UNKNOWN = "unknown"


class ProductRole(str, Enum):
    """How a candidate relates to the requested product.

    A ``PRIMARY`` candidate is the product itself, ``ACCESSORY`` is a
    supporting good (case, charger, charm), and ``UNKNOWN`` is a candidate
    that could not be classified. Only primary candidates may ever be
    presented as the requested item; accessory and unknown candidates must
    never be silently treated as matches.
    """

    PRIMARY = "primary"
    ACCESSORY = "accessory"
    UNKNOWN = "unknown"


class RequirementStatus(str, Enum):
    """Per-requirement verification state.

    ``UNKNOWN`` means no evidence was found; it is not a mismatch. Only
    explicit contradictory evidence may produce ``CONTRADICTED``.
    """

    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"
    CONTRADICTED = "CONTRADICTED"


class MatchStatus(str, Enum):
    """Overall eligibility of a candidate against the requirements."""

    VERIFIED = "VERIFIED"
    POSSIBLE = "POSSIBLE"
    REJECTED = "REJECTED"


# Ordered most specific first so overlapping aliases resolve deterministically.
_CATEGORY_ALIASES: tuple[tuple[ProductCategory, tuple[str, ...]], ...] = (
    (ProductCategory.SMARTPHONE, ("smartphone", "smart phone", "mobile phone", "cell phone", "5g phone")),
    (ProductCategory.LAPTOP, ("laptop", "ultrabook", "chromebook")),
    (ProductCategory.TABLET, ("tablet", "ipad")),
    (ProductCategory.TELEVISION, ("television", "smart tv", "led tv", "oled tv")),
    (ProductCategory.HEADPHONES, ("headphone", "earphone", "earbud", "headset", "airpods")),
    (ProductCategory.CAMERA, ("camera", "dslr", "mirrorless")),
    (ProductCategory.SMARTWATCH, ("smartwatch", "smart watch", "fitness band")),
    (ProductCategory.T_SHIRT, ("t-shirt", "t shirt", "tee shirt")),
    (ProductCategory.SHIRT, ("shirt",)),
    (ProductCategory.JEANS, ("jeans", "denim pant")),
    (ProductCategory.KURTA, ("kurta", "kurti")),
    (ProductCategory.DRESS, ("dress", "gown", "frock")),
)

# Markers that make a listing an accessory regardless of the primary noun it
# mentions (e.g. a "smart phone charm" mentions a phone but is an accessory).
_ACCESSORY_MARKERS: tuple[str, ...] = (
    "charm", "cover", "case", "tempered glass", "screen protector", "protector",
    "charger", "charging cable", "usb cable", "cable", "adapter", "power adapter",
    "sleeve", "stand", "holder", "strap", "mount", "cleaning kit", "mouse pad",
    "backpack", "pouch",
)

_NON_PRIMARY_CATEGORIES = frozenset({ProductCategory.ACCESSORY, ProductCategory.UNKNOWN})


def category_from_text(text: str | None) -> ProductCategory:
    """Infer a canonical category from free text deterministically.

    Accessory markers take precedence over primary nouns so that listings
    like "smart phone charm" resolve to ``ACCESSORY`` rather than
    ``SMARTPHONE``. Text that matches nothing resolves to ``UNKNOWN``.
    """

    normalized = (text or "").lower()
    if any(marker in normalized for marker in _ACCESSORY_MARKERS):
        return ProductCategory.ACCESSORY
    for category, aliases in _CATEGORY_ALIASES:
        if any(alias in normalized for alias in aliases):
            return category
    return ProductCategory.UNKNOWN


def role_for_category(category: ProductCategory | str | None) -> ProductRole:
    """Map a category to its role, treating unrecognized input as unknown."""

    if isinstance(category, str):
        try:
            category = ProductCategory(category)
        except ValueError:
            return ProductRole.UNKNOWN
    if category is None:
        return ProductRole.UNKNOWN
    if category is ProductCategory.ACCESSORY:
        return ProductRole.ACCESSORY
    if category is ProductCategory.UNKNOWN:
        return ProductRole.UNKNOWN
    return ProductRole.PRIMARY


def is_primary_category(category: ProductCategory | str | None) -> bool:
    return role_for_category(category) is ProductRole.PRIMARY


@dataclass(frozen=True)
class SearchRequirement:
    """A single attribute the user asked for.

    ``required`` distinguishes mandatory requirements (which gate verified
    matches) from preferred ones (which only influence ranking).
    """

    attribute: str
    value: str | None = None
    required: bool = True

    @property
    def name(self) -> str:
        """Design-document alias for :attr:`attribute`."""

        return self.attribute

    @classmethod
    def mandatory(cls, attribute: str, value: str | None = None) -> "SearchRequirement":
        return cls(attribute=attribute, value=value, required=True)

    @classmethod
    def preferred(cls, attribute: str, value: str | None = None) -> "SearchRequirement":
        return cls(attribute=attribute, value=value, required=False)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(frozen=True)
class AttributeEvidence:
    """Where a claimed attribute value came from and when it was captured."""

    attribute: str
    value: str
    source_url: str
    source_field: str
    excerpt: str | None = None
    retrieved_at: datetime = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass
class ProductCandidate:
    """Normalized candidate produced by a provider adapter.

    The first seven fields mirror :class:`ScrapedProduct` so converted
    objects preserve existing field names and ordering. The remaining
    fields are additive and defaulted, keeping existing constructors valid.
    """

    name: str
    price: float
    rating: float | None
    link: str
    source: str
    image_url: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    retailer_product_id: str | None = None
    currency: str = "INR"
    evidence: list[AttributeEvidence] = field(default_factory=list)
    variant: dict[str, str] = field(default_factory=dict)
    availability: str | None = None
    category: ProductCategory = ProductCategory.UNKNOWN
    identity: str | None = None

    @property
    def role(self) -> ProductRole:
        return role_for_category(self.category)

    @property
    def is_primary(self) -> bool:
        return self.role is ProductRole.PRIMARY

    def evidence_for(self, attribute: str) -> list[AttributeEvidence]:
        key = attribute.lower()
        return [item for item in self.evidence if item.attribute.lower() == key]

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(frozen=True)
class RequirementResult:
    """Verification outcome for one requirement against one candidate."""

    requirement: SearchRequirement
    status: RequirementStatus
    evidence: AttributeEvidence | None = None
    note: str | None = None

    @property
    def attribute(self) -> str:
        return self.requirement.attribute

    @classmethod
    def confirmed(
        cls,
        requirement: SearchRequirement,
        evidence: AttributeEvidence | None = None,
        note: str | None = None,
    ) -> "RequirementResult":
        return cls(requirement=requirement, status=RequirementStatus.CONFIRMED, evidence=evidence, note=note)

    @classmethod
    def unknown(
        cls,
        requirement: SearchRequirement,
        note: str | None = None,
    ) -> "RequirementResult":
        return cls(requirement=requirement, status=RequirementStatus.UNKNOWN, evidence=None, note=note)

    @classmethod
    def contradicted(
        cls,
        requirement: SearchRequirement,
        evidence: AttributeEvidence | None = None,
        note: str | None = None,
    ) -> "RequirementResult":
        return cls(requirement=requirement, status=RequirementStatus.CONTRADICTED, evidence=evidence, note=note)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass
class MatchAssessment:
    """Aggregate verification result for a candidate.

    ``VERIFIED`` requires every mandatory requirement to be ``CONFIRMED``
    and no contradiction. Missing mandatory evidence yields ``POSSIBLE``.
    Any contradiction yields ``REJECTED``.
    """

    status: MatchStatus
    attribute_results: list[RequirementResult] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    explanation: str = ""

    @classmethod
    def from_results(
        cls,
        results: list[RequirementResult],
        *,
        explanation: str = "",
    ) -> "MatchAssessment":
        ordered = list(results)
        contradictions = [r.attribute for r in ordered if r.status is RequirementStatus.CONTRADICTED]
        missing_required = [
            r.attribute
            for r in ordered
            if r.requirement.required and r.status is RequirementStatus.UNKNOWN
        ]
        if contradictions:
            status = MatchStatus.REJECTED
        elif missing_required:
            status = MatchStatus.POSSIBLE
        else:
            status = MatchStatus.VERIFIED
        if not explanation:
            explanation = _explain(status, missing_required, contradictions)
        return cls(
            status=status,
            attribute_results=ordered,
            missing_required=missing_required,
            contradictions=contradictions,
            explanation=explanation,
        )

    @property
    def eligible(self) -> bool:
        """True only when the candidate is fully verified and may be shown."""

        return self.status is MatchStatus.VERIFIED

    @property
    def is_verified(self) -> bool:
        return self.status is MatchStatus.VERIFIED

    @property
    def is_possible(self) -> bool:
        return self.status is MatchStatus.POSSIBLE

    @property
    def is_rejected(self) -> bool:
        return self.status is MatchStatus.REJECTED

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


def _explain(status: MatchStatus, missing_required: list[str], contradictions: list[str]) -> str:
    if status is MatchStatus.REJECTED:
        return "Contradicted requirements: " + ", ".join(contradictions) + "."
    if status is MatchStatus.POSSIBLE:
        return "Missing evidence for required attributes: " + ", ".join(missing_required) + "."
    return "All required attributes confirmed."


def candidate_from_scraped(
    product: ScrapedProduct,
    *,
    category: ProductCategory | None = None,
    retailer_product_id: str | None = None,
    currency: str = "INR",
    evidence: list[AttributeEvidence] | None = None,
    variant: dict[str, str] | None = None,
    availability: str | None = None,
    identity: str | None = None,
) -> ProductCandidate:
    """Convert an existing :class:`ScrapedProduct` without losing fields.

    Every pre-existing field is carried over verbatim. When ``category`` is
    omitted it is inferred from the product name.
    """

    return ProductCandidate(
        name=product.name,
        price=product.price,
        rating=product.rating,
        link=product.link,
        source=product.source,
        image_url=product.image_url,
        attributes=dict(product.attributes or {}),
        retailer_product_id=retailer_product_id,
        currency=currency,
        evidence=list(evidence or []),
        variant=dict(variant or {}),
        availability=availability,
        category=category if category is not None else category_from_text(product.name),
        identity=identity,
    )


def to_scraped_product(candidate: ProductCandidate) -> ScrapedProduct:
    """Convert back to :class:`ScrapedProduct`, preserving its public shape.

    An empty attribute map becomes ``None`` to match the original default
    instead of leaking an empty dict into existing consumers.
    """

    return ScrapedProduct(
        name=candidate.name,
        price=candidate.price,
        rating=candidate.rating,
        link=candidate.link,
        source=candidate.source,
        image_url=candidate.image_url,
        attributes=dict(candidate.attributes) or None,
    )


def _jsonable(value: Any) -> Any:
    """Recursively convert schema objects into JSON-serializable values."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
