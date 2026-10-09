from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .trusted_sources import detect_intent

_PRODUCT_ALLOW_DOMAINS = {
    "amazon.in",
    "flipkart.com",
    "croma.com",
    "reliancedigital.in",
    "tatacliq.com",
    "vijaysales.com",
    "91mobiles.com",
    "gadgets360.com",
    "mysmartprice.com",
    "digit.in",
    "smartprix.com",
}

_FLIGHT_ALLOW_DOMAINS = {
    "ixigo.com",
    "skyscanner.com",
    "kayak.com",
    "makemytrip.com",
    "easemytrip.com",
    "cleartrip.com",
    "goibibo.com",
}

_HOTEL_ALLOW_DOMAINS = {
    "booking.com",
    "agoda.com",
    "tripadvisor.com",
    "goibibo.com",
    "makemytrip.com",
    "hotels.com",
}

_BLOCK_DOMAINS = {
    "sourceforge.net",
    "medium.com",
    "geeksforgeeks.org",
}


def _domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    return urlparse(url).netloc.replace("www.", "").lower() or None


@dataclass
class NormalizedItem:
    name: str
    price: float | None
    rating: float | None
    link: str | None
    source_domain: str | None
    raw: dict[str, Any]


class ResultExtractor:
    """Convert raw HTML/text into a normalized list of items (name/price/rating/link)."""

    def extract_items(
        self,
        *,
        html: str,
        base_url: str | None = None,
        max_items: int = 20,
        user_query: str | None = None,
        intent: str | None = None,
    ) -> list[NormalizedItem]:
        inferred_intent = intent or (detect_intent(user_query or "") if user_query else "general")
        
        # Specialized Flight Pipeline Delegation
        if inferred_intent == "flight":
            try:
                from .flight_extractor import FlightExtractor
                from .flight_utils import extract_flight_params
                f_params = extract_flight_params(user_query or "")
                f_extractor = FlightExtractor()
                return f_extractor.extract(
                    html=html,
                    base_url=base_url,
                    time_pref=f_params.get("time_pref"),
                    target_date=f_params.get("date_str")
                )
            except Exception as e:
                print(f"[TaskAutomationExtractor] Flight delegation failed: {e}")
                # fall back to default logic if something goes wrong
        
        soup = BeautifulSoup(html or "", "html.parser")
        domain = _domain_from_url(base_url)

        # Domain filtering for product/travel tasks: only allow commercial/trusted sources.
        if inferred_intent in {"product", "flight", "hotel"}:
            if domain and domain in _BLOCK_DOMAINS:
                print(f"[TaskAutomationExtractor] blocked_domain={domain}")
                return []
            
            allowed = set()
            if inferred_intent == "product": allowed = _PRODUCT_ALLOW_DOMAINS
            elif inferred_intent == "flight": allowed = _FLIGHT_ALLOW_DOMAINS
            elif inferred_intent == "hotel": allowed = _HOTEL_ALLOW_DOMAINS
            
            if domain and domain not in allowed:
                print(f"[TaskAutomationExtractor] skip_non_commercial_domain={domain}")
                return []

            # Skip blog/review/SEO pages.
            page_text = soup.get_text(" ", strip=True).lower()
            blog_markers = ["top 10", "best", "guide", "review", "alternatives"]
            if any(m in page_text for m in blog_markers):
                # Trust domains in allowed set to bypass blog filter (they often have review-like keywords).
                if domain not in allowed:
                    print("[TaskAutomationExtractor] skip_blog_like_page")
                    return []

        # Broader but smarter selectors (higher recall on modern sites).
        selectors = [
            "article",
            "[class*='product']",
            "[class*='item']",
            "[class*='card']",
            "[class*='listing']",
            "[class*='result']",
            "li",
        ]

        candidates = []
        for selector in selectors:
            for node in soup.select(selector)[:400]:
                text = node.get_text(" ", strip=True)
                if not text:
                    continue
                if _looks_like_login_wall(text):
                    continue
                # Relax candidate filtering (CRITICAL FIX): collect first, filter later.
                if len(text) > 60:
                    candidates.append(node)

        items: list[NormalizedItem] = []
        print(f"[TaskAutomationExtractor] url={base_url} intent={inferred_intent} candidates={len(candidates)}")

        for node in candidates:
            if len(items) >= max_items * 3:
                break

            text = node.get_text(" ", strip=True)
            name = _guess_name(node, text)
            price = _parse_price(text, node=node)
            rating = _parse_rating(text)
            link = _extract_link(node, base_url)

            if not name:
                continue

            # Junk filter (CRITICAL)
            if _is_junk_item(name, text):
                continue

            # Minimum quality rules by intent
            if inferred_intent == "flight":
                if price is None:
                    continue
            elif inferred_intent in {"product", "hotel"}:
                if price is None and rating is None:
                    continue

            # Relevance scoring (filter out irrelevant UI labels)
            score = _score_item(name, text, user_query) if user_query else 5
            if user_query and score < 3:
                continue

            items.append(
                NormalizedItem(
                    name=_clean_name(name),
                    price=price,
                    rating=rating,
                    link=link,
                    source_domain=domain,
                    raw={"text": text[:1200]},
                )
            )

        # Post-extraction filtering + dedupe
        items = _dedupe_items(items)
        items = _prefer_structured(items, keep=max_items)
        print(f"[TaskAutomationExtractor] filtered_items={len(items)}")

        # Mandatory fallback: never return empty.
        if not items:
            items.append(
                NormalizedItem(
                    name=(domain or "Website"),
                    price=None,
                    rating=None,
                    link=base_url,
                    source_domain=domain,
                    raw={},
                )
            )

        return items[:max_items]


