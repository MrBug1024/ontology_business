"""Tenant-owned discovery drafts and immutable, atomic modeling publications."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, orm_datetime as DateTime


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DistillationProject(Base):
    __tablename__ = "distillation_projects"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_distillation_projects_tenant"),
        CheckConstraint("revision >= 1", name="ck_distillation_project_revision"),
        ForeignKeyConstraint(
            ["scenario_id", "tenant_id"], ["business_scenarios.id", "business_scenarios.tenant_id"],
            name="fk_distillation_project_scenario_tenant", ondelete="RESTRICT",
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), index=True)
    scenario_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    document: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    updated_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DistillationScenarioState(Base):
    __tablename__ = "distillation_scenario_states"
    __table_args__ = (
        UniqueConstraint("scenario_id", "tenant_id", name="uq_distillation_scenario_state"),
        CheckConstraint("revision >= 1", name="ck_distillation_scenario_state_revision"),
        ForeignKeyConstraint(
            ["scenario_id", "tenant_id"], ["business_scenarios.id", "business_scenarios.tenant_id"],
            name="fk_distillation_scenario_state_tenant", ondelete="RESTRICT",
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    scenario_id: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    document: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    updated_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DistillationPublication(Base):
    __tablename__ = "distillation_publications"
    __table_args__ = (
        UniqueConstraint("project_id", "project_revision", name="uq_distillation_publication_revision"),
        UniqueConstraint("data_source_id", name="uq_distillation_publication_source"),
        ForeignKeyConstraint(
            ["project_id", "tenant_id"], ["distillation_projects.id", "distillation_projects.tenant_id"],
            name="fk_distillation_publication_project_tenant",
        ),
        ForeignKeyConstraint(
            ["scenario_id", "tenant_id"], ["business_scenarios.id", "business_scenarios.tenant_id"],
            name="fk_distillation_publication_scenario_tenant", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["data_source_id", "tenant_id"], ["data_sources.id", "data_sources.tenant_id"],
            name="fk_distillation_publication_source_tenant", ondelete="RESTRICT",
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(32), nullable=False)
    scenario_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    project_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    data_source_id: Mapped[str] = mapped_column(String(32), nullable=False)
    document: Mapped[dict] = mapped_column(JSONB, nullable=False)
    artifacts: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
