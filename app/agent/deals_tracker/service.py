from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session
from sqlmodel import select

from .evaluator import evaluate_deal
from .models import DealAlert, DealPriceHistory, DealProduct
from .scraper import RETAILER_DOMAINS, ScrapedProduct, build_related_query, filter_products, is_primary_product, is_valid_product_link, scrape_url, search_products, search_retailer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetailerConfig:
    name: str
    categories: set[str]
    connector: bool
    access_status: str


RETAILER_REGISTRY = {
    "amazon": RetailerConfig("amazon", {"product", "laptop", "electronics"}, True, "available"),
    "flipkart": RetailerConfig("flipkart", {"product", "laptop", "electronics"}, True, "available"),
    "croma": RetailerConfig("croma", {"product", "laptop", "electronics"}, True, "blocked"),
}
_RETAILER_ALIASES = {
    "amazon": "amazon",
    "amazon.in": "amazon",
    "flipkart": "flipkart",
    "flipkart.com": "flipkart",
    "croma": "croma",
    "croma.com": "croma",
    "myntra": "myntra",
    "ajio": "ajio",
    "reliance digital": "reliance_digital",
    "reliance": "reliance_digital",
    "vijay sales": "vijay_sales",
    "tatacliq": "tatacliq",
    "tata cliq": "tatacliq",
}


@dataclass(frozen=True)
class SearchIntent:
    category: str
    keywords: list[str]
    max_budget: float | None
    specifications: list[str]
    retailers: list[str]
    result_count: int
    clothing_type: str | None = None
    gender: str | None = None
    fit: str | None = None
    fabric: str | None = None
    color: str | None = None
    size: str | None = None
    preferred_brands: list[str] | None = None

    @property
    def search_query(self) -> str:
        if self.category == "fashion":
            parts = [self.gender, self.fit, self.fabric, self.color, self.clothing_type, *(self.preferred_brands or []), *self.keywords]
            return _clean_query_parts(parts) or self.clothing_type or "clothing"
        category_part = self.clothing_type if self.category == "fashion" and self.clothing_type else self.category
        parts = [*self.specifications, category_part, *self.keywords]
        return _clean_query_parts(parts) or self.category or "product"


def _clean_query_parts(parts: list[str | None]) -> str:
        seen: set[str] = set()
        concise: list[str] = []
        for part in parts:
            if not part:
                continue
            cleaned = re.sub(r"[^a-zA-Z0-9% -]", " ", part).strip().lower()
            cleaned = re.sub(r"\blaptops\b", "laptop", cleaned)
            cleaned = re.sub(r"\s+", " ", cleaned)
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                concise.append(cleaned)
        return " ".join(concise[:8])


