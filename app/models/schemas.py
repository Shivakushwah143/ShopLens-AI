from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class TaskStatus(str, Enum):
    pending = "pending"
    planning = "planning"
    researching = "researching"
    ranking = "ranking"
    verifying = "verifying"
    composing = "composing"
    completed = "completed"
    failed = "failed"


class ResearchMode(str, Enum):
    web = "web"
    video = "video"
    mixed = "mixed"


class UserQuery(BaseModel):
    text: str = Field(min_length=3, max_length=2000)
    require_citations: bool = True
    preferred_answer_style: str = "concise"


class QueryIntent(BaseModel):
    mode: ResearchMode
    topic: str
    subtopics: list[str] = Field(default_factory=list)
    requires_youtube: bool = False
    answer_format: str = "direct_answer"


class ResearchTask(BaseModel):
    task_id: str
    query: UserQuery
    intent: QueryIntent
    status: TaskStatus = TaskStatus.pending
    created_at: datetime


class ResearchPlan(BaseModel):
    objective: str
    search_queries: list[str]
    video_queries: list[str] = Field(default_factory=list)
    subquestions: list[str] = Field(default_factory=list)
    source_limit: int = 5
    stopping_criteria: list[str] = Field(default_factory=list)


class BrowserActionType(str, Enum):
    search = "search"
    navigate = "navigate"
    extract = "extract"
    screenshot = "screenshot"
    rank = "rank"
    verify = "verify"
    compose = "compose"
    error = "error"


class BrowserAction(BaseModel):
    task_id: str
    action_type: BrowserActionType
    description: str
    url: str | None = None
    screenshot_path: str | None = None
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceType(str, Enum):
    web = "web"
    video = "video"


class SourceItem(BaseModel):
    task_id: str
    source_type: SourceType
    title: str
    url: HttpUrl | str
    domain: str
    snippet: str | None = None
    author: str | None = None
    published_at: str | None = None
    relevance_score: float = 0.0
    authority_score: float = 0.0
    freshness_score: float = 0.0
    completeness_score: float = 0.0
    rank_score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidenceType(str, Enum):
    text = "text"
    table = "table"
    image = "image"
    transcript = "transcript"
    metadata = "metadata"


class EvidenceItem(BaseModel):
    task_id: str
    source_url: str
    evidence_type: EvidenceType
    content: str
    excerpt: str | None = None
    confidence: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class VideoItem(BaseModel):
    task_id: str
    title: str
    url: HttpUrl | str
    channel: str | None = None
    description: str | None = None
    transcript_excerpt: str | None = None
    duration_text: str | None = None
    rank_score: float = 0.0
    reasons: list[str] = Field(default_factory=list)


class ImageItem(BaseModel):
    """An image extracted from a web source during research."""
    task_id: str
    src: str
    alt: str
    source_url: str
    source_title: str = ""
    relevance_score: float = 0.0


class ConfidenceScore(BaseModel):
    overall: float
    evidence_count: int
    source_count: int
    conflicts: list[str] = Field(default_factory=list)
    rationale: str


class FinalAnswer(BaseModel):
    direct_answer: str
    supporting_points: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    videos: list[VideoItem] = Field(default_factory=list)
    images: list[ImageItem] = Field(default_factory=list)
    confidence: ConfidenceScore


class ErrorEvent(BaseModel):
    task_id: str
    message: str
    recoverable: bool = True
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class TaskCreateRequest(BaseModel):
    query: str = Field(min_length=3, max_length=2000)


class TaskSummary(BaseModel):
    id: str
    query: str
    status: TaskStatus
    current_step: str
    latest_screenshot: str | None = None
    created_at: datetime
    updated_at: datetime


class TaskDetail(TaskSummary):
    plan: ResearchPlan | None = None
    answer: FinalAnswer | None = None
    sources: list[SourceItem] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    actions: list[BrowserAction] = Field(default_factory=list)
    errors: list[ErrorEvent] = Field(default_factory=list)
