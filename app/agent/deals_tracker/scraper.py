from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from bs4 import BeautifulSoup
import httpx

from app.browser.worker import BrowserWorker
from app.safety.guardrails import safety_guard

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

SEARCH_TEMPLATES = {
    "amazon": "https://www.amazon.in/s?k={q}",
    "flipkart": "https://www.flipkart.com/search?q={q}",
    "croma": "https://www.croma.com/searchB?q={q}%3Arelevance&text={q}",
}
RETAILER_DOMAINS = {"amazon": "amazon.in", "flipkart": "flipkart.com", "croma": "croma.com"}
_BLOCK_MARKERS = ["captcha", "verify you are human", "robot or human", "access denied", "403 forbidden", "unusual traffic", "checking your browser", "cloudflare"]


@dataclass
class ScrapedProduct:
    name: str
    price: float
    rating: float | None
    link: str
    source: str
    image_url: str | None = None
    attributes: dict[str, str] | None = None


@dataclass
class SourceSearchResult:
    retailer: str
    status: str
    products: list[ScrapedProduct]
    message: str | None = None
    diagnostics: dict[str, Any] | None = None


@dataclass
class ProductDetailResult:
    """Product-detail fetch outcome with the raw HTML retained for evidence."""

    product: ScrapedProduct | None
    status: str
    html: str = ""
    base_url: str = ""
    message: str | None = None
    diagnostics: dict[str, Any] | None = None


@dataclass
class FetchResult:
    status: str
    html: str
    message: str | None = None
    method: str = "unknown"
    http_status: int | None = None
    final_url: str | None = None


@dataclass
class ExtractionResult:
    products: list[ScrapedProduct]
    diagnostics: dict[str, Any]


@dataclass
class SearchPageResult:
    status: str
    products: list[ScrapedProduct]
    message: str | None = None
    diagnostics: dict[str, Any] | None = None


async def search_products(query: str, *, task_id: str = "") -> list[ScrapedProduct]:
    products: list[ScrapedProduct] = []
    for retailer in SEARCH_TEMPLATES:
        result = await search_retailer(retailer, query, task_id=task_id)
        products.extend(result.products)
    return _deduplicate(products)


async def search_retailer(retailer: str, query: str, *, task_id: str = "", timeout_seconds: float = 12.0) -> SourceSearchResult:
    retailer_key = retailer.lower().strip()
    template = SEARCH_TEMPLATES.get(retailer_key)
    if not template:
        return SourceSearchResult(retailer_key, "unsupported", [], "Retailer is not supported", {"retrieval_method": "none"})
    try:
        page_result = await asyncio.wait_for(_scrape_search_page_detailed(template.format(q=quote_plus(query)), task_id=task_id), timeout_seconds)
    except TimeoutError:
        return SourceSearchResult(retailer_key, "timeout", [], "Retailer search timed out", {"retrieval_method": "timeout"})
    except Exception as exc:
        logger.warning("deals_scraper: retailer search failed %s - %s", retailer_key, exc)
        return SourceSearchResult(retailer_key, "failed", [], str(exc), {"retrieval_method": "exception"})

    if page_result.status in {"blocked", "timeout", "failed", "unavailable"}:
        return SourceSearchResult(retailer_key, page_result.status, [], page_result.message, page_result.diagnostics)
    products = [p for p in page_result.products if _domain_matches_retailer(p.link, retailer_key)]
    diagnostics = dict(page_result.diagnostics or {})
    diagnostics["final_verified_products"] = len(products)
    if not products:
        return SourceSearchResult(retailer_key, "no_results", [], page_result.message or "No verified product cards found", diagnostics)
    return SourceSearchResult(retailer_key, "success", _deduplicate(products), diagnostics=diagnostics)


async def scrape_url(url: str, *, task_id: str = "") -> list[ScrapedProduct]:
    result = await _fetch_page_html(url, task_id=task_id)
    if result.status != "success" or not result.html:
        return []
    soup = BeautifulSoup(result.html, "html.parser")
    domain = _domain(url)
    if is_valid_product_link(url):
        product = _extract_product_page_for_domain(soup, url, domain) or _extract_generic_product(soup, url, domain)
        return [product] if product else []
    return extract_product_data(result.html, base_url=url)