class DealsService:
    def __init__(self, session: Session) -> None:
        self.session = session

    async def search(self, *, query: str | None = None, url: str | None = None) -> dict[str, Any]:
        if url:
            result = await self._url_flow(url)
            return {"status": "success" if result.get("main_product") else "no_results", **result}
        if query:
            return await self._search_flow(query)
        return {"status": "no_results", "mode": "search", "results": [], "source_statuses": []}

    async def stream_search(self, *, query: str | None = None, url: str | None = None) -> AsyncIterator[dict[str, Any]]:
        if url:
            yield {"event": "stage", "stage": "Understanding request", "message": "Analyzing product URL"}
            yield {"event": "final", "payload": await self.search(url=url)}
            return
        if not query:
            yield {"event": "final", "payload": {"status": "no_results", "mode": "search", "results": []}}
            return

        yield {"event": "stage", "stage": "Understanding request"}
        intent = parse_search_intent(query)
        yield {"event": "intent", "intent": _intent_dict(intent)}
        yield {"event": "stage", "stage": "Searching websites"}
        collected = await self._collect_search_results(intent)
        for source in collected["source_statuses"]:
            yield {"event": "source_status", "source": source}
        for product in collected["preliminary_results"]:
            yield {"event": "product", "product": {**product, "preliminary": True}}
        yield {"event": "stage", "stage": "Verifying prices and specifications"}
        final = self._finalize_search_results(intent, collected["products"], collected["source_statuses"])
        yield {"event": "stage", "stage": "Ranking results"}
        yield {"event": "final", "payload": final}

    async def _url_flow(self, url: str) -> dict[str, Any]:
        raw_products = await scrape_url(url)
        if not raw_products:
            return {"mode": "url", "main_product": None, "related": [], "comparison": [], "verdict": None, "results": []}
        main_raw = raw_products[0]
        main_product = self._persist_and_evaluate(main_raw)
        related_raw: list[ScrapedProduct] = []
        try:
            related_raw = await search_products(build_related_query(main_raw.name))
        except Exception as exc:
            logger.warning("Failed to find related products: %s", exc)
        related_filtered = [p for p in filter_products(related_raw, main_raw.name) if not (p.source == main_raw.source and abs(p.price - main_raw.price) < 100)]
        related = [self._persist_and_evaluate(p) for p in related_filtered[:5]]
        all_items = [main_product] + related
        return {"mode": "url", "main_product": main_product, "related": related, "comparison": self._build_comparison(all_items), "verdict": self._build_verdict(main_product, all_items), "results": all_items}

    async def _search_flow(self, query: str) -> dict[str, Any]:
        intent = parse_search_intent(query)
        collected = await self._collect_search_results(intent)
        return self._finalize_search_results(intent, collected["products"], collected["source_statuses"])

    async def _collect_search_results(self, intent: SearchIntent) -> dict[str, Any]:
        semaphore = asyncio.Semaphore(3)
        searchable, skipped_results = _route_retailers(intent)

        async def run(retailer: str):
            async with semaphore:
                return await search_retailer(retailer, intent.search_query, timeout_seconds=12.0)

        source_results = [*skipped_results, *list(await asyncio.gather(*(run(retailer) for retailer in searchable)))]
        if intent.category == "fashion":
            await self._enrich_fashion_candidates(source_results, intent)
        products, source_statuses, preliminary = self._merge_source_results(source_results, intent)
        if len(products) < intent.result_count and intent.max_budget is not None:
            refined_query = _budget_refinement_query(intent)
            if refined_query and refined_query != intent.search_query:
                async def run_refined(retailer: str):
                    async with semaphore:
                        return await search_retailer(retailer, refined_query, timeout_seconds=12.0)
                refined_results = await asyncio.gather(*(run_refined(retailer) for retailer in searchable))
                if intent.category == "fashion":
                    await self._enrich_fashion_candidates(refined_results, intent)
                source_results.extend(refined_results)
                products, source_statuses, preliminary = self._merge_source_results(source_results, intent)
        return {"products": products, "source_statuses": source_statuses, "preliminary_results": preliminary}

    async def _enrich_fashion_candidates(self, source_results: list[Any], intent: SearchIntent) -> None:
        tasks: list[tuple[Any, int, Any]] = []
        for source in source_results:
            for idx, product in enumerate(getattr(source, "products", [])[:6]):
                if _promising_fashion_candidate(product.name, intent) and not _matches_fashion_constraints(product.name, intent):
                    tasks.append((source, idx, product))
        for source, idx, product in tasks[:8]:
            try:
                details = await asyncio.wait_for(scrape_url(product.link), timeout=5.0)
            except Exception:
                continue
            if not details:
                continue
            detail = details[0]
            merged_name = detail.name if len(detail.name) > len(product.name) else f"{product.name} {detail.name}"
            source.products[idx] = ScrapedProduct(merged_name[:220], detail.price or product.price, product.rating or detail.rating, product.link, product.source, product.image_url or detail.image_url)

    def _merge_source_results(self, source_results: list[Any], intent: SearchIntent) -> tuple[list[ScrapedProduct], list[dict[str, Any]], list[dict[str, Any]]]:
        products: list[ScrapedProduct] = []
        by_retailer: dict[str, dict[str, Any]] = {}
        preliminary: list[dict[str, Any]] = []
        preliminary_seen: set[str] = set()
        for source in source_results:
            verified, filter_diag = self._filter_verified_with_diagnostics(source.products, intent, source.retailer)
            products.extend(verified)
            status = "success" if verified else ("no_results" if source.status == "success" else source.status)
            diagnostics = {**(source.diagnostics or {}), **filter_diag, "final_verified_products": len(verified)}
            existing = by_retailer.get(source.retailer)
            if not existing or (status == "success" and existing["status"] != "success"):
                by_retailer[source.retailer] = {"retailer": source.retailer, "status": status, "count": len(verified), "message": source.message if not verified else None, "diagnostics": diagnostics}
            elif existing:
                existing["count"] += len(verified)
                existing["diagnostics"] = {**existing.get("diagnostics", {}), **diagnostics, "final_verified_products": existing["count"]}
            for product in verified:
                stable_id = _stable_product_id(product)
                if stable_id in preliminary_seen:
                    continue
                preliminary_seen.add(stable_id)
                preliminary.append(self._to_response_dict(product, intent, preliminary=True))
                if len(preliminary) >= intent.result_count:
                    break
        return products, list(by_retailer.values()), preliminary[: intent.result_count]

    def _finalize_search_results(self, intent: SearchIntent, products: list[ScrapedProduct], source_statuses: list[dict[str, Any]]) -> dict[str, Any]:
        selected = self._rank_products(products, intent)[: intent.result_count]
        results = [self._persist_and_evaluate(p, intent) for p in selected]
        explanation = None if len(results) >= intent.result_count else _search_explanation(intent, results, source_statuses)
        return {"status": _overall_status(results, source_statuses), "mode": "search", "intent": _intent_dict(intent), "results": results, "source_statuses": source_statuses, "explanation": explanation}

    def _filter_verified(self, products: list[ScrapedProduct], intent: SearchIntent, retailer: str) -> list[ScrapedProduct]:
        return self._filter_verified_with_diagnostics(products, intent, retailer)[0]

    def _filter_verified_with_diagnostics(self, products: list[ScrapedProduct], intent: SearchIntent, retailer: str) -> tuple[list[ScrapedProduct], dict[str, Any]]:
        domain = RETAILER_DOMAINS.get(retailer, retailer)
        verified = []
        diagnostics: dict[str, Any] = {
            "rejected_by_category_or_spec": 0,
            "rejected_by_budget": 0,
            "rejected_by_domain_or_url": 0,
            "fashion_rejection_reasons": {},
            "rejection_samples": [],
        }
        query = " ".join([intent.category, *intent.keywords, *intent.specifications])
        for product in products:
            if not product.name or not is_valid_product_link(product.link) or product.price <= 0 or domain not in product.link.lower() or domain not in product.source.lower():
                diagnostics["rejected_by_domain_or_url"] += 1
                _add_rejection_sample(diagnostics, product, "domain_or_url")
                continue
            if not is_primary_product(query, product.name) or not _matches_category(product.name, intent.category) or (intent.category != "fashion" and not _matches_required_specs(product.name, intent.specifications)):
                diagnostics["rejected_by_category_or_spec"] += 1
                _add_rejection_sample(diagnostics, product, "category_or_spec")
                continue
            fashion_reason = _fashion_rejection_reason(product.name, intent) if intent.category == "fashion" else None
            if fashion_reason:
                diagnostics["rejected_by_category_or_spec"] += 1
                diagnostics["fashion_rejection_reasons"][fashion_reason] = diagnostics["fashion_rejection_reasons"].get(fashion_reason, 0) + 1
                _add_rejection_sample(diagnostics, product, fashion_reason)
                continue
            if intent.max_budget is not None and product.price > intent.max_budget:
                diagnostics["rejected_by_budget"] += 1
                _add_rejection_sample(diagnostics, product, "budget")
                continue
            verified.append(product)
        logger.info("deals_filter_diagnostics retailer=%s %s", retailer, diagnostics)
        return verified, diagnostics

    def _rank_products(self, products: list[ScrapedProduct], intent: SearchIntent) -> list[ScrapedProduct]:
        deduped: dict[str, ScrapedProduct] = {}
        for product in products:
            key = product.link.lower().split("?")[0]
            if key not in deduped or product.price < deduped[key].price:
                deduped[key] = product
        query_terms = set(re.findall(r"[a-z0-9]+", " ".join([intent.category, *intent.keywords, *intent.specifications]).lower()))
        return sorted(deduped.values(), key=lambda p: (_brand_rank(p.name, intent), -len(query_terms & set(re.findall(r"[a-z0-9]+", p.name.lower()))), p.price, p.name.lower()))

    def _to_response_dict(self, p: ScrapedProduct, intent: SearchIntent | None = None, *, preliminary: bool = False) -> dict[str, Any]:
        return {"name": p.name, "price": p.price, "rating": p.rating, "link": p.link, "source": p.source, "image_url": p.image_url, "deal_status": "AVERAGE", "match_label": _match_label(p.name, intent), "retailer_product_id": _stable_product_id(p), "comparison_key": _comparison_key(p.name)}

    def _build_comparison(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, dict[str, Any]]] = {}
        for item in items:
            source, price = item.get("source", "Unknown"), item.get("price")
            key = item.get("comparison_key")
            if not key:
                continue
            sources = groups.setdefault(key, {})
            if price is not None and (source not in sources or price < sources[source]["price"]):
                sources[source] = {"source": source, "price": price, "link": item.get("link"), "name": item.get("name"), "comparison_key": key}
        comparable = [sources for sources in groups.values() if len(sources) > 1]
        if not comparable:
            return []
        sources = max(comparable, key=len)
        comparison = sorted(sources.values(), key=lambda x: x["price"])
        if comparison:
            comparison[0]["is_best"] = True
        return comparison

    def _build_verdict(self, main_product: dict[str, Any], all_items: list[dict[str, Any]]) -> dict[str, Any]:
        if not all_items:
            return {"status": "UNKNOWN", "message": "No data available for comparison"}
        best = min(all_items, key=lambda x: x.get("price", float("inf")))
        if best.get("link") == main_product.get("link"):
            return {"status": "BEST_DEAL", "message": "This is the best verified price found.", "best_price": main_product.get("price"), "best_source": main_product.get("source")}
        return {"status": "BETTER_AVAILABLE", "message": f"Better verified price available on {best.get('source')}", "best_price": best.get("price"), "best_source": best.get("source"), "best_link": best.get("link"), "savings": main_product.get("price", 0) - best.get("price", 0)}

    def _persist_and_evaluate(self, p: ScrapedProduct, intent: SearchIntent | None = None) -> dict[str, Any]:
        product = self._get_or_create_product(p.name, p.link)
        self._record_price(product.id, p.price, p.source, p.link, p.rating)
        history = self._price_list(product.id)
        deal_status = evaluate_deal(p.price, history[:-1]) if len(history) > 1 else "AVERAGE"
        return {"product_id": product.id, "name": p.name, "price": p.price, "rating": p.rating, "link": p.link, "source": p.source, "image_url": p.image_url, "deal_status": deal_status, "match_label": _match_label(p.name, intent), "retailer_product_id": _stable_product_id(p), "comparison_key": _comparison_key(p.name)}

    def get_history(self, product_id: str) -> list[dict[str, Any]]:
        stmt = select(DealPriceHistory).where(DealPriceHistory.product_id == product_id).order_by(DealPriceHistory.checked_at.desc()).limit(100)
        return [{"price": r.price, "source": r.source, "link": r.link, "rating": r.rating, "checked_at": r.checked_at.isoformat() if r.checked_at else None} for r in self.session.execute(stmt).scalars().all()]

    def set_alert(self, product_id: str, target_price: float) -> dict[str, Any]:
        alert = DealAlert(product_id=product_id, target_price=target_price)
        self.session.add(alert)
        self.session.commit()
        return {"alert_id": alert.id, "product_id": product_id, "target_price": target_price}

    def check_alerts(self) -> list[dict[str, Any]]:
        stmt = select(DealAlert).where(DealAlert.triggered == False)
        alerts = self.session.execute(stmt).scalars().all()
        triggered = []
        for alert in alerts:
            prices = self._price_list(alert.product_id)
            if prices and min(prices) <= alert.target_price:
                alert.triggered = True
                self.session.add(alert)
                triggered.append({"alert_id": alert.id, "product_id": alert.product_id, "target_price": alert.target_price})
        if triggered:
            self.session.commit()
        return triggered

    def _get_or_create_product(self, name: str, url: str | None) -> DealProduct:
        stmt = select(DealProduct).where(DealProduct.name == name).limit(1)
        existing = self.session.execute(stmt).scalars().first()
        if existing:
            return existing
        product = DealProduct(name=name, canonical_url=url)
        self.session.add(product)
        self.session.commit()
        self.session.refresh(product)
        return product

    def _record_price(self, product_id: str, price: float, source: str, link: str | None, rating: float | None) -> None:
        self.session.add(DealPriceHistory(product_id=product_id, price=price, source=source, link=link, rating=rating))
        self.session.commit()

    def _price_list(self, product_id: str) -> list[float]:
        stmt = select(DealPriceHistory).where(DealPriceHistory.product_id == product_id)
        return [r.price for r in self.session.execute(stmt).scalars().all()]


