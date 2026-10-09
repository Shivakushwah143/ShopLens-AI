from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class TaskIntent(str, Enum):
    shop = "shop"
    travel = "travel"
    generic = "generic"


class ActionType(str, Enum):
    search = "search"
    open_result = "open_result"
    click = "click"
    type = "type"
    apply_filter = "apply_filter"
    extract_list = "extract_list"
    extract_detail = "extract_detail"
    navigate_back = "navigate_back"
    rank = "rank"
    stop = "stop"


class SearchQuery(BaseModel):
    text: str = Field(min_length=2, max_length=200)
    engine_hint: Literal["auto", "bing", "startpage", "duckduckgo", "mojeek"] = "auto"


class ActionStep(BaseModel):
    step: int = Field(ge=1)
    action: ActionType
    params: dict[str, Any] = Field(default_factory=dict)


class ActionPlan(BaseModel):
    """Strict plan for an action-oriented browser automation workflow."""

    intent: TaskIntent = TaskIntent.generic
    objective: str = Field(min_length=3, max_length=500)
    constraints: dict[str, Any] = Field(default_factory=dict)
    search_queries: list[SearchQuery] = Field(default_factory=list)
    steps: list[ActionStep] = Field(default_factory=list)
    stop_when: dict[str, Any] = Field(default_factory=lambda: {"min_results": 5})

    def ordered_steps(self) -> list[ActionStep]:
        return sorted(self.steps, key=lambda s: s.step)