def _looks_like_login_wall(text: str) -> bool:
    lowered = text.lower()
    return any(k in lowered for k in ["sign in", "log in", "login", "create an account", "verify you are human"])


def _has_any_price(text: str) -> bool:
    return bool(re.search(r"(₹\s?[\d,]+|\$\s?[\d,]+|rs\.?\s?[\d,]+)", text, flags=re.IGNORECASE))


def _has_any_rating(text: str) -> bool:
    return bool(re.search(r"(\d\.\d)\s*(/5|out of 5|stars?)", text, flags=re.IGNORECASE))


def _parse_price(text: str, *, node=None) -> float | None:
    """Extract price from node text and common attributes.

    Supports:
    - ₹12,999
    - Rs 12999
    - INR 12,999
    - 'Starting from ₹...'
    - prices inside aria-label/title attributes
    """
    chunks: list[str] = [text or ""]
    if node is not None:
        try:
            # common attributes where sites stash price
            for attr in ["aria-label", "title", "data-price", "content", "value"]:
                v = node.get(attr)
                if v:
                    chunks.append(str(v))
            # also scan child attributes of a few nodes
            for child in node.find_all(True)[:25]:
                for attr in ["aria-label", "title", "data-price", "content", "value"]:
                    v = child.get(attr)
                    if v:
                        chunks.append(str(v))
        except Exception:
            pass

    blob = " | ".join(chunks)

    patterns = [
        r"(?:price:?|starting\s+at)?\s*(?:₹|rs\.?|inr)\s*([\d,]{3,})",
        r"(?:price:?|starting\s+at)\s*([\d,]{3,})",
        r"starting\s+from\s*(?:₹|rs\.?|inr)\s*([\d,]{3,})",
    ]

    candidates: list[float] = []
    for pat in patterns:
        for m in re.finditer(pat, blob, flags=re.IGNORECASE):
            raw = (m.group(1) or "").replace(",", "").strip()
            try:
                candidates.append(float(raw))
            except ValueError:
                continue

    if not candidates:
        return None
    # Prefer the lowest price found (often the offer price).
    return min(candidates)