def parse_search_intent(query: str) -> SearchIntent:
    text = query.lower()
    budget = _parse_budget(text)
    count_match = re.search(r"(?:best|top|find|show|get)\s+(\d{1,2})\b", text)
    result_count = min(max(int(count_match.group(1)), 1), 10) if count_match else 3
    clothing_type = _extract_clothing_type(text)
    category = "laptop" if re.search(r"\blaptops?\b", text) else ("fashion" if clothing_type else "product")
    retailers = _select_retailers(text, category)
    specs = ["gaming"] if "gaming" in text and category != "fashion" else []
    fashion_specs = _extract_fashion_specs(text) if category == "fashion" else {}
    if category == "fashion":
        specs.extend(v for v in [fashion_specs.get("fit"), fashion_specs.get("fabric"), fashion_specs.get("color"), fashion_specs.get("gender"), fashion_specs.get("size")] if v)
    for match in re.finditer(r"\b(\d{1,2}\s*gb|rtx\s*\d{3,4}|gtx\s*\d{3,4}|i[3579]|ryzen\s*\d)\b", text):
        specs.append(match.group(1).replace(" ", ""))
    cleaned = _strip_budget_phrases(text)
    cleaned = re.sub(r"\b(?:search\s+across|available\s+shopping\s+platforms?|shopping\s+platforms?|their\s+specifications?|their\s+ratings?|ratings?\s+recommend(?:ed|ations?)?|price\s+comparison|direct\s+comparison|best\s+deals?|no\s+products?\s+found|found\s+only|found\s+\d+|within\s+budget)\b", " ", cleaned)
    cleaned = re.sub(r"\b(?:amazon|flipkart|croma|myntra|ajio|reliance|digital|vijay|sales|tatacliq|tata|cliq|find|found|within|best|top|show|get|search|across|available|shopping|platform|platforms|site|sites|store|stores|retailer|retailers|website|websites|marketplace|marketplaces|from|compare|comparison|prices|price|deal|deals|offer|offers|relevant|provide|purchase|links|list|include|suggest|recommend|recommended|recommendations|their|spec|specs|specification|specifications|rating|ratings|review|reviews|result|results|candidate|candidates|verified|matching|match|matches|product|products|option|options|please|under|below|less|than|rs|inr|lakh|lakhs|and|or|the|a|an|of|with|for|to|me|my)\b", " ", cleaned)
    skip = {category, f"{category}s", clothing_type or "", f"{clothing_type}s" if clothing_type else "", *[s.lower() for s in specs]}
    for spec in specs:
        skip.update(re.findall(r"[a-z0-9]+", spec.lower()))
    keywords = [w for w in re.findall(r"[a-z0-9]+", cleaned) if len(w) > 2 and w not in skip and not w.isdigit()]
    preferred_brands = _extract_preferred_brands(text)
    return SearchIntent(category, list(dict.fromkeys(keywords))[:6], budget, list(dict.fromkeys(specs)), retailers, result_count, clothing_type, fashion_specs.get("gender"), fashion_specs.get("fit"), fashion_specs.get("fabric"), fashion_specs.get("color"), fashion_specs.get("size"), preferred_brands)


