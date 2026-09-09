"""Durable references keep reviewed evidence available for audit."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, orm_datetime as DateTime


class WorkflowApprovalEvidence(Base):
    __tablename__ = "workflow_approval_evidence"
    __table_args__ = (
        ForeignKeyConstraint(["approval_id", "scenario_id"], ["workflow_approval_requests.id", "workflow_approval_requests.scenario_id"], name="fk_approval_evidence_request", ondelete="RESTRICT"),
        ForeignKeyConstraint(["scenario_id", "tenant_id"], ["business_scenarios.id", "business_scenarios.tenant_id"], name="fk_approval_evidence_scenario", ondelete="RESTRICT"),
        ForeignKeyConstraint(["asset_version_id", "tenant_id"], ["data_asset_versions.id", "data_asset_versions.tenant_id"], name="fk_approval_evidence_asset", ondelete="RESTRICT"),
        ForeignKeyConstraint(["dataset_version_id", "tenant_id"], ["dataset_versions.id", "dataset_versions.tenant_id"], name="fk_approval_evidence_dataset", ondelete="RESTRICT"),
        CheckConstraint("(asset_version_id IS NOT NULL AND dataset_version_id IS NULL) OR (asset_version_id IS NULL AND dataset_version_id IS NOT NULL)", name="ck_approval_evidence_reference"),
        UniqueConstraint("approval_id", "asset_version_id", name="uq_approval_evidence_asset"),
        UniqueConstraint("approval_id", "dataset_version_id", name="uq_approval_evidence_dataset"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid4().hex)
    approval_id: Mapped[str] = mapped_column(String(32), nullable=False)
    scenario_id: Mapped[str] = mapped_column(String(32), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_version_id: Mapped[str | None] = mapped_column(String(32), index=True)
    dataset_version_id: Mapped[str | None] = mapped_column(String(32))
    bucket_file_id: Mapped[str | None] = mapped_column(ForeignKey("bucket_files.id", ondelete="RESTRICT"), index=True)
    content_signature: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
