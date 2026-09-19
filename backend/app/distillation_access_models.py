"""Project-bound research grants. Credentials never enter a business document."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, orm_datetime as DateTime


class DistillationSystemAccess(Base):
    __tablename__ = "distillation_system_access"
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "tenant_id"], ["distillation_projects.id", "distillation_projects.tenant_id"],
            name="fk_distillation_system_access_project", ondelete="RESTRICT"),
        UniqueConstraint("project_id", "project_revision", name="uq_distillation_system_access_revision"),
        CheckConstraint("auth_type IN ('basic','bearer','browser')", name="ck_distillation_system_access_auth"),
        Index("ix_distillation_system_access_target", "project_id", "target_key", "project_revision"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    project_id: Mapped[str] = mapped_column(String(32), nullable=False)
    project_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    target_key: Mapped[str] = mapped_column(String(64), nullable=False)
    target_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(16), nullable=False)
    credential_envelope: Mapped[dict] = mapped_column(JSONB, nullable=False)
    authorization_basis: Mapped[str] = mapped_column(String(4000), nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