def _select_retailers(text: str, category: str) -> list[str]:
    matches: list[tuple[int, str]] = []
    for alias, retailer in _RETAILER_ALIASES.items():
        match = re.search(rf"\b{re.escape(alias)}\b", text)
        if match:
            matches.append((match.start(), retailer))
    explicit: list[str] = []
    for _, retailer in sorted(matches):
        if retailer not in explicit:
            explicit.append(retailer)
    if explicit:
        return explicit
    routed = [
        name for name, config in RETAILER_REGISTRY.items()
        if config.connector and config.access_status == "available" and (category in config.categories or "product" in config.categories)
    ]
    return routed or [name for name, config in RETAILER_REGISTRY.items() if config.connector and config.access_status == "available"]


def _route_retailers(intent: SearchIntent) -> tuple[list[str], list[Any]]:
    searchable: list[str] = []
    skipped: list[Any] = []
    for retailer in intent.retailers:
        config = RETAILER_REGISTRY.get(retailer)
        if not config:
            skipped.append(_source_result(retailer, "unsupported", "Retailer is not integrated", {"connector_available": False}))
        elif not config.connector:
            skipped.append(_source_result(retailer, "unsupported", "Retailer connector is not implemented", {"connector_available": False}))
        elif config.access_status != "available":
            skipped.append(_source_result(retailer, config.access_status, "Retailer access is blocked or unavailable", {"connector_available": True, "access_status": config.access_status}))
        elif intent.category not in config.categories and "product" not in config.categories:
            skipped.append(_source_result(retailer, "unsupported", "Retailer does not support this category", {"connector_available": True, "supported_categories": sorted(config.categories)}))
        else:
            searchable.append(retailer)
    return searchable, skipped


