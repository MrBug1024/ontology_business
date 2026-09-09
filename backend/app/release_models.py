"""Append-only lifecycle evidence for manually managed scenario releases."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import ForeignKey, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, orm_datetime as DateTime


class ReleaseLifecycleEvent(Base):
    __tablename__ = "release_lifecycle_events"
    __table_args__ = (
        UniqueConstraint("release_id", "revision", name="uq_release_events_revision"),
        ForeignKeyConstraint(
            ["release_id", "tenant_id", "scenario_id", "snapshot_id"],
            ["ontology_releases.id", "ontology_releases.tenant_id",
             "ontology_releases.scenario_id", "ontology_releases.snapshot_id"],
            name="fk_release_events_release_scope", ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    release_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    scenario_id: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False,
    )