async def scrape_product_detail(url: str, *, task_id: str = "") -> "ProductDetailResult":
    """Fetch a product-detail page and return its HTML plus the parsed product.

    Unlike :func:`scrape_url` this preserves the fetched document so callers
    can run evidence-bearing structured attribute extraction on it. Failures
    are reported explicitly instead of raising.
    """

    fetched = await _fetch_page_html(url, task_id=task_id)
    base_url = fetched.final_url or url
    diagnostics = {"retrieval_method": fetched.method, "http_status": fetched.http_status}
    if fetched.status != "success" or not fetched.html:
        return ProductDetailResult(None, fetched.status, "", base_url, fetched.message, diagnostics)
    soup = BeautifulSoup(fetched.html, "html.parser")
    domain = _domain(url)
    product = _extract_product_page_for_domain(soup, url, domain) or _extract_generic_product(soup, url, domain)
    status = "success" if product else "empty"
    message = None if product else "No structured product data found on detail page"
    if product is None:
        product = _extract_product_page_for_domain(soup, base_url, _domain(base_url)) or _extract_generic_product(soup, base_url, _domain(base_url))
        status = "success" if product else "empty"
        message = None if product else message
    return ProductDetailResult(product, status, fetched.html, base_url, message, diagnostics)


async def _scrape_search_page(url: str, *, task_id: str = "") -> list[ScrapedProduct]:
    return (await _scrape_search_page_detailed(url, task_id=task_id)).products


async def _scrape_search_page_detailed(url: str, *, task_id: str = "") -> SearchPageResult:
    if not safety_guard.is_safe_url(url):
        return SearchPageResult("failed", [], safety_guard.reason_for_block(url), {"page_classification": "unsafe_url"})
    fetched = await _fetch_page_html(url, task_id=task_id)
    base_diag = {"retrieval_method": fetched.method, "http_status": fetched.http_status, "final_url": fetched.final_url or url}
    if fetched.status != "success":
        base_diag["page_classification"] = "access_challenge" if fetched.status == "blocked" else fetched.status
        logger.info("deals_retrieval_diagnostics %s", base_diag)
        return SearchPageResult(fetched.status, [], fetched.message, base_diag)
    extraction = extract_product_data_detailed(fetched.html, base_url=url)
    diagnostics = {**base_diag, **extraction.diagnostics}
    if not extraction.products and fetched.method == "http" and "amazon" not in _domain(url) and diagnostics.get("page_classification") == "unknown" and diagnostics.get("candidate_product_containers", 0) == 0:
        retry = await _fetch_page_html(url, task_id=task_id, force_browser=True)
        retry_diag = {"retrieval_method": retry.method, "http_status": retry.http_status, "final_url": retry.final_url or url}
        if retry.status != "success":
            retry_diag["page_classification"] = "access_challenge" if retry.status == "blocked" else retry.status
            logger.info("deals_retrieval_diagnostics %s", retry_diag)
            return SearchPageResult(retry.status, [], retry.message, retry_diag)
        extraction = extract_product_data_detailed(retry.html, base_url=url)
        diagnostics = {**retry_diag, **extraction.diagnostics, "retried_after_http_unknown": True}
    if not extraction.products:
        page_class = diagnostics.get("page_classification")
        status = "no_results" if page_class == "empty_results" else "failed"
        message = "No products matched the retailer search page" if status == "no_results" else "Could not verify retailer search page or product extraction"
        logger.info("deals_retrieval_diagnostics %s", diagnostics)
        return SearchPageResult(status, [], message, diagnostics)
    logger.info("deals_retrieval_diagnostics %s", diagnostics)
    return SearchPageResult("success", extraction.products, diagnostics=diagnostics)