def _source_result(retailer: str, status: str, message: str, diagnostics: dict[str, Any]) -> Any:
    class Result:
        pass
    result = Result()
    result.retailer = retailer
    result.status = status
    result.products = []
    result.message = message
    result.diagnostics = diagnostics
    return result


_GARMENT_TYPES = {
    "t-shirt": ["t shirt", "t-shirt", "tee"],
    "kurta set": ["kurta set", "kurta pant", "kurta dupatta", "kurti set"],
    "kurta": ["kurta", "kurti"],
    "jeans": ["jeans", "denim"],
    "shirt": ["shirt"],
    "dress": ["dress"],
}
_FASHION_BRANDS = ["levis", "levi's", "wrangler", "pepe", "spykar", "killer", "lee", "allen solly", "van heusen", "w", "biba", "aurelia", "libas", "max", "roadster", "hrx", "puma", "adidas", "nike"]


def _extract_clothing_type(text: str) -> str | None:
    for garment, aliases in _GARMENT_TYPES.items():
        if any(re.search(rf"\b{re.escape(alias)}s?\b", text) for alias in aliases):
            return garment
    return None


def _extract_fashion_specs(text: str) -> dict[str, str | None]:
    return {
        "gender": _first_match(text, [("women", r"\b(women|woman|female|ladies|girls)\b"), ("men", r"\b(men|man|male|mens|boys)\b")]),
        "fit": _first_match(text, [("oversized", r"\boversi[sz]ed\b"), ("slim", r"\bslim\s*fit\b|\bslim\b"), ("regular", r"\bregular\s*fit\b"), ("tapered", r"\btapered\s*fit\b|\btapered\b"), ("straight", r"\bstraight\s*fit\b")]),
        "fabric": _first_match(text, [("100% cotton", r"\b(?:100\s*%|pure)\s+cotton\b"), ("cotton", r"\bcotton\b"), ("linen", r"\blinen\b"), ("denim", r"\bdenim\b"), ("polyester", r"\bpolyester\b"), ("poly-cotton", r"\bpoly\s*cotton\b|\bpolycotton\b")]),
        "color": _first_match(text, [(c, rf"\b{c}\b") for c in ["blue", "black", "white", "green", "red", "pink", "yellow", "grey", "gray", "navy", "beige", "brown"]]),
        "size": _first_match(text, [("xs", r"\bxs\b"), ("s", r"\bsmall\b|\bsize\s*s\b"), ("m", r"\bmedium\b|\bsize\s*m\b"), ("l", r"\blarge\b|\bsize\s*l\b"), ("xl", r"\bxl\b"), ("xxl", r"\bxxl\b")]),
    }


