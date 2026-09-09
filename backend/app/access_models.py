"""Account governance and workspace invitation persistence."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, orm_datetime as DateTime


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class AccessGovernanceGuard(Base):
    __tablename__ = "access_governance_guard"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bootstrap_completed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class WorkspaceInvitation(Base):
    __tablename__ = "workspace_invitations"
    __table_args__ = (
        UniqueConstraint("organization_id", "email", name="uq_workspace_invitation_email"),
        ForeignKeyConstraint(["role_id", "organization_id"],
                             ["organization_roles.id", "organization_roles.organization_id"],
                             name="fk_workspace_invitation_role_org", ondelete="RESTRICT"),
        CheckConstraint("status IN ('pending', 'accepted', 'declined', 'revoked')", name="ck_workspace_invitation_status"),
        CheckConstraint("delivery_status IN ('queued', 'sending', 'sent', 'failed', 'indeterminate', 'cancelled')", name="ck_workspace_invitation_delivery"),
        Index("ix_workspace_invitation_recipient", "email", "status", "expires_at"),
        Index("ix_workspace_invitation_delivery", "delivery_status", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    role_id: Mapped[str] = mapped_column(String(32), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    invited_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_status: Mapped[str] = mapped_column(String(20), default="queued")
    delivery_generation: Mapped[int] = mapped_column(Integer, default=1)
    delivery_lease: Mapped[str | None] = mapped_column(String(32), nullable=True)
    delivery_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class AccessAuditEvent(Base):
    __tablename__ = "access_audit_events"
    __table_args__ = (Index("ix_access_audit_scope_time", "tenant_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str | None] = mapped_column(ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=True)
    actor_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    target_id: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    before_value: Mapped[str] = mapped_column(String(100), default="")
    after_value: Mapped[str] = mapped_column(String(100), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    correlation_id: Mapped[str] = mapped_column(String(32), default=new_id)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuthRateLimit(Base):
    __tablename__ = "auth_rate_limits"
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