async def _fetch_page_html(url: str, *, task_id: str = "", force_browser: bool = False) -> FetchResult:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36", "Accept-Language": "en-IN,en;q=0.9"}
    if not force_browser:
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, headers=headers) as client:
                response = await client.get(url)
                if response.status_code in {401, 403, 429}:
                    return FetchResult("blocked", response.text or "", f"HTTP {response.status_code} access restricted", "http", response.status_code, str(response.url))
                if response.status_code >= 500:
                    return FetchResult("failed", response.text or "", f"HTTP {response.status_code}", "http", response.status_code, str(response.url))
                if response.text and len(response.text) > 1500:
                    text = BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True).lower()
                    if _is_blocked_page(text):
                        return FetchResult("blocked", response.text, "Access restriction or bot challenge detected", "http", response.status_code, str(response.url))
                    return FetchResult("success", response.text, method="http", http_status=response.status_code, final_url=str(response.url))
        except httpx.TimeoutException:
            return FetchResult("timeout", "", "HTTP fetch timed out", "http")
        except Exception as exc:
            logger.debug("deals_scraper: http fetch failed for %s - %s", url, exc)

    try:
        async with BrowserWorker(task_id=task_id) as bw:
            page = bw.page
            page.set_default_navigation_timeout(9000)
            response = await page.goto(url, wait_until="domcontentloaded", timeout=9000)
            await page.wait_for_timeout(700)
            html = await page.content()
            text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True).lower()
            status = response.status if response else None
            final_url = page.url
            if status in {401, 403, 429} or _is_blocked_page(text):
                return FetchResult("blocked", html, f"HTTP {status} access restricted" if status else "Access restriction or bot challenge detected", "playwright", status, final_url)
            return FetchResult("success", html, method="playwright", http_status=status, final_url=final_url)
    except TimeoutError:
        return FetchResult("timeout", "", "Browser navigation timed out", "playwright")
    except Exception as exc:
        return FetchResult("failed", "", str(exc), "playwright")

def extract_product_data(html: str, *, base_url: str = "") -> list[ScrapedProduct]:
    return extract_product_data_detailed(html, base_url=base_url).products


def extract_product_data_detailed(html: str, *, base_url: str = "") -> ExtractionResult:
    soup = BeautifulSoup(html, "html.parser")
    domain = _domain(base_url)
    candidates: list[Any] = []
    for selector in ["[data-component-type='s-search-result']", "div[data-id]", "div.tUxRFH", "div._2kHMtA", "div._4ddWXP", "div._75nlfW", "div._1AtVbE", "a.CGtC98", "div.slAVV4", "li.product-item", "div.product-card", "li[data-pid]"]:
        nodes = soup.select(selector)
        if nodes:
            candidates = nodes
            break
    counts = {"page_classification": _classify_listing_page(soup, len(candidates)), "candidate_product_containers": len(candidates), "valid_titles": 0, "valid_prices": 0, "valid_product_urls": 0, "extracted_products": 0}
    items: list[ScrapedProduct] = []
    for node in candidates:
        text = node.get_text(" ", strip=True)
        if len(text) < 20:
            continue
        name = _guess_name(node, text)
        if name and len(name) >= 5:
            counts["valid_titles"] += 1
        price = _extract_price_from_node(node) or _parse_price(text)
        if price is not None:
            counts["valid_prices"] += 1
        link = _extract_link(node, base_url)
        if is_valid_product_link(link) and not _is_junk_url(link or ""):
            counts["valid_product_urls"] += 1
        if not name or len(name) < 5 or price is None or not is_valid_product_link(link) or _is_junk_url(link or ""):
            continue
        items.append(ScrapedProduct(name[:160], price, _parse_rating(text), link or "", domain, _extract_image_url(node, base_url)))
        if len(items) >= 20:
            break
    counts["extracted_products"] = len(items)
    return ExtractionResult(items, counts)