def _first_match(text: str, patterns: list[tuple[str, str]]) -> str | None:
    for value, pattern in patterns:
        if re.search(pattern, text, flags=re.I):
            return value
    return None


def _extract_preferred_brands(text: str) -> list[str]:
    return list(dict.fromkeys(brand for brand in _FASHION_BRANDS if re.search(rf"\b{re.escape(brand)}\b", text, flags=re.I)))


def _parse_budget(text: str) -> float | None:
    normalized = _normalize_currency_text(text.lower().replace("rs.", "rs"))
    lakh_match = re.search(r"(?:under|below|less than)\s*(?:rs\s*|inr\s*|\u20b9\s*)?(\d+(?:\.\d+)?)\s*lakhs?\b", normalized, flags=re.I)
    if lakh_match:
        return float(lakh_match.group(1)) * 100000
    money_match = re.search(r"(?:under|below|less than)\s*(?:rs\s*|inr\s*|\u20b9\s*)?([\d,]+)", normalized, flags=re.I)
    if money_match:
        return float(money_match.group(1).replace(",", ""))
    return None


def _strip_budget_phrases(text: str) -> str:
    text = _normalize_currency_text(text)
    text = re.sub(r"(?:under|below|less than)\s*(?:rs\.?\s*|inr\s*|\u20b9\s*)?\d+(?:\.\d+)?\s*lakhs?\b", " ", text, flags=re.I)
    text = re.sub(r"(?:under|below|less than)\s*(?:rs\.?\s*|inr\s*|\u20b9\s*)?[\d,]+", " ", text, flags=re.I)
    return text


def _normalize_currency_text(text: str) -> str:
    return text.replace("â‚¹", "\u20b9").replace("Ã¢â€šÂ¹", "\u20b9")


def _stable_product_id(product: ScrapedProduct) -> str:
    link_key = (product.link or "").lower().split("?")[0].rstrip("/")
    fallback = re.sub(r"[^a-z0-9]+", "-", product.name.lower()).strip("-")
    return f"{product.source.lower()}::{link_key or fallback}"


def _comparison_key(name: str) -> str | None:
    text = name.lower()
    model_tokens = re.findall(r"\b(?:[a-z]{1,4}\d{2,4}[a-z0-9-]*|[a-z0-9]{2,}-[a-z0-9]{2,})\b", text)
    model_tokens = [m for m in model_tokens if any(ch.isalpha() for ch in m) and any(ch.isdigit() for ch in m)]
    model = max(model_tokens, key=len) if model_tokens else None
    cpu = re.search(r"\b(?:i[3579]-?\d{4,5}[a-z]*|ryzen\s*[3579]\s*\d{3,4}[a-z]*)\b", text)
    gpu = re.search(r"\b(?:rtx\s*\d{3,4}|gtx\s*\d{3,4}|radeon\s+rx\s*\d{3,4}|rx\s*\d{3,4})\b", text)
    ram = re.search(r"\b(?:8|12|16|24|32|64)\s*gb\s*(?:ram|ddr\d?)?\b", text)
    ssd = re.search(r"\b(?:256|512)\s*gb\s*(?:ssd)?\b|\b(?:1|2)\s*tb\s*(?:ssd)?\b", text)
    if not model or not (cpu or gpu) or not ram or not ssd:
        return None
    parts = [model, cpu.group(0) if cpu else "", gpu.group(0) if gpu else "", ram.group(0), ssd.group(0)]
    return "|".join(re.sub(r"\s+", "", part) for part in parts if part)


