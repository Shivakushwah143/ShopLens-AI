"""Structured attribute extraction for evidence-backed product verification.

This module extracts normalized product attributes from accessible product
detail pages and keeps an :class:`~.product_schema.AttributeEvidence` record
for every value it reads. Sources are tried in order of trustworthiness:

1. ``application/ld+json`` Product data (highest confidence),
2. structured specification tables / definition lists,
3. labelled specification fields in page text,
4. title-only hints (lowest confidence, supplementary).

Extraction never fabricates a value: absent fields stay absent so verification
can report ``UNKNOWN`` instead of guessing.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from bs4 import BeautifulSoup

from .product_schema import AttributeEvidence

__all__ = [
    "normalize_attribute_name",
    "normalize_attribute_value",
    "extract_attributes_from_soup",
    "extract_attributes_from_html",
]

# Canonical attribute -> label aliases. Order matters: more specific labels
# (e.g. "display size") must appear before greedy ones (e.g. "size").
_LABELS: dict[str, tuple[str, ...]] = {
    "ram": ("ram", "installed ram", "ram size", "memory", "ram memory", "memory ram"),
    "storage": ("storage", "internal storage", "storage capacity", "rom", "hard disk", "hard drive", "hdd", "ssd", "total storage"),
    "processor": ("processor", "cpu", "processor brand", "processor type", "processor model", "processor name", "chipset", "cpu model"),
    "gpu": ("gpu", "graphics", "graphic processor", "graphics processor", "gpu model", "dedicated graphics", "graphics memory"),
    "display": ("display", "display size", "screen", "screen size", "display type", "display resolution", "resolution"),
    "battery": ("battery", "battery capacity", "battery type", "battery power"),
    "camera": ("camera", "rear camera", "primary camera", "camera resolution"),
    "os": ("operating system", "os", "operating system type"),
    "brand": ("brand", "brand name"),
    "model": ("model", "model name", "model number", "model no", "sku", "mpn"),
    "fabric": ("fabric", "material", "material composition", "fabric type", "fabric material", "outer material", "main material"),
    "fit": ("fit", "fit type"),
    "gender": ("gender", "ideal for", "department", "suitable for", "target gender"),
    "size": ("size", "size name", "size chart"),
    "color": ("color", "colour", "primary color", "primary colour", "colour family", "color family"),
    "pattern": ("pattern", "print", "pattern type"),
}

_LABEL_INDEX: list[tuple[str, str]] = [
    (alias, attr) for attr, aliases in _LABELS.items() for alias in aliases
]

_UNIT_ATTRS = {"ram", "storage", "battery"}


def normalize_attribute_name(label: str | None) -> str | None:
    """Map a raw specification label to a canonical attribute name."""

    if not label:
        return None
    key = re.sub(r"[^a-z0-9% ]", " ", str(label).lower())
    key = re.sub(r"\s+", " ", key).strip()
    if not key:
        return None
    for alias, attr in _LABEL_INDEX:
        if key == alias:
            return attr
    for alias, attr in _LABEL_INDEX:
        if alias in key:
            return attr
    return None


def normalize_attribute_value(attr: str, raw: str | None) -> str:
    """Normalize units/formats for a canonical attribute value."""

    if raw is None:
        return ""
    value = re.sub(r"\s+", " ", str(raw)).strip().lower()
    if not value:
        return ""
    if attr in {"ram", "storage"}:
        match = re.search(r"(\d+(?:\.\d+)?)\s*(tb|gb)", value)
        if match:
            return f"{match.group(1)}{match.group(2)}"
        return value if len(value) <= 24 else ""
    if attr == "battery":
        match = re.search(r"(\d{3,5})\s*mah", value)
        if match:
            return f"{match.group(1)}mah"
        return value if len(value) <= 24 else ""
    return value if len(value) <= 120 else value[:120]


def _evidence(attr: str, value: str, base_url: str, field: str, excerpt: str) -> AttributeEvidence:
    return AttributeEvidence(
        attribute=attr,
        value=value,
        source_url=base_url or "",
        source_field=field,
        excerpt=excerpt[:160],
    )


def _set(
    attrs: dict[str, str],
    evidence: list[AttributeEvidence],
    attr: str,
    raw: Any,
    base_url: str,
    field: str,
) -> None:
    if raw is None or attr in attrs:
        return
    if isinstance(raw, (list, tuple)):
        raw = " ".join(str(item) for item in raw if item)
    value = normalize_attribute_value(attr, str(raw))
    if not value:
        return
    attrs[attr] = value
    evidence.append(_evidence(attr, value, base_url, field, str(raw)))


def _iter_jsonld_products(data: Any) -> Iterable[dict[str, Any]]:
    if isinstance(data, list):
        for item in data:
            yield from _iter_jsonld_products(item)
        return
    if not isinstance(data, dict):
        return
    if "@graph" in data:
        yield from _iter_jsonld_products(data["@graph"])
    node_type = data.get("@type")
    types = node_type if isinstance(node_type, list) else [node_type]
    if any(str(t).lower() == "product" for t in types if t):
        yield data


def _apply_jsonld(soup: BeautifulSoup, base_url: str, attrs: dict[str, str], evidence: list[AttributeEvidence]) -> None:
    for script in soup.find_all("script"):
        script_type = (script.get("type") or "").lower()
        if "ld+json" not in script_type:
            continue
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for node in _iter_jsonld_products(data):
            brand = node.get("brand")
            if isinstance(brand, dict):
                brand = brand.get("name")
            _set(attrs, evidence, "brand", brand, base_url, "json-ld:brand")
            _set(attrs, evidence, "model", node.get("model") or node.get("mpn") or node.get("sku"), base_url, "json-ld:model")
            _set(attrs, evidence, "color", node.get("color"), base_url, "json-ld:color")
            _set(attrs, evidence, "fabric", node.get("material"), base_url, "json-ld:material")
            for prop in node.get("additionalProperty") or []:
                if not isinstance(prop, dict):
                    continue
                key = normalize_attribute_name(prop.get("name"))
                if key:
                    _set(attrs, evidence, key, prop.get("value"), base_url, f"json-ld:{prop.get('name')}")


def _apply_spec_tables(soup: BeautifulSoup, base_url: str, attrs: dict[str, str], evidence: list[AttributeEvidence]) -> None:
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        label = cells[0].get_text(" ", strip=True)
        value = cells[1].get_text(" ", strip=True)
        _apply_pair(attrs, evidence, label, value, base_url, "spec:table")
    for dl in soup.find_all("dl"):
        term = None
        for child in dl.find_all(["dt", "dd"]):
            text = child.get_text(" ", strip=True)
            if child.name == "dt":
                term = text
            elif term:
                _apply_pair(attrs, evidence, term, text, base_url, "spec:list")
                term = None


def _apply_pair(attrs: dict[str, str], evidence: list[AttributeEvidence], label: str, value: str, base_url: str, field: str) -> None:
    if not value or len(value) > 120:
        return
    key = normalize_attribute_name(label)
    if key:
        _set(attrs, evidence, key, value, base_url, field)


def _apply_labeled_fields(soup: BeautifulSoup, base_url: str, attrs: dict[str, str], evidence: list[AttributeEvidence]) -> None:
    text = soup.get_text(" ", strip=True)
    for match in re.finditer(
        r"\b(RAM|Memory|Storage|Processor|CPU|GPU|Graphics|Battery|Display|Screen|Material Composition|Fabric|Material|Fit|Brand|Model|Colour|Color)\b\s*[:\-]\s*([A-Za-z0-9%+\-./ ]{2,60})",
        text,
        flags=re.I,
    ):
        _apply_pair(attrs, evidence, match.group(1), match.group(2).strip(), base_url, "field:labeled")


def extract_attributes_from_soup(
    soup: BeautifulSoup,
    base_url: str = "",
) -> tuple[dict[str, str], list[AttributeEvidence]]:
    """Extract normalized attributes and their supporting evidence."""

    attrs: dict[str, str] = {}
    evidence: list[AttributeEvidence] = []
    _apply_jsonld(soup, base_url, attrs, evidence)
    _apply_spec_tables(soup, base_url, attrs, evidence)
    _apply_labeled_fields(soup, base_url, attrs, evidence)
    return attrs, evidence


def extract_attributes_from_html(
    html: str,
    base_url: str = "",
) -> tuple[dict[str, str], list[AttributeEvidence]]:
    """Convenience wrapper extracting attributes from a raw HTML document."""

    if not html:
        return {}, []
    return extract_attributes_from_soup(BeautifulSoup(html, "html.parser"), base_url)
