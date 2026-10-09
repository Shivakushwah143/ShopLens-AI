# ShopLens AI — Product Search Pipeline Technical Design v1.0
**Status:** Design only; implementation not authorized  
**Baseline:** User-uploaded `ShopLens-AI-main.zip` (GitHub snapshot, before latest unverified Codex changes)  
**Goal:** Find relevant products, verify requested attributes from evidence, and distinguish exact matches from possible matches without regressing laptops, basic clothing, streaming, or price tracking.

## 1. Current code and gaps

| Existing code | Responsibility | Main gap to resolve |
|---|---|---|
| `app/agent/deals_tracker/service.py` | `SearchIntent`, retailer routing, search flow, fashion checks, ranking, persistence | Validation is largely title-based; unknown garment attributes can be labeled mismatches; enrichment selects from first six items per source and caps at eight overall. |
| `app/agent/deals_tracker/scraper.py` | `ScrapedProduct`, `search_retailer`, `scrape_url`, listing/detail parsing | Lists up to 20 products; product detail records do not reliably carry attribute evidence into verification. |
| `app/api/deals_routes.py` | HTTP search and SSE search; price history and tracking | Preserve external paths and event types. |
| `app/ui/deals.js` | Search flow, products, retailer statuses, comparison, price alerts | Need a visually distinct “possible matches / not verified” section. |
| `app/agent/deals_tracker/models.py` | `DealProduct`, `DealPriceHistory`, `DealAlert` | Preserve storage behavior; avoid conflating retailer variants in later identity work. |

## 2. Target pipeline

`Query → Parsed requirements → Retailer query plans → Provider adapters → Normalized candidates → Broad relevance screen → Bounded detail enrichment → Attribute evidence → Three-state verification → Variant-safe deduplication → Ranking → Existing API/SSE + UI`

1. **Parse:** Produce typed requirements: category/garment, gender, fit, material and composition, color, size, brand, max price, retailer list, result count. Mark each requirement `required` or `preferred`. For “100% cotton”, the composition percentage is required; “cotton” alone cannot establish it.
2. **Discover:** Generate concise retailer-specific searches, excluding conversational words (“provide links”, “compare”, “direct”). Use provider adapters returning candidates and explicit `success`, `empty`, `blocked`, `timeout`, `unsupported`, or `failed` states.
3. **Normalize:** Keep retailer IDs, canonical product URL, title, price/currency, rating, image, variant and available structured attributes. Reject clearly bad URLs, prices, raw fabric, and unrelated categories early; **do not** reject unknown fit as a confirmed mismatch.
4. **Enrich:** Prioritize high-relevance candidates with *missing required fields* across the entire pool, not just the first six. Use configurable per-provider quotas, bounded concurrency, request timeouts, and a shared budget. Try structured product data, specification tables, then trustworthy detail text. Do not bypass anti-bot controls.
5. **Verify:** For each required attribute, return `CONFIRMED`, `UNKNOWN`, or `CONTRADICTED` with source URL, field/excerpt, and capture time. A mismatch requires explicit contradictory evidence; absent evidence is `UNKNOWN`. No LLM inference may turn an unsupported claim into a fact.
6. **Group and rank:** Show **Verified matches** only when all hard requirements are confirmed (including price). Put plausible products with unknown attributes in **Possible matches — verification needed** with clear missing-field labels; omit contradicted products. Rank by relevance, verified attributes, price, and dependable rating, not lowest price alone.

## 3. Internal data contracts (new; backward compatible)

- `SearchRequirement`: `name`, `value`, `required: bool`.
- `AttributeEvidence`: `attribute`, `value`, `source_url`, `source_field`, `excerpt?`, `retrieved_at`.
- `ProductCandidate`: existing `ScrapedProduct` fields + `retailer_product_id?`, `currency='INR'`, `attributes`, `evidence`, `variant`, `availability?`; append optional fields/defaults so existing constructors survive migration.
- `MatchAssessment`: `status: VERIFIED | POSSIBLE | REJECTED`; `attribute_results`, `missing_required`, `contradictions`, `explanation`.
- `RetailerAdapter`: `search(query_plan) -> SourceSearchResult`, `fetch_details(candidate) -> ProductCandidate`; provider methods must describe access limits and explicit statuses.

