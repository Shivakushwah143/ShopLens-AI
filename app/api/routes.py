from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse
from sqlmodel.ext.asyncio.session import AsyncSession

from app.agent.orchestrator import ResearchOrchestrator, task_runner
from app.core.database import get_session
from app.core.stream import screencast_stream
from app.models.schemas import TaskCreateRequest
from app.storage.repositories import TaskRepository

router = APIRouter()
orchestrator = ResearchOrchestrator()

@router.websocket("/api/tasks/{task_id}/screencast")
async def task_screencast(websocket: WebSocket, task_id: str):
    await screencast_stream.connect(task_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        screencast_stream.disconnect(task_id, websocket)


@router.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/ui/index.html")


@router.post("/api/tasks")
async def create_task(
    payload: TaskCreateRequest,
    session: AsyncSession = Depends(get_session),
):
    repo = TaskRepository(session)
    task = await repo.create_task(payload.query)
    background_session = AsyncSession(session.bind)
    runner = asyncio.create_task(orchestrator.run(background_session, task.id))
    task_runner.track(task.id, runner)
    return {"task_id": task.id}


@router.get("/api/tasks")
async def list_tasks(session: AsyncSession = Depends(get_session)):
    repo = TaskRepository(session)
    return await repo.build_task_summaries()


@router.get("/api/tasks/{task_id}")
async def get_task(task_id: str, session: AsyncSession = Depends(get_session)):
    repo = TaskRepository(session)
    task = await repo.build_task_detail(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.get("/artifacts/{path:path}")
async def get_artifact(path: str):
    file_path = Path("artifacts") / path
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(file_path)