def _classify_listing_page(soup: BeautifulSoup, candidate_count: int) -> str:
    text = soup.get_text(" ", strip=True).lower()
    if _is_blocked_page(text):
        return "access_challenge"
    if candidate_count > 0:
        return "product_listing"
    if any(marker in text for marker in ["no results", "did not match any products", "try checking your spelling"]):
        return "empty_results"
    return "unknown"


def _extract_product_page_for_domain(soup: BeautifulSoup, url: str, domain: str) -> ScrapedProduct | None:
    if "amazon" in domain:
        return _extract_product_page(soup, url, domain, ["#productTitle", "span#productTitle"], ["span.a-price .a-offscreen", "span.a-price-whole", "#priceblock_ourprice", "#priceblock_dealprice"])
    if "flipkart" in domain:
        return _extract_product_page(soup, url, domain, ["span.VU-ZEz", "span.B_NuCI", "h1 span", "h1"], ["div.Nx9bqj", "div._30jeq3"])
    if "croma" in domain:
        return _extract_product_page(soup, url, domain, ["h1.pd-title", "h1"], ["span.amount", "span.new-price", "span.pdp-price"])
    return None


def _extract_product_page(soup: BeautifulSoup, url: str, domain: str, title_selectors: list[str], price_selectors: list[str]) -> ScrapedProduct | None:
    title = _first_text(soup, title_selectors)
    price = _first_price(soup, price_selectors)
    if not title or not price:
        return None
    return ScrapedProduct(title[:200], price, _parse_rating(soup.get_text(" ", strip=True)), url, domain, _extract_image_url(soup, url), _extract_attributes(soup))


def _extract_generic_product(soup: BeautifulSoup, url: str, domain: str) -> ScrapedProduct | None:
    title = _first_text(soup, ["h1", "[itemprop='name']", "meta[property='og:title']"])
    price = _parse_price(soup.get_text(" ", strip=True))
    if not title or not price:
        return None
    return ScrapedProduct(re.sub(r"\s*[-|:]\s*(Amazon|Flipkart|Croma|Buy Online).*$", "", title, flags=re.I)[:200], price, _parse_rating(soup.get_text(" ", strip=True)), url, domain, _extract_image_url(soup, url), _extract_attributes(soup))


_ACCESSORY_WORDS = ["cover", "case", "tempered", "protector", "charger", "cable", "adapter", "earbuds", "headphones", "skin", "sleeve", "stand", "holder", "mouse pad", "cleaning kit"]
_QUERY_NOISE = {"under", "below", "above", "rs", "inr", "best", "cheapest", "deal", "deals", "buy", "online", "india", "price", "prices", "compare", "provide", "purchase", "links", "from", "for", "in", "the", "a", "an", "of", "with", "to", "and", "or"}


def is_primary_product(query: str, title: str) -> bool:
    """Decide whether ``title`` is the primary product the query asked for.

    When the query names a definite primary category (smartphone, laptop,
    garment, ...) the reusable taxonomy gate decides, so accessories and
    unrelated listings cannot pass on a shared keyword. Queries without a
    definite category fall back to the legacy token-overlap heuristic.
    """

    from .category_taxonomy import ProductCategory, category_for_query, evaluate_category

    requested = category_for_query(query)
    if requested is not ProductCategory.UNKNOWN:
        return evaluate_category(requested, title).matched
    title_low = title.lower()
    if any(word in title_low for word in _ACCESSORY_WORDS):
        return False
    core = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in _QUERY_NOISE and len(w) > 1]
    if not core:
        return True
    if "gaming" in core and "laptop" in core and _has_gaming_laptop_signal(title_low):
        return True
    return any(k in title_low for k in core)


def _has_gaming_laptop_signal(title: str) -> bool:
    dedicated_gpu = r"\b(?:rtx|gtx|geforce|radeon\s+rx|rx\s*\d{3,4})\b"
    gaming_series = r"\b(?:victus|loq|legion|alienware|rog|tuf|nitro|omen|predator|ideapad\s+gaming|msi\s+thin|katana|cyborg|pulse)\b"
    return bool(re.search(dedicated_gpu, title, flags=re.I) or re.search(gaming_series, title, flags=re.I))


