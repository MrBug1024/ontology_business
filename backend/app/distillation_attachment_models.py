"""Expiring parsed inputs and immutable links to explicitly authorized turns."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, orm_datetime as DateTime


class DistillationAttachment(Base):
    __tablename__ = "distillation_attachments"
    __table_args__ = (
        ForeignKeyConstraint(["project_id", "tenant_id"], ["distillation_projects.id", "distillation_projects.tenant_id"],
            name="fk_distillation_attachment_project", ondelete="RESTRICT"),
        ForeignKeyConstraint(["scenario_id", "tenant_id"], ["business_scenarios.id", "business_scenarios.tenant_id"],
            name="fk_distillation_attachment_scenario", ondelete="RESTRICT"),
        # Attachment visibility follows the owning project's scenario ACL.
        # Retain the historical owner key for downgrade compatibility;
        # ``created_by`` remains an audit/idempotency field only.
        UniqueConstraint("id", "project_id", "tenant_id", "created_by", name="uq_distillation_attachment_owner"),
        UniqueConstraint("id", "project_id", "tenant_id", name="uq_distillation_attachment_scope"),
        UniqueConstraint("project_id", "created_by", "request_id", name="uq_distillation_attachment_request"),
        CheckConstraint("status IN ('ready','bound','removed','expired')", name="ck_distillation_attachment_status"),
        CheckConstraint("byte_size > 0 AND byte_size <= 10485760 AND char_length(parsed_text) <= 200000", name="ck_distillation_attachment_size"),
        CheckConstraint("status IN ('ready','bound') OR parsed_text = ''", name="ck_distillation_attachment_expiry_content"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    scenario_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    request_id: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(150))
    byte_size: Mapped[int] = mapped_column(Integer)
    content_sha256: Mapped[str] = mapped_column(String(64))
    parsed_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="ready")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class DistillationTurnAttachment(Base):
    __tablename__ = "distillation_turn_attachments"
    __table_args__ = (
        ForeignKeyConstraint(["turn_id", "project_id", "tenant_id"],
            ["distillation_conversation_turns.id", "distillation_conversation_turns.project_id",
             "distillation_conversation_turns.tenant_id"],
            name="fk_distillation_turn_attachment_turn_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["attachment_id", "project_id", "tenant_id"],
            ["distillation_attachments.id", "distillation_attachments.project_id",
             "distillation_attachments.tenant_id"],
            name="fk_distillation_turn_attachment_input_scope", ondelete="RESTRICT"),
        ForeignKeyConstraint(["user_id", "tenant_id"], ["users.id", "users.tenant_id"],
            name="fk_distillation_turn_attachment_actor_tenant", ondelete="RESTRICT"),
    )
    turn_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    attachment_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32))
    project_id: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[str] = mapped_column(String(32))
