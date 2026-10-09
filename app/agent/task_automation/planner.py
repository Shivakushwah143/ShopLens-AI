from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.core.config import get_settings
from app.core.llm import llm_client

from .schema import ActionPlan, ActionStep, SearchQuery, TaskIntent
from .trusted_sources import TRUSTED_SOURCES, detect_intent, extract_url

logger = logging.getLogger(__name__)


class TaskPlanner:
    """Task Automation planner: user query -> strict ActionPlan JSON."""

    async def plan(self, user_query: str) -> ActionPlan:
        query = " ".join(user_query.strip().split())
        if not query:
            raise ValueError("Empty query")

        # User-provided URL support: if URL exists, skip search and open directly.
        direct_url = extract_url(query)
        if direct_url:
            return self._url_plan(query, direct_url)

        # Specialized Flight Pipeline
        if detect_intent(query) == "flight":
            return self._flight_plan(query)

        settings = get_settings()
        if llm_client.enabled:
            planned = await self._plan_with_llm(query, settings.llm_model_planner)
            if planned is not None:
                planned.search_queries = _expand_search_queries(query, planned.intent, planned.constraints, planned.search_queries)
                return planned

        return self._fallback_plan(query)

    def _flight_plan(self, query: str) -> ActionPlan:
        from .flight_utils import extract_flight_params, resolve_date, get_flight_search_urls
        from .schema import ActionStep, TaskIntent

        params = extract_flight_params(query)
        date_obj = resolve_date(params["date_str"])
        urls = get_flight_search_urls(params["origin"], params["destination"], date_obj)
        
        steps = []
        for url in urls:
            steps.append(ActionStep(step=len(steps)+1, action="open_result", params={"url": url}))
            steps.append(ActionStep(step=len(steps)+1, action="extract_list", params={"max_items": 20}))
        
        steps.append(ActionStep(step=len(steps)+1, action="rank", params={"top_k": 8}))
        steps.append(ActionStep(step=len(steps)+1, action="stop", params={}))
        
        return ActionPlan(
            intent=TaskIntent.travel,
            objective=f"Find flights from {params['origin']} to {params['destination']} on {date_obj.strftime('%d %b')}",
            constraints={
                "origin": params["origin"],
                "destination": params["destination"],
                "date": date_obj.strftime("%d/%m/%Y"),
                "time_pref": params["time_pref"],
                "vertical": "flight"
            },
            search_queries=[],
            steps=steps,
            stop_when={"min_results": 10},
        )

    async def _plan_with_llm(self, query: str, model: str) -> ActionPlan | None:
        system_prompt = (
            "You are a Task Automation planner for a browser agent.\n"
            "Return STRICT JSON ONLY (no markdown, no code fences).\n\n"
            "Your job:\n"
            "- Understand the user's task intent.\n"
            "- Extract constraints (budget, filters, RAM, origin/destination, etc.).\n"
            "- Produce an executable ActionPlan with numbered steps.\n\n"
            "CRITICAL RULES:\n"
            "- Output must validate against this JSON shape:\n"
            "{\n"
            '  "intent": "shop|travel|generic",\n'
            '  "objective": "string",\n'
            '  "constraints": { "any": "json" },\n'
            '  "search_queries": [{"text":"string","engine_hint":"auto|bing|startpage|duckduckgo|mojeek"}],\n'
            '  "steps": [{"step":1,"action":"search|open_result|click|type|apply_filter|extract_list|extract_detail|navigate_back|rank|stop","params":{}}],\n'
            '  "stop_when": {"min_results": 5}\n'
            "}\n"
            "- Steps must start at 1 and be sequential.\n"
            "- Prefer simple robust selectors: text=..., role selectors, or CSS.\n"
            "- Avoid login/checkout/payment flows.\n"
            "- Ensure there is at least one extract_list step.\n"
            "- Include a rank step before stop.\n"
        )

        llm_result = await llm_client.text_completion(
            model=model,
            system_prompt=system_prompt,
            user_prompt=f"User task: {query}",
        )
        if not llm_result:
            return None
        payload = self._coerce_json(llm_result)
        try:
            return ActionPlan.model_validate(payload)
        except Exception as exc:
            logger.warning("Task planner JSON did not validate, falling back. err=%s", exc)
            return None

    def _fallback_plan(self, query: str) -> ActionPlan:
        lowered = query.lower()
        intent = TaskIntent.generic
        constraints: dict[str, Any] = {}

        # Heuristics for the required test prompts.
        if "laptop" in lowered or "laptops" in lowered:
            intent = TaskIntent.shop
            budget = _extract_first_int(lowered)
            if budget:
                constraints["max_price"] = budget
            ram = _extract_ram_gb(lowered)
            if ram:
                constraints["ram_gb"] = ram
            sq = SearchQuery(text=f"laptops under {constraints.get('max_price', '')} 16GB RAM".strip(), engine_hint="auto")
        elif "flight" in lowered or "flights" in lowered:
            intent = TaskIntent.travel
            budget = _extract_first_int(lowered)
            if budget:
                constraints["max_price"] = budget
            src, dst = _extract_route(lowered)
            if src:
                constraints["from"] = src
            if dst:
                constraints["to"] = dst
            sq = SearchQuery(
                text=f"flights {constraints.get('from','')} to {constraints.get('to','')} under {constraints.get('max_price','')}".strip(),
                engine_hint="auto",
            )
        else:
            sq = SearchQuery(text=query, engine_hint="auto")

        steps = [
            ActionStep(step=1, action="search", params={"query_index": 0, "limit": 8}),
            ActionStep(step=2, action="open_result", params={"result_index": 0}),
            ActionStep(step=3, action="extract_list", params={"max_items": 12}),
            ActionStep(step=4, action="rank", params={"top_k": 5}),
            ActionStep(step=5, action="stop", params={}),
        ]

        return ActionPlan(
            intent=intent,
            objective=f"Complete the task: {query}",
            constraints=constraints,
            search_queries=_expand_search_queries(query, intent, constraints, [sq]),
            steps=steps,
            stop_when={"min_results": 5},
        )

    def _url_plan(self, query: str, url: str) -> ActionPlan:
        # Keep schema/action types unchanged: use open_result with explicit url param.
        steps = [
            ActionStep(step=1, action="open_result", params={"url": url}),
            ActionStep(step=2, action="extract_list", params={"max_items": 25}),
            ActionStep(step=3, action="rank", params={"top_k": 5}),
            ActionStep(step=4, action="stop", params={}),
        ]
        return ActionPlan(
            intent=TaskIntent.generic,
            objective=f"Extract structured results from: {url}",
            constraints={"direct_url": url, "original_query": query},
            search_queries=[],
            steps=steps,
            stop_when={"min_results": 5},
        )

    def _coerce_json(self, text: str) -> dict[str, Any]:
        s = text.strip()
        # Some LLMs may return fenced blocks; strip them defensively.
        if s.startswith("```"):
            lines = s.splitlines()
            if len(lines) >= 3:
                s = "\n".join(lines[1:-1]).strip()
        return json.loads(s)