**Match example:** “Pure Cotton T-Shirt, ₹449” with no oversized-fit evidence ⇒ `POSSIBLE`, `missing_required=['fit:oversized']`; never `VERIFIED`, never automatically `CONTRADICTED`. “100% cotton” requires an explicit composition statement or trusted structured specification; an ordinary “cotton” label is insufficient.

## 4. Integration map and compatibility

- **Keep:** `POST /api/deals/search`, `POST /api/deals/search/stream`, `GET /api/deals/history/{product_id}`, `POST /api/deals/track`; existing SSE `stage`, `intent`, `source_status`, `product`, `final`, `error` events; existing result keys `name`, `price`, `rating`, `link`, `source`, `image_url`, `product_id`, and tracking fields.
- **Extend, don't break:** final payload gains `possible_matches`, `verification_summary` and per-result `match_status`/`evidence`; existing `results` remains verified matches only. SSE `product` must not label unverified items as confirmed; optional `candidate` events can follow client support.
- **Initial adapters:** Wrap current Amazon and Flipkart retrieval without changing the browser engine. Add API/feed adapters only with authorized access; `Myntra=unsupported` until a legitimate data integration is available; `Croma=blocked` remains blocked. No fabricated retailer participation.
- **Cross-store price comparison:** Compare only identical products *and the same variant* using trusted IDs (e.g., GTIN) or corroborated brand/model + size/color/configuration. Otherwise say “No verified identical offers.” Keep price-history records linked to the correct retailer offering/variant; defer DB migration until identity requirements are frozen.

## 5. Incremental implementation sequence

1. **Gate 0 — Baseline:** Reconcile the latest working source with the uploaded ZIP, create a safe code checkpoint, and define 30–50 labeled queries (including strict fashion, laptops, and negative examples). Capture deterministic HTML/API fixtures and existing outputs. No behavior changes.
2. **Gate 1 — Types and evidence:** Add normalized models and evidence extraction in a separate module; adapt existing scraper outputs; shadow-mode diagnostics only.
3. **Gate 2 — Discovery/enrichment:** Implement retailer adapter boundaries, sane queries, bounded candidate selection, and evidence-aware detail enrichment.
4. **Gate 3 — Verification and UI:** Add three-state verification and separate verified/possible results while preserving current endpoints, SSE, and price tracking.
5. **Gate 4 — Provider expansion:** Evaluate official/authorized product APIs and Myntra access independently; enable providers only after access and quality checks.

## 6. Release acceptance criteria

- **No false exact matches:** Every displayed verified result has evidence for every required attribute; 100% cotton and oversized fit must be independently supported.
- **Correct uncertainty:** Missing evidence yields `POSSIBLE`, not `CONTRADICTED`; explicit alternative fabric or fit yields `REJECTED` with reason. No empty-result response that hides possible candidates.
- **Budget and identity:** 100% of verified recommendations respect budget; cross-store comparison excludes different sizes/colors/configurations.
- **Regression:** Existing gaming-laptop, basic T-shirt, price-tracking, source-status, stream/cancellation flows remain functional; compare metrics against frozen baseline.
- **Benchmark:** Report verified Precision@3, Recall@20, attribute evidence coverage, false-rejection rate, source success/blocked rates, and price/link validity on labeled fixtures. Set improvement targets *after* measuring the baseline; do not claim success from a single live query.

**Out of scope for v1:** UI redesign, autonomous checkout, CAPTCHA/bot-evasion, guaranteed Myntra integration, database rewrite, and LLM-only product fact verification.

**Approval gate:** Do not authorize production edits until the baseline snapshot and provider-access feasibility are reviewed against this design.