def is_valid_product_link(url: str | None) -> bool:
    if not url:
        return False
    low = url.lower()
    if low.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg")):
        return False
    if any(s in low for s in ["/blog/", "/article/", "/review/", "/news/", "captcha", "/login", "/search", "/s?"]):
        return False
    parsed = urlparse(url)
    path = parsed.path.rstrip("/").lower()
    if path in ("", "/", "/gp", "/s", "/searchb"):
        return False
    if "amazon" in parsed.netloc and ("/dp/" in path or "/gp/product/" in path):
        return True
    if "flipkart" in parsed.netloc and ("/p/" in path or "/p/itm" in path):
        return True
    if "croma" in parsed.netloc and ("/p/" in path or re.search(r"/p-\d+", path)):
        return True
    return bool(re.search(r"/(product|item|p|dp|buy)/", path))


def build_related_query(product_name: str) -> str:
    return " ".join([w for w in product_name.split() if len(w) > 2][:6])


def filter_products(products: list[ScrapedProduct], query: str) -> list[ScrapedProduct]:
    return [p for p in products if is_primary_product(query, p.name) and is_valid_product_link(p.link) and p.price > 0]


def _extract_price_from_node(node: Any) -> float | None:
    for selector in [".a-price .a-offscreen", ".a-price-whole", ".Nx9bqj", "._30jeq3", ".amount", ".new-price"]:
        el = node.select_one(selector)
        if el:
            price = _clean_price(el.get_text(" ", strip=True))
            if price:
                return price
    return None


def _clean_price(raw: str) -> float | None:
    raw = raw.replace("₹", "").replace("â‚¹", "").replace("Ã¢â€šÂ¹", "").replace(",", "").replace(" ", "").strip().rstrip(".")
    m = re.search(r"\d+(?:\.\d+)?", raw)
    if not m:
        return None
    value = float(m.group(0))
    return value if value > 0 else None


def _parse_price(text: str) -> float | None:
    for raw in re.findall(r"(?:₹|â‚¹|Ã¢â€šÂ¹|rs\.?|inr)\s*([\d,]{3,})", text, flags=re.I):
        value = float(raw.replace(",", ""))
        if value > 0:
            return value
    return None


def _parse_rating(text: str) -> float | None:
    m = re.search(r"(\d(?:\.\d)?)\s*(?:out of 5|/5|stars?)", text, flags=re.I)
    if not m:
        return None
    value = float(m.group(1))
    return value if 0 < value <= 5 else None


def _guess_name(node: Any, text: str) -> str:
    for selector in ["h2 a[aria-label]", "h2 a span", "span.a-size-medium.a-color-base.a-text-normal", "h2 span.a-text-normal", "span.a-text-normal", "div.KzDlHZ", "a.wjcEIp", "a.s1Q9rs", "a.IRpwTa", "div.s1Q9rs", "h3", "h4", "a[class*='title']", "span[class*='title']", "a[class*='name']"]:
        el = node.select_one(selector)
        if el:
            value = _clean_title(str(el.get("aria-label") or el.get("title") or el.get_text(" ", strip=True)))
            if 5 < len(value) < 200 and not _is_variant_label(value):
                return value
    m = re.search(r"(?:₹|â‚¹|Ã¢â€šÂ¹|rs\.?|inr)", text, flags=re.I)
    return _clean_title((text[: m.start()] if m else text[:100]).strip())


def _is_variant_label(value: str) -> bool:
    value = value.lower().strip()
    return bool(re.fullmatch(r"(?:\+\d+\s*)?(?:other\s+colo(?:u)?r?s?|more\s+colo(?:u)?r?s?|colo(?:u)?r?s?|size|sizes|options?)", value))


