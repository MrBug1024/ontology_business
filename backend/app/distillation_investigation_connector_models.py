"""One-shot investigation connector sessions binding a member machine as executor.

The plaintext token exists only once in the creation response; the database
keeps a domain-separated hash, mirroring the platform's other token domains.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, orm_datetime as DateTime


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DistillationInvestigationConnectorSession(Base):
    __tablename__ = "distillation_investigation_connector_sessions"
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "tenant_id"], ["distillation_projects.id", "distillation_projects.tenant_id"],
            name="fk_distillation_connector_project_tenant", ondelete="CASCADE"),
        CheckConstraint("status IN ('pending','connected','revoked','expired')", name="ck_distillation_connector_status"),
        Index("uq_distillation_connector_active", "project_id", "target_key", unique=True,
            postgresql_where=text("status IN ('pending','connected')")),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    project_id: Mapped[str] = mapped_column(String(32))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    target_key: Mapped[str] = mapped_column(String(64))
    scope_hash: Mapped[str] = mapped_column(String(64))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_prefix: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(12), default="pending")
    connector_info: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
