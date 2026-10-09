from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import desc
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db import (
    BrowserActionRecord,
    ErrorEventRecord,
    EvidenceRecord,
    SourceRecord,
    TaskRecord,
)
from app.models.schemas import (
    BrowserAction,
    ErrorEvent,
    EvidenceItem,
    FinalAnswer,
    ResearchPlan,
    SourceItem,
    TaskDetail,
    TaskStatus,
    TaskSummary,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_task(self, query_text: str) -> TaskRecord:
        task = TaskRecord(query_text=query_text)
        self.session.add(task)
        await self.session.commit()
        await self.session.refresh(task)
        return task

    async def get_task(self, task_id: str) -> TaskRecord | None:
        return await self.session.get(TaskRecord, task_id)

    async def list_tasks(self) -> list[TaskRecord]:
        statement = select(TaskRecord).order_by(desc(TaskRecord.created_at))
        result = await self.session.exec(statement)
        return list(result.all())

    async def update_task(
        self,
        task: TaskRecord,
        *,
        status: TaskStatus | None = None,
        current_step: str | None = None,
        plan: ResearchPlan | None = None,
        answer: FinalAnswer | None = None,
        latest_screenshot: str | None = None,
    ) -> TaskRecord:
        if status:
            task.status = status.value
        if current_step:
            task.current_step = current_step
        if plan:
            task.plan_json = plan.model_dump()
        if answer:
            task.answer_json = answer.model_dump()
        if latest_screenshot is not None:
            task.latest_screenshot = latest_screenshot
        task.updated_at = utcnow()
        self.session.add(task)
        await self.session.commit()
        await self.session.refresh(task)
        return task

    async def add_action(self, action: BrowserAction) -> None:
        record = BrowserActionRecord(
            task_id=action.task_id,
            action_type=action.action_type.value,
            description=action.description,
            url=action.url,
            screenshot_path=action.screenshot_path,
            metadata_json=action.metadata,
            created_at=action.created_at,
        )
        self.session.add(record)
        await self.session.commit()

    async def replace_sources(self, task_id: str, sources: list[SourceItem]) -> None:
        statement = select(SourceRecord).where(SourceRecord.task_id == task_id)
        for record in (await self.session.exec(statement)).all():
            await self.session.delete(record)
        await self.session.commit()
        for source in sources:
            self.session.add(
                SourceRecord(
                    task_id=source.task_id,
                    source_type=source.source_type.value,
                    title=source.title,
                    url=str(source.url),
                    domain=source.domain,
                    snippet=source.snippet,
                    author=source.author,
                    published_at=source.published_at,
                    relevance_score=source.relevance_score,
                    authority_score=source.authority_score,
                    freshness_score=source.freshness_score,
                    completeness_score=source.completeness_score,
                    rank_score=source.rank_score,
                    metadata_json=source.metadata,
                )
            )
        await self.session.commit()

    async def replace_evidence(self, task_id: str, evidence_items: list[EvidenceItem]) -> None:
        statement = select(EvidenceRecord).where(EvidenceRecord.task_id == task_id)
        for record in (await self.session.exec(statement)).all():
            await self.session.delete(record)
        await self.session.commit()
        for item in evidence_items:
            self.session.add(
                EvidenceRecord(
                    task_id=item.task_id,
                    source_url=item.source_url,
                    evidence_type=item.evidence_type.value,
                    content=item.content,
                    excerpt=item.excerpt,
                    confidence=item.confidence,
                    metadata_json=item.metadata,
                )
            )
        await self.session.commit()

    async def add_error(self, event: ErrorEvent) -> None:
        record = ErrorEventRecord(
            task_id=event.task_id,
            message=event.message,
            recoverable=event.recoverable,
            context_json=event.context,
            created_at=event.created_at,
        )
        self.session.add(record)
        await self.session.commit()

    async def build_task_detail(self, task_id: str) -> TaskDetail | None:
        task = await self.get_task(task_id)
        if not task:
            return None

        actions_stmt = (
            select(BrowserActionRecord)
            .where(BrowserActionRecord.task_id == task_id)
            .order_by(BrowserActionRecord.created_at)
        )
        sources_stmt = (
            select(SourceRecord)
            .where(SourceRecord.task_id == task_id)
            .order_by(desc(SourceRecord.rank_score))
        )
        evidence_stmt = (
            select(EvidenceRecord)
            .where(EvidenceRecord.task_id == task_id)
            .order_by(desc(EvidenceRecord.confidence))
        )
        errors_stmt = (
            select(ErrorEventRecord)
            .where(ErrorEventRecord.task_id == task_id)
            .order_by(ErrorEventRecord.created_at)
        )

        actions = [
            BrowserAction(
                task_id=record.task_id,
                action_type=record.action_type,
                description=record.description,
                url=record.url,
                screenshot_path=record.screenshot_path,
                created_at=record.created_at,
                metadata=record.metadata_json,
            )
            for record in (await self.session.exec(actions_stmt)).all()
        ]
        sources = [
            SourceItem(
                task_id=record.task_id,
                source_type=record.source_type,
                title=record.title,
                url=record.url,
                domain=record.domain,
                snippet=record.snippet,
                author=record.author,
                published_at=record.published_at,
                relevance_score=record.relevance_score,
                authority_score=record.authority_score,
                freshness_score=record.freshness_score,
                completeness_score=record.completeness_score,
                rank_score=record.rank_score,
                metadata=record.metadata_json,
            )
            for record in (await self.session.exec(sources_stmt)).all()
        ]
        evidence = [
            EvidenceItem(
                task_id=record.task_id,
                source_url=record.source_url,
                evidence_type=record.evidence_type,
                content=record.content,
                excerpt=record.excerpt,
                confidence=record.confidence,
                metadata=record.metadata_json,
            )
            for record in (await self.session.exec(evidence_stmt)).all()
        ]
        errors = [
            ErrorEvent(
                task_id=record.task_id,
                message=record.message,
                recoverable=record.recoverable,
                context=record.context_json,
                created_at=record.created_at,
            )
            for record in (await self.session.exec(errors_stmt)).all()
        ]

        return TaskDetail(
            id=task.id,
            query=task.query_text,
            status=task.status,
            current_step=task.current_step,
            latest_screenshot=task.latest_screenshot,
            created_at=task.created_at,
            updated_at=task.updated_at,
            plan=ResearchPlan.model_validate(task.plan_json) if task.plan_json else None,
            answer=FinalAnswer.model_validate(task.answer_json) if task.answer_json else None,
            sources=sources,
            evidence=evidence,
            actions=actions,
            errors=errors,
        )

    async def build_task_summaries(self) -> list[TaskSummary]:
        tasks = await self.list_tasks()
        return [
            TaskSummary(
                id=task.id,
                query=task.query_text,
                status=task.status,
                current_step=task.current_step,
                latest_screenshot=task.latest_screenshot,
                created_at=task.created_at,
                updated_at=task.updated_at,
            )
            for task in tasks
        ]
