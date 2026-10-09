from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.agent.task_automation.controller import TaskAutomationController


router = APIRouter()


class AutomationRunRequest(BaseModel):
    query: str = Field(min_length=3, max_length=2000)


@router.post("/api/automation/run")
async def run_automation(payload: AutomationRunRequest):
    controller = TaskAutomationController()
    try:
        result = await controller.run(payload.query)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "plan": result.plan.model_dump(),
        "execution": {
            "logs": [log.__dict__ for log in result.execution.logs],
            "opened_urls": result.execution.opened_urls,
        },
        "items": result.items,
        "output": {
            "summary": result.output.summary,
            "results": result.output.results,
            "reasoning": result.output.reasoning,
        },
    }

