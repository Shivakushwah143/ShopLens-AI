from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import Column, JSON, Text
from sqlmodel import Field, SQLModel

from app.models.schemas import TaskStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskRecord(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    query_text: str
    status: str = Field(default=TaskStatus.pending.value)
    current_step: str = Field(default="Queued")
    plan_json: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    answer_json: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    latest_screenshot: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class BrowserActionRecord(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    task_id: str = Field(index=True)
    action_type: str
    description: str
    url: str | None = None
    screenshot_path: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)


class SourceRecord(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    task_id: str = Field(index=True)
    source_type: str
    title: str
    url: str
    domain: str
    snippet: str | None = Field(default=None, sa_column=Column(Text))
    author: str | None = None
    published_at: str | None = None
    relevance_score: float = 0.0
    authority_score: float = 0.0
    freshness_score: float = 0.0
    completeness_score: float = 0.0
    rank_score: float = 0.0
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class EvidenceRecord(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    task_id: str = Field(index=True)
    source_url: str
    evidence_type: str
    content: str = Field(sa_column=Column(Text))
    excerpt: str | None = Field(default=None, sa_column=Column(Text))
    confidence: float = 0.0
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class ErrorEventRecord(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    task_id: str = Field(index=True)
    message: str
    recoverable: bool = True
    context_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)