def _add_rejection_sample(diagnostics: dict[str, Any], product: ScrapedProduct, reason: str) -> None:
    samples = diagnostics.setdefault("rejection_samples", [])
    if len(samples) >= 3:
        return
    samples.append({"name": product.name[:120], "price": product.price, "reason": reason})


def _budget_refinement_query(intent: SearchIntent) -> str:
    if intent.category == "product" or intent.max_budget is None:
        return ""
    if intent.category == "fashion":
        parts = [intent.gender, intent.fit, intent.fabric, intent.color, intent.clothing_type, "under", str(int(intent.max_budget))]
        return _clean_query_parts(parts)
    parts = [*intent.specifications, intent.category, "under", str(int(intent.max_budget))]
    seen = []
    for part in parts:
        cleaned = re.sub(r"[^a-zA-Z0-9 ]", " ", part).strip().lower()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return " ".join(seen)

def _matches_category(title: str, category: str) -> bool:
    if category == "product":
        return True
    title_text = title.lower()
    if category.lower() in title_text:
        return True
    if category == "laptop":
        laptop_hardware = any(signal in title_text for signal in [
            "ram", "ddr", "ssd", "windows", "win 11", "15.6", "14\"", "16\"",
            "144hz", "fhd", "wuxga", "nvidia", "geforce", "ryzen", "intel", "core i",
        ])
        return laptop_hardware and _has_gaming_laptop_signal(title_text)
    if category == "fashion":
        return not _is_raw_fabric_or_material(title_text)
    return False

def _matches_required_specs(title: str, specifications: list[str]) -> bool:
    title_text = title.lower()
    title_low = title_text.replace(" ", "")
    required = [spec.lower().replace(" ", "") for spec in specifications if spec.lower() != "gaming"]
    if "gaming" in [s.lower() for s in specifications] and not _has_gaming_laptop_signal(title_text):
        return False
    return all(spec in title_low for spec in required)


def _matches_fashion_constraints(title: str, intent: SearchIntent) -> bool:
    return _fashion_rejection_reason(title, intent) is None


def _fashion_rejection_reason(title: str, intent: SearchIntent) -> str | None:
    text = title.lower()
    if _is_raw_fabric_or_material(text):
        return "confirmed_mismatch:raw_fabric"
    if intent.clothing_type and not _matches_garment_type(text, intent.clothing_type):
        return f"confirmed_mismatch:garment:{intent.clothing_type}"
    if intent.fit and not _matches_fit(text, intent.fit):
        return _fashion_attr_reason(text, "fit", intent.fit)
    if intent.fabric and not _matches_fabric(text, intent.fabric):
        return _fashion_attr_reason(text, "fabric", intent.fabric)
    if intent.color and intent.color not in {"gray", "grey"} and not re.search(rf"\b{re.escape(intent.color)}\b", text):
        return f"unverified:color:{intent.color}"
    if intent.color in {"gray", "grey"} and not re.search(r"\bgr[ae]y\b", text):
        return "unverified:color:grey"
    if intent.gender and not _matches_gender(text, intent.gender):
        return f"confirmed_mismatch:gender:{intent.gender}"
    return None


def _fashion_attr_reason(text: str, attr: str, requested: str) -> str:
    if attr == "fit" and re.search(r"\b(slim|regular|tapered|straight|oversi[sz]ed)\b", text):
        return f"confirmed_mismatch:{attr}:{requested}"
    if attr == "fabric" and re.search(r"\b(cotton|polyester|poly\s*cotton|polycotton|viscose|rayon|nylon|linen|denim|blend)\b", text):
        return f"confirmed_mismatch:{attr}:{requested}"
    return f"unverified:{attr}:{requested}"


def _promising_fashion_candidate(title: str, intent: SearchIntent) -> bool:
    text = title.lower()
    if _is_raw_fabric_or_material(text):
        return False
    return not intent.clothing_type or _matches_garment_type(text, intent.clothing_type)


def _matches_garment_type(text: str, garment: str) -> bool:
    aliases = _GARMENT_TYPES.get(garment, [garment])
    if not any(re.search(rf"\b{re.escape(alias)}s?\b", text) for alias in aliases):
        return False
    wrong = {
        "t-shirt": ["fabric", "cloth", "material", "kurta", "jeans", "shirt fabric"],
        "kurta set": ["fabric", "cloth", "material", "unstitched", "jeans", "t-shirt", "t shirt"],
        "jeans": ["fabric", "cloth", "material", "jeggings", "trouser", "kurta", "t-shirt"],
    }
    return not any(word in text for word in wrong.get(garment, []))