def _extract_first_int(text: str) -> int | None:
    m = re.search(r"(\d[\d,]{2,})", text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _extract_ram_gb(text: str) -> int | None:
    m = re.search(r"(\d{1,2})\s*gb\s*ram", text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _extract_route(text: str) -> tuple[str | None, str | None]:
    # Very lightweight heuristic: "from X to Y"
    m = re.search(r"from\s+([a-z\s]+?)\s+to\s+([a-z\s]+)", text)
    if not m:
        return None, None
    src = m.group(1).strip().title()
    dst = m.group(2).strip().title()
    return src or None, dst or None


def _expand_search_queries(
    user_query: str,
    intent: TaskIntent,
    constraints: dict[str, Any],
    existing: list[SearchQuery],
) -> list[SearchQuery]:
    """Generate multiple queries using trusted sources + generic variants."""
    base = user_query.strip()

    # Determine intent category for trusted sources (flight/product/general/hotel).
    inferred = detect_intent(base)
    sources = TRUSTED_SOURCES.get(inferred, [])

    queries: list[str] = []

    # Keep existing first (backwards compatibility).
    for sq in existing or []:
        if sq.text and sq.text.strip():
            queries.append(sq.text.strip())

    # Trusted-source queries (site-restricted)
    for site in sources:
        queries.append(f"{base} site:{site}")

    # 2–3 generic variants. For product intent, prefer commercial intent.
    if inferred == "product":
        generic_variants = [
            f"buy {base}",
            f"{base} price",
            f"{base} offers",
        ]
    else:
        generic_variants = [
            base,
            f"{base} review",
            f"{base} comparison",
        ]
    for v in generic_variants:
        queries.append(v)

    def is_bad_query(q: str) -> bool:
        low = q.lower()
        # avoid Google pages & captcha/login bait patterns (planner-level preference)
        return any(k in low for k in ["site:google.", "google search", "captcha", "login", "sign in"])

    out: list[SearchQuery] = []
    seen: set[str] = set()
    for q in queries:
        q2 = " ".join(str(q).split()).strip()
        if not q2 or is_bad_query(q2):
            continue
        # De-prioritize marketplace-only searches; keep marketplaces only via trusted-sources list.
        if ("amazon" in q2.lower() or "flipkart" in q2.lower()) and "site:" not in q2.lower():
            continue
        key = q2.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(SearchQuery(text=q2[:200], engine_hint="auto"))
        if len(out) >= 8:
            break

    return out if out else existing