def _parse_rating(text: str) -> float | None:
    # Try specific "Rating: X.X" first (common in comparison sites)
    m = re.search(r"rating:?\s*(\d\.\d)", text, flags=re.IGNORECASE)
    if not m:
        # Try star ratings (e.g. "5 star", "4.5 stars")
        m = re.search(r"(\d(?:\.\d)?)\s*(?:star|-star)", text, flags=re.IGNORECASE)
    if not m:
        # Fallback to suffix-based ratings (e.g. 4.5/5 or 4.5 stars)
        m = re.search(r"(\d\.\d)\s*(/5|out of 5|stars?)", text, flags=re.IGNORECASE)

    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _extract_link(node, base_url: str | None) -> str | None:
    if node is None:
        return None

    def normalize(href: str) -> str | None:
        h = (href or "").strip()
        if not h or h == "#" or h.startswith("#"):
            return None
        low = h.lower()
        if low.startswith("javascript:") or low.startswith("mailto:"):
            return None
        # Ignore image/static assets
        if any(low.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".css", ".js", ".ico"]):
            return None
        if "//" in h and h.startswith("//"):
            h = "https:" + h
        if h.startswith("http://") or h.startswith("https://"):
            return h
        if base_url and h.startswith("/"):
            parsed = urlparse(base_url)
            return f"{parsed.scheme}://{parsed.netloc}{h}"
        return None

    anchors = node.find_all("a", href=True)
    best: tuple[float, str] | None = None
    for a in anchors[:40]:
        href = normalize(str(a.get("href") or ""))
        if not href:
            continue
        txt = a.get_text(" ", strip=True)
        if not txt or len(txt) < 5:
            continue
        # prefer links with longer visible text and not generic "view"
        low_txt = txt.lower()
        if low_txt in {"view", "open", "more", "details", "buy"}:
            pass
        score = min(len(txt), 60) / 10.0
        if best is None or score > best[0]:
            best = (score, href)

    return best[1] if best else None


def _guess_name(node, text: str) -> str:
    for tag in ["h1", "h2", "h3"]:
        h = node.find(tag)
        if h:
            t = h.get_text(strip=True)
            if 10 <= len(t) <= 120:
                return t

    for tag in ["strong", "b"]:
        el = node.find(tag)
        if el:
            t = el.get_text(strip=True)
            if 10 <= len(t) <= 120:
                return t

    a = node.find("a")
    if a:
        t = a.get_text(strip=True)
        if 10 <= len(t) <= 140:
            return t

    return ""


def _clean_name(name: str) -> str:
    return re.sub(r"\s{2,}", " ", name).strip()


def _dedupe_items(items: list[NormalizedItem]) -> list[NormalizedItem]:
    seen: set[tuple[str, str, str]] = set()
    out: list[NormalizedItem] = []
    for it in items:
        key = (
            (it.name or "").strip().lower(),
            "" if it.price is None else str(int(it.price)),
            (it.link or "").strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _prefer_structured(items: list[NormalizedItem], *, keep: int) -> list[NormalizedItem]:
    # Prefer items with a real name, and at least price or rating.
    good = [
        it
        for it in items
        if (it.name and len(it.name) >= 10) and (it.price is not None or it.rating is not None or it.link)
    ]
    if len(good) >= 3:
        items = good

    def score(it: NormalizedItem) -> float:
        s = 0.0
        if it.price is not None:
            s += 2.0
        if it.rating is not None:
            s += it.rating / 5.0
        if it.link:
            s += 0.2
        # penalize overly long names (usually noisy)
        if len(it.name) > 140:
            s -= 0.5
        return s

    # Sort final results (price first, then rating)
    ranked = sorted(items, key=lambda x: (x.price is None, -(x.rating or 0.0)))
    return ranked[:keep]


def _is_junk_item(name: str, text: str) -> bool:
    lowered = (f"{name} {text}").lower()
    junk_patterns = [
        r"\bsearch\b",
        r"\bfilter\b",
        r"\bsort\b",
        r"\blogin\b",
        r"\bsign in\b",
        r"\bmenu\b",
        r"\bnavigation\b",
        r"\bhome\b",
        r"\bexplore\b",
        r"\bcategories\b",
        r"\boptions\b",
        r"economy class",
        r"business class",
        r"premium economy",
    ]
    for pat in junk_patterns:
        if re.search(pat, lowered):
            return True
    if len(name.split()) < 2:
        return True
    return False


def _score_item(name: str, text: str, query: str) -> int:
    score = 0
    q_words = (query or "").lower().split()
    name_low = (name or "").lower()
    text_low = (text or "").lower()
    for w in q_words:
        if not w or len(w) < 3:
            continue
        if w in name_low:
            score += 3
        if w in text_low:
            score += 1
    if _parse_price(text) is not None:
        score += 2
    if _parse_rating(text) is not None:
        score += 1
    
    # Heuristic for airport codes (e.g. DEL, BOM, DXB)
    if re.search(r"\b[A-Z]{3}\b", name + text):
        score += 1
        
    return score