def _matches_fit(text: str, requested: str) -> bool:
    if requested == "slim":
        return re.search(r"\bslim\s*fit\b|\bslim\b", text) is not None and "tapered" not in text and "regular fit" not in text
    if requested == "oversized":
        return re.search(r"\boversi[sz]ed\b", text) is not None
    if requested == "tapered":
        return "tapered" in text
    if requested == "regular":
        return "regular" in text
    if requested == "straight":
        return "straight" in text
    return True


def _matches_fabric(text: str, requested: str) -> bool:
    if requested == "100% cotton":
        if re.search(r"\b(poly\s*cotton|polycotton|polyester|viscose|rayon|nylon|blend|cotton blend)\b", text):
            return False
        return re.search(r"\b(?:100\s*%|pure)\s+cotton\b", text) is not None
    if requested == "cotton":
        if re.search(r"\b(poly\s*cotton|polycotton|polyester|viscose|rayon|nylon|blend)\b", text):
            return False
        return re.search(r"\bcotton\b", text) is not None
    if requested == "poly-cotton":
        return re.search(r"\bpoly\s*cotton\b|\bpolycotton\b", text) is not None
    return re.search(rf"\b{re.escape(requested)}\b", text) is not None


def _matches_gender(text: str, requested: str) -> bool:
    if requested == "men":
        return not re.search(r"\b(women|woman|female|ladies|girls)\b", text)
    if requested == "women":
        return not re.search(r"\b(men|man|male|mens|boys)\b", text)
    return True


def _is_raw_fabric_or_material(text: str) -> bool:
    return bool(re.search(r"\b(unstitched|fabric|cloth material|dress material|material only|by meter|per metre|per meter|suiting|shirting fabric)\b", text))


def _brand_rank(title: str, intent: SearchIntent) -> int:
    brands = intent.preferred_brands or []
    if not brands:
        return 0
    return 0 if any(re.search(rf"\b{re.escape(brand)}\b", title, flags=re.I) for brand in brands) else 1


def _match_label(title: str, intent: SearchIntent | None) -> str | None:
    if not intent or not intent.preferred_brands:
        return None
    return "preferred_brand" if _brand_rank(title, intent) == 0 else "alternative"


def _has_gaming_laptop_signal(title: str) -> bool:
    dedicated_gpu = r"\b(?:rtx|gtx|geforce|radeon\s+rx|rx\s*\d{3,4})\b"
    gaming_series = r"\b(?:victus|loq|legion|alienware|rog|tuf|nitro|omen|predator|ideapad\s+gaming|msi\s+thin|katana|cyborg|pulse)\b"
    return bool(re.search(dedicated_gpu, title, flags=re.I) or re.search(gaming_series, title, flags=re.I))


def _overall_status(results: list[dict[str, Any]], source_statuses: list[dict[str, Any]]) -> str:
    if results:
        return "success" if all(s.get("status") == "success" for s in source_statuses) else "partial_success"
    if source_statuses and all(s.get("status") == "no_results" for s in source_statuses):
        return "no_results"
    return "failed"


def _search_explanation(intent: SearchIntent, results: list[dict[str, Any]], source_statuses: list[dict[str, Any]]) -> str:
    if intent.category != "fashion":
        return f"Found only {len(results)} verified matching products across requested retailers."
    reasons: dict[str, int] = {}
    for source in source_statuses:
        for reason, count in ((source.get("diagnostics") or {}).get("fashion_rejection_reasons") or {}).items():
            reasons[reason] = reasons.get(reason, 0) + count
    if not results and reasons:
        unknown = sorted(k for k in reasons if k.startswith("unverified:"))
        mismatch = sorted(k for k in reasons if k.startswith("confirmed_mismatch:"))
        parts = []
        if unknown:
            parts.append("unverified attributes: " + ", ".join(r.split(":", 1)[1] for r in unknown[:4]))
        if mismatch:
            parts.append("confirmed mismatches: " + ", ".join(r.split(":", 1)[1] for r in mismatch[:4]))
        return "No products could be confirmed against all requested fashion attributes (" + "; ".join(parts) + ")."
    return f"Found only {len(results)} confirmed fashion products matching the requested garment, fit, fabric, and budget."


def _intent_dict(intent: SearchIntent) -> dict[str, Any]:
    return {"category": intent.category, "keywords": intent.keywords, "max_budget": intent.max_budget, "specifications": intent.specifications, "clothing_type": intent.clothing_type, "gender": intent.gender, "fit": intent.fit, "fabric": intent.fabric, "color": intent.color, "size": intent.size, "preferred_brands": intent.preferred_brands or [], "requested_websites": intent.retailers, "requested_result_count": intent.result_count, "search_query": intent.search_query}


def sse_payload(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


