from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup

from .composer import ComposedOutput, ResultComposer
from .executor import BrowserExecutor, ExecutionResult
from .extractor import ResultExtractor, NormalizedItem
from .planner import TaskPlanner
from .schema import ActionPlan
from .trusted_sources import detect_intent

logger = logging.getLogger(__name__)


@dataclass
class AutomationRunResult:
    plan: ActionPlan
    execution: ExecutionResult
    items: list[dict[str, Any]]
    output: ComposedOutput


class TaskAutomationController:
    """Orchestrates: planner -> executor -> extractor -> composer."""

    def __init__(self, *, task_id: str = "") -> None:
        self.task_id = task_id
        self.planner = TaskPlanner()
        self.executor = BrowserExecutor(task_id=task_id)
        self.extractor = ResultExtractor()
        self.composer = ResultComposer()

    async def run(self, user_query: str) -> AutomationRunResult:
        logger.info("TaskAutomation: query=%s", user_query)

        plan = await self.planner.plan(user_query)
        self._validate_plan(plan)
        logger.info("TaskAutomation: plan objective=%s steps=%d", plan.objective, len(plan.steps))

        execution = await self.executor.run(plan)

        normalized_items = []
        last_url: str | None = None
        last_title: str | None = None
        last_html: str | None = None
        for blob in execution.collected_html:
            url = str(blob.get("url") or "")
            html = str(blob.get("html") or "")
            last_url = url or last_url
            last_title = str(blob.get("title") or "") or last_title
            last_html = html or last_html
            items = self.extractor.extract_items(
                html=html,
                base_url=url,
                max_items=30,
                user_query=user_query,
                intent=detect_intent(user_query),
            )
            normalized_items.extend(items)

            # Stop early if we already have enough candidates
            min_results = int((plan.stop_when or {}).get("min_results", 5))
            if len(normalized_items) >= min_results:
                break

        # Single fallback overall (avoid adding one per skipped page)
        if not normalized_items:
            normalized_items = [
                self._build_fallback_item(
                    url=last_url or "",
                    title=last_title,
                    html=last_html,
                )
            ]

        composed = self.composer.compose(user_query=user_query, items=normalized_items, top_k=5)

        return AutomationRunResult(
            plan=plan,
            execution=execution,
            items=[it.__dict__ for it in normalized_items],
            output=composed,
        )

    def _build_fallback_item(self, *, url: str, title: str | None, html: str | None) -> NormalizedItem:
        domain = ""
        try:
            from urllib.parse import urlparse

            domain = urlparse(url).netloc.replace("www.", "")
        except Exception:
            domain = ""

        safe_title = (title or "").strip()
        if not safe_title:
            safe_title = domain or "Website"

        snippet = ""
        if html:
            try:
                soup = BeautifulSoup(html, "html.parser")
                text = soup.get_text(" ", strip=True)
                snippet = " ".join(text.split())[:220]
            except Exception:
                snippet = ""

        return NormalizedItem(
            name=safe_title,
            price=None,
            rating=None,
            link=url or None,
            source_domain=domain or None,
            raw={"snippet": snippet} if snippet else {},
        )

    def _validate_plan(self, plan: ActionPlan) -> None:
        steps = plan.ordered_steps()
        if not steps:
            raise ValueError("ActionPlan has no steps")
        # Ensure sequential step numbering.
        expected = list(range(1, len(steps) + 1))
        got = [s.step for s in steps]
        if got != expected:
            raise ValueError(f"ActionPlan step numbering must be sequential: expected {expected}, got {got}")
        # Ensure at least one extraction action exists.
        if not any(s.action.value in {"extract_list", "extract_detail"} for s in steps):
            raise ValueError("ActionPlan must include at least one extraction step")