def _extract_attributes(soup: BeautifulSoup) -> dict[str, str]:
    """Extract normalized attributes from structured data, then title hints.

    JSON-LD Product data and specification tables take precedence over the
    low-confidence fit/fabric text fallback kept for garment detail pages.
    """

    from .attribute_extraction import extract_attributes_from_soup

    attrs, _evidence = extract_attributes_from_soup(soup, base_url="")
    text = soup.get_text(" ", strip=True)
    fabric = re.search(r"\b(?:material composition|fabric|material)\b\s*[:\-]?\s*((?:100\s*%|pure)?\s*[a-z][a-z\s-]{2,40})", text, flags=re.I)
    fit = re.search(r"\b(slim fit|regular fit|oversi[sz]ed|tapered fit|straight fit)\b", text, flags=re.I)
    if fabric and "fabric" not in attrs:
        attrs["fabric"] = _clean_title(fabric.group(1)).strip().lower()
    if fit and "fit" not in attrs:
        attrs["fit"] = fit.group(1).strip().lower()
    return attrs


def _clean_title(value: str) -> str:
    value = re.sub(r"^\s*\+\d+\s+", " ", value)
    value = re.sub(r"\bSponsored\b.*?Let us know", " ", value, flags=re.I)
    value = re.sub(r"\b(?:Sponsored|Ad)\b", " ", value, flags=re.I)
    value = re.sub(r"Amazon'?s Choice\s+for\s+\"[^\"]+\"", " ", value, flags=re.I)
    value = re.sub(r"Amazon'?s Choice\s+for\s+[^|,-]+", " ", value, flags=re.I)
    value = re.sub(r"\b(?:Overall Pick|Limited time deal)\b", " ", value, flags=re.I)
    value = re.sub(r"\bAdd\s+to\s+Compare\b", " ", value, flags=re.I)
    value = re.sub(r"\b\d+(?:\.\d)?\s*(?:out of 5|/5|stars?)\b", " ", value, flags=re.I)
    value = re.sub(r"\b\d[\d,]*\s+(?:ratings?|reviews?)\b", " ", value, flags=re.I)
    value = re.sub(r"\b\d+(?:\.\d)?\s*\(\s*\d[\d,]*\s*\)\b", " ", value, flags=re.I)
    value = re.sub(r"\b\d[\d,]*\s*(?:ratings?|reviews?)\s*&\s*\d[\d,]*\s*(?:ratings?|reviews?)\b", " ", value, flags=re.I)
    value = re.sub(r"\b(?:ratings?\s*&\s*reviews?|free delivery|only \d+ left)\b", " ", value, flags=re.I)
    value = re.sub(r"\b(?:add\s+to\s+cart|buy\s+now|view\s+details|compare)\b", " ", value, flags=re.I)
    value = re.sub(r"\b(?:new arrival|bestseller|hot deal|sale|special price|sponsored|visit the store|official store|page \d+ of \d+)\b", " ", value, flags=re.I)
    value = re.sub(r"\b(?:men'?s|women'?s)\s+(?=(?:men'?s|women'?s)\b)", " ", value, flags=re.I)
    value = re.sub(r"\b(\d+\s*gb\s*(?:ram|ddr\d?|ssd)?|(?:rtx|gtx)\s*\d{3,4}|ryzen\s*[3579]\s*\d{3,4}[a-z]*|i[3579]-?\d{4,5}[a-z]*)\b(?:\s+\1\b)+", r"\1", value, flags=re.I)
    value = _dedupe_repeated_title_chunks(value)
    return re.sub(r"\s+", " ", value).strip(" -|\n\t")


