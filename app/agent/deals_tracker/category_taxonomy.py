"""Reusable product-category taxonomy for category-aware retrieval.

This module is additive. It builds on the Gate 1 :mod:`product_schema`
(``ProductCategory`` and ``category_from_text``) and adds:

* multi-signal category inference (title text plus structured attributes),
* a role taxonomy that separates primary products from accessories,
  components, replacements and unrelated listings,
* a mandatory category gate used to reject accessory/unrelated candidates
  before ranking.

It has no side effects and does not import the retailer scraper at module
load, so it is safe to consume from both the scraper and the service layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .product_schema import ProductCategory, category_from_text

__all__ = [
    "CatalogRole",
    "CategoryGate",
    "classify_category",
    "classify_role",
    "category_for_query",
    "evaluate_category",
    "category_query_term",
    "GARMENT_CATEGORIES",
]


class CatalogRole(str, Enum):
    """How a listing relates to the requested primary product."""

    PRIMARY = "primary"
    ACCESSORY = "accessory"
    COMPONENT = "component"
    REPLACEMENT = "replacement"
    UNRELATED = "unrelated"
    UNKNOWN = "unknown"


# Markers that make a listing a supporting good rather than the product itself.
_ACCESSORY_MARKERS: tuple[str, ...] = (
    "charm", "cover", "back cover", "flip cover", "case", "pouch", "sleeve",
    "bag", "backpack", "handbag", "tempered glass", "screen protector",
    "protector", "screen guard", "stand", "holder", "mount", "strap",
    "lanyard", "keychain", "keyring", "sticker", "skin", "bumper",
    "cleaning kit", "cleaner", "cleaning cloth", "dust plug", "sim ejector",
    "charger", "charging cable", "usb cable", "cable", "adapter",
    "power adapter", "power bank", "otg", "dock", "docking station",
    "cooling pad", "cooling fan", "cooling stand", "mouse pad",
)

# Parts/components that are not the requested product even though they share a noun.
_COMPONENT_MARKERS: tuple[str, ...] = (
    "mouse", "graphics card", "gpu card", "motherboard", "ram module",
    "memory module", "ssd drive", "internal ssd", "hard disk drive",
    "external hard drive", "pen drive", "pendrive", "flash drive",
)

_REPLACEMENT_MARKERS: tuple[str, ...] = (
    "replacement", "spare part", "compatible for", "compatible with",
)

# Listings that are raw material rather than a finished garment.
_UNRELATED_MARKERS: tuple[str, ...] = (
    "unstitched", "fabric material", "dress material", "fabric only",
    "fabric by meter", "by meter", "by metre", "per meter", "per metre",
    "running fabric", "suiting fabric", "shirting fabric", "fabric roll",
)

_PHONE_BRANDS = (
    "samsung", "redmi", "realme", "oppo", "vivo", "iqoo", "oneplus", "poco",
    "nothing", "infinix", "tecno", "lava", "micromax", "itel", "honor",
    "motorola", "xiaomi", "iphone", "pixel", "google", "apple", "narzo",
    "galaxy",
)

_LAPTOP_SERIES = (
    "victus", "loq", "legion", "alienware", "rog", "tuf", "nitro", "omen",
    "predator", "katana", "cyborg", "ideapad", "thinkpad", "thinkbook",
    "macbook", "pavilion", "inspiron", "vivobook", "zenbook", "aspire",
    "probook", "elitebook", "latitude", "chromebook", "ultrabook",
)

GARMENT_CATEGORIES: dict[str, ProductCategory] = {
    "t-shirt": ProductCategory.T_SHIRT,
    "shirt": ProductCategory.SHIRT,
    "jeans": ProductCategory.JEANS,
    "kurta": ProductCategory.KURTA,
    "kurta set": ProductCategory.KURTA,
    "dress": ProductCategory.DRESS,
}

_CATEGORY_QUERY_TERMS: dict[ProductCategory, str] = {
    ProductCategory.SMARTPHONE: "smartphone",
    ProductCategory.LAPTOP: "laptop",
    ProductCategory.TABLET: "tablet",
    ProductCategory.TELEVISION: "television",
    ProductCategory.HEADPHONES: "headphones",
    ProductCategory.CAMERA: "camera",
    ProductCategory.SMARTWATCH: "smartwatch",
    ProductCategory.T_SHIRT: "t-shirt",
    ProductCategory.SHIRT: "shirt",
    ProductCategory.JEANS: "jeans",
    ProductCategory.KURTA: "kurta",
    ProductCategory.DRESS: "dress",
}

_MARKER_PATTERNS = {
    marker: re.compile(rf"\b{re.escape(marker)}s?\b") for marker in (
        *_ACCESSORY_MARKERS, *_COMPONENT_MARKERS, *_UNRELATED_MARKERS, *_REPLACEMENT_MARKERS
    )
}


@dataclass(frozen=True)
class CategoryGate:
    """Outcome of validating a candidate against the requested category."""

    matched: bool
    category: ProductCategory
    role: CatalogRole
    reason: str | None = None
    status: str = "CONFIRMED"


def _coerce_category(value: ProductCategory | str | None) -> ProductCategory:
    if isinstance(value, ProductCategory):
        return value
    if isinstance(value, str):
        try:
            return ProductCategory(value)
        except ValueError:
            return ProductCategory.UNKNOWN
    return ProductCategory.UNKNOWN


def _blob(title: str | None, attributes: dict[str, str] | None = None) -> str:
    parts = [title or ""]
    if attributes:
        parts.extend(str(v) for v in attributes.values() if v)
    return " ".join(parts).lower()


def _structured_category(attributes: dict[str, str] | None = None) -> ProductCategory:
    if not attributes:
        return ProductCategory.UNKNOWN
    for key in ("product_category", "category", "product_type", "item_type", "type"):
        value = attributes.get(key) or attributes.get(key.title())
        category = category_from_text(value or "")
        if category is not ProductCategory.UNKNOWN:
            return category
    return ProductCategory.UNKNOWN


def _has(text: str, marker: str) -> bool:
    pattern = _MARKER_PATTERNS.get(marker)
    if pattern is None:
        pattern = re.compile(rf"\b{re.escape(marker)}s?\b")
        _MARKER_PATTERNS[marker] = pattern
    return pattern.search(text) is not None


def classify_role(title: str | None, attributes: dict[str, str] | None = None) -> CatalogRole:
    """Classify a listing as primary vs supporting/unrelated."""

    text = _blob(title, attributes)
    if not text.strip():
        return CatalogRole.UNKNOWN
    if any(_has(text, marker) for marker in _UNRELATED_MARKERS):
        return CatalogRole.UNRELATED
    if any(_has(text, marker) for marker in _REPLACEMENT_MARKERS):
        return CatalogRole.REPLACEMENT
    if any(_has(text, marker) for marker in _ACCESSORY_MARKERS):
        return CatalogRole.ACCESSORY
    if any(_has(text, marker) for marker in _COMPONENT_MARKERS):
        return CatalogRole.COMPONENT
    return CatalogRole.PRIMARY


def _has_tablet_signal(text: str) -> bool:
    return bool(re.search(r"\b(tablet|ipad|tab)\b|\bgalaxy tab\b|\btab [as]\b", text))


def _has_tv_signal(text: str) -> bool:
    return bool(re.search(r"\b(television|smart tv|led tv|oled tv|qled)\b|\btv\b", text))


def _has_headphone_signal(text: str) -> bool:
    return bool(re.search(r"\b(headphone|headphones|earphone|earphones|earbud|earbuds|headset|airpods|neckband|tws)\b", text))


def _has_camera_signal(text: str) -> bool:
    if _has_phone_signal(text):
        return False
    return bool(re.search(r"\b(dslr|mirrorless|gopro|camera\s+(?:body|kit|lens|with|for)|digital\s+camera)\b", text))


def _has_smartwatch_signal(text: str) -> bool:
    return bool(re.search(r"\b(smartwatch|smart watch|fitness band|fitness tracker)\b", text))


def _has_laptop_signal(text: str) -> bool:
    if re.search(r"\b(laptop|notebook|chromebook|ultrabook|macbook|netbook)\b", text):
        return True
    if re.search(rf"\b({'|'.join(_LAPTOP_SERIES)})\b", text):
        return True
    if re.search(r"\b(rtx|gtx|geforce|radeon rx)\b", text) and re.search(r"\b(ram|ssd|hdd|gb)\b", text):
        return True
    if re.search(r"\b(intel core i[3579]|ryzen [3579]|celeron|pentium)\b", text) and re.search(r"\b(ram|ssd|hdd|gb)\b", text):
        return True
    return False


def _has_phone_signal(text: str) -> bool:
    if re.search(r"\b(smartphone|smart phone|mobile phone|cell phone|phone|mobile)\b", text):
        return True
    if re.search(r"\b(5g|4g|lte)\b", text) and re.search(r"\b(ram|storage|sim|battery|gb)\b", text):
        return True
    if re.search(r"\b(dual sim|octa core|snapdragon|dimensity|helio|mediatek)\b", text) and re.search(r"\b(ram|storage|gb|5g|4g)\b", text):
        return True
    if re.search(rf"\b({'|'.join(_PHONE_BRANDS)})\b", text) and re.search(r"\b(ram|storage|gb|5g|4g|sim)\b", text):
        return True
    return False


def classify_category(title: str | None, attributes: dict[str, str] | None = None) -> ProductCategory:
    """Infer the canonical category from title text and structured attributes."""

    structured = _structured_category(attributes)
    if structured is not ProductCategory.UNKNOWN:
        return structured
    text = _blob(title, attributes)
    if not text.strip():
        return ProductCategory.UNKNOWN
    role = classify_role(title, attributes)
    if role in (CatalogRole.ACCESSORY, CatalogRole.COMPONENT, CatalogRole.REPLACEMENT):
        return ProductCategory.ACCESSORY
    if role is CatalogRole.UNRELATED:
        return ProductCategory.UNKNOWN
    if _has_phone_signal(text):
        return ProductCategory.SMARTPHONE
    if _has_laptop_signal(text):
        return ProductCategory.LAPTOP
    if _has_tablet_signal(text):
        return ProductCategory.TABLET
    if _has_tv_signal(text):
        return ProductCategory.TELEVISION
    if _has_camera_signal(text):
        return ProductCategory.CAMERA
    if _has_headphone_signal(text):
        return ProductCategory.HEADPHONES
    if _has_smartwatch_signal(text):
        return ProductCategory.SMARTWATCH
    return category_from_text(text)


def category_for_query(query: str | None) -> ProductCategory:
    """Infer the requested primary category from a natural-language query."""

    text = (query or "").lower()
    if not text.strip():
        return ProductCategory.UNKNOWN
    category = classify_category(text)
    if category is ProductCategory.ACCESSORY:
        return ProductCategory.UNKNOWN
    return category


def category_query_term(category: ProductCategory | str | None) -> str | None:
    """Canonical retailer search term for a category, when known."""

    return _CATEGORY_QUERY_TERMS.get(_coerce_category(category))


def evaluate_category(
    requested: ProductCategory | str | None,
    title: str | None,
    attributes: dict[str, str] | None = None,
) -> CategoryGate:
    """Mandatory category gate applied before ranking.

    A definite primary category requested by the user (smartphone, laptop,
    garment, ...) is matched only when the candidate is a primary listing of
    that same category. Accessories, components, replacements and unrelated
    listings are rejected. Unknown-category candidates are reported as
    unverified rather than silently accepted.
    """

    req = _coerce_category(requested)
    category = classify_category(title, attributes)
    role = classify_role(title, attributes)

    if req is ProductCategory.UNKNOWN:
        return CategoryGate(True, category, role, None)

    if req is ProductCategory.ACCESSORY:
        matched = role is CatalogRole.ACCESSORY or category is ProductCategory.ACCESSORY
        return CategoryGate(matched, category, role, None if matched else "category_mismatch:primary")

    if role in (CatalogRole.ACCESSORY, CatalogRole.COMPONENT, CatalogRole.REPLACEMENT, CatalogRole.UNRELATED):
        return CategoryGate(False, category, role, f"category_mismatch:{role.value}", "CONTRADICTED")

    if category is req:
        return CategoryGate(True, category, role, None)
    if category is ProductCategory.UNKNOWN:
        return CategoryGate(False, category, role, "category_unverified", "UNKNOWN")
    return CategoryGate(False, category, role, f"category_mismatch:{category.value}", "CONTRADICTED")
