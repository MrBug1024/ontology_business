"""Durable, fenced investigation turns. No browser connection owns execution."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, orm_datetime as DateTime


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DistillationConversationTurn(Base):
    __tablename__ = "distillation_conversation_turns"
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "tenant_id"], ["distillation_projects.id", "distillation_projects.tenant_id"],
            name="fk_distillation_turn_project_tenant", ondelete="RESTRICT"),
        UniqueConstraint("project_id", "request_id", name="uq_distillation_turn_request"),
        UniqueConstraint("project_id", "turn_number", name="uq_distillation_turn_number"),
        # Turns are visible through the project's scenario ACL. Keep both the
        # historical owner key (for migration compatibility) and a
        # tenant/project-scoped key for link integrity; ``created_by`` is
        # audit metadata, not an access boundary.
        UniqueConstraint("id", "project_id", "tenant_id", "created_by", name="uq_distillation_turn_owner"),
        UniqueConstraint("id", "project_id", "tenant_id", name="uq_distillation_turn_scope"),
        CheckConstraint("status IN ('queued','running','waiting','succeeded','cancelled','failed')", name="ck_distillation_turn_status"),
        CheckConstraint("base_revision >= 1 AND turn_number >= 1 AND attempt >= 0 AND model_calls >= 0 AND lease_generation >= 0", name="ck_distillation_turn_counters"),
        Index("uq_distillation_turn_active", "project_id", unique=True, postgresql_where=text("status IN ('queued','running')")),
        Index("ix_distillation_turn_claim", "status", "lease_expires_at", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    project_id: Mapped[str] = mapped_column(String(32))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    turn_number: Mapped[int] = mapped_column(Integer)
    request_id: Mapped[str] = mapped_column(String(64))
    input_hash: Mapped[str] = mapped_column(String(64))
    base_revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    message: Mapped[str] = mapped_column(Text)
    assistant_message: Mapped[str] = mapped_column(Text, default="")
    steps: Mapped[list] = mapped_column(JSONB, default=list)
    questions: Mapped[list] = mapped_column(JSONB, default=list)
    proposal: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    applied_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str] = mapped_column(String(500), default="")
    context: Mapped[dict] = mapped_column(JSONB)
    checkpoint: Mapped[list] = mapped_column(JSONB, default=list)
    model_calls: Mapped[int] = mapped_column(Integer, default=0)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lease_generation: Mapped[int] = mapped_column(Integer, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