def _dedupe_repeated_title_chunks(value: str) -> str:
    words = value.split()
    compact: list[str] = []
    for word in words:
        if len(compact) >= 1 and compact[-1].lower() == word.lower():
            continue
        compact.append(word)
    value = " ".join(compact)
    half = len(value) // 2
    if half > 20 and value[:half].strip().lower() == value[half:].strip().lower():
        return value[:half].strip()
    for size in range(3, min(12, len(words) // 2) + 1):
        out: list[str] = []
        i = 0
        while i < len(words):
            chunk = words[i:i + size]
            next_chunk = words[i + size:i + 2 * size]
            if chunk and [w.lower() for w in chunk] == [w.lower() for w in next_chunk]:
                out.extend(chunk)
                i += 2 * size
            else:
                out.append(words[i])
                i += 1
        words = out
        value = " ".join(words)
    return value


def _extract_link(node: Any, base_url: str) -> str | None:
    anchors = [node] if getattr(node, "name", None) == "a" and node.get("href") else []
    anchors.extend(node.find_all("a", href=True))
    for a in anchors:
        resolved = _resolve_product_href(str(a.get("href") or ""), base_url)
        if resolved and is_valid_product_link(resolved):
            return resolved
    return None


def _extract_image_url(node: Any, base_url: str) -> str | None:
    for img in node.find_all("img"):
        dynamic = img.get("data-a-dynamic-image")
        if dynamic:
            try:
                for candidate in json.loads(dynamic).keys():
                    resolved = _resolve_image_url(str(candidate), base_url)
                    if resolved:
                        return resolved
            except Exception:
                pass
        for attr in ("data-old-hires", "data-a-hires", "data-src", "data-lazy-src", "src"):
            candidate = str(img.get(attr) or "").strip()
            resolved = _resolve_image_url(candidate, base_url)
            if resolved:
                return resolved
        srcset = str(img.get("srcset") or img.get("data-srcset") or "")
        for part in srcset.split(","):
            candidate = part.strip().split(" ")[0]
            resolved = _resolve_image_url(candidate, base_url)
            if resolved:
                return resolved
    return None


def _resolve_image_url(url: str, base_url: str) -> str | None:
    if not url or url.startswith("data:"):
        return None
    if url.startswith("//"):
        url = f"https:{url}"
    elif url.startswith("/"):
        base = urlparse(base_url)
        url = f"{base.scheme}://{base.netloc}{url}"
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    low = parsed.path.lower()
    if not re.search(r"\.(?:jpg|jpeg|png|webp)(?:$|[._-])", low):
        return None
    return url


def _resolve_product_href(href: str, base_url: str) -> str | None:
    if not href:
        return None
    base = urlparse(base_url)
    if href.startswith("/"):
        href = f"{base.scheme}://{base.netloc}{href}"
    parsed = urlparse(href)
    if not parsed.scheme or not parsed.netloc:
        return None
    query = parse_qs(parsed.query)
    for key in ("url", "u"):
        if query.get(key):
            nested = unquote(query[key][0])
            if nested.startswith("/"):
                nested = f"{base.scheme}://{base.netloc}{nested}"
            if is_valid_product_link(nested):
                return nested
    return href


def _first_text(soup: BeautifulSoup, selectors: list[str]) -> str | None:
    for selector in selectors:
        el = soup.select_one(selector)
        if el:
            value = el.get("content", "") if el.name == "meta" else el.get_text(" ", strip=True)
            if value and len(value) > 3:
                return _clean_title(value).strip()
    return None


def _first_price(soup: BeautifulSoup, selectors: list[str]) -> float | None:
    for selector in selectors:
        el = soup.select_one(selector)
        if el:
            price = _clean_price(el.get_text(" ", strip=True))
            if price:
                return price
    return None


def _is_blocked_page(page_text_lower: str) -> bool:
    return any(marker in page_text_lower for marker in _BLOCK_MARKERS)


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


def _is_junk_url(url: str) -> bool:
    low = (url or "").lower()
    return any(j in low for j in [".jpg", ".png", ".gif", ".svg", "/blog/", "/article/", "/review/", "captcha", "/login"])


def _domain_matches_retailer(url_or_domain: str, retailer: str) -> bool:
    expected = RETAILER_DOMAINS.get(retailer)
    parsed = urlparse(url_or_domain)
    domain = (parsed.netloc or url_or_domain).replace("www.", "").lower()
    return bool(expected and domain.endswith(expected))


def _deduplicate(items: list[ScrapedProduct]) -> list[ScrapedProduct]:
    seen: set[str] = set()
    out: list[ScrapedProduct] = []
    for item in items:
        key = item.link.lower().split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
