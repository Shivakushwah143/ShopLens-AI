from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import Column, JSON, Text
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DealProduct(SQLModel, table=True):
    """Canonical product tracked by the deals agent."""

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    name: str
    canonical_url: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class DealPriceHistory(SQLModel, table=True):
    """Price snapshot for a tracked product."""

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    product_id: str = Field(index=True)
    price: float
    source: str
    link: str | None = None
    rating: float | None = None
    checked_at: datetime = Field(default_factory=_utcnow)


class DealAlert(SQLModel, table=True):
    """User-defined price alert."""

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    product_id: str = Field(index=True)
    target_price: float
    triggered: bool = Field(default=False)
    created_at: datetime = Field(default_factory=_utcnow)
