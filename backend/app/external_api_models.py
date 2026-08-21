"""Persistence models for the P2 external API credential boundary.

The browser session token and an integration credential deliberately live in
different tables and use different hash domains.  An external API key therefore
cannot accidentally authenticate a browser endpoint (or the other way around).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ExternalApiKey(Base):
    """A revocable, scoped credential for the versioned external API.

    ``token_hash`` is the only representation of the secret retained by the
    platform.  ``key_prefix`` and ``token_hint`` are non-secret operator aids
    for identifying a credential in the management UI/API.
    """

    __tablename__ = "external_api_keys"
    __table_args__ = (
        Index("ix_external_api_keys_tenant_status", "tenant_id", "status"),
        Index("ix_external_api_keys_user_status", "user_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # The credential subject and the governance actor are deliberately
    # independent: an owner may issue a constrained key for another member.
    # They are nullable so an upgraded legacy row is never assigned a made-up
    # historical actor.
    issued_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    revoked_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    token_hint: Mapped[str] = mapped_column(String(8), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class ExternalApiKeyAuditEvent(Base):
    """Append-only governance evidence for external credential lifecycle.

    API-key metadata is mutable in one direction (active -> revoked), while an
    event retains the original issuer/revoker and safe policy facts.  No secret,
    token hash, or endpoint configuration is ever written here.
    """

    __tablename__ = "external_api_key_audit_events"
    __table_args__ = (
        Index("ix_external_api_key_audit_events_key_created", "api_key_id", "created_at"),
        Index("ix_external_api_key_audit_events_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    api_key_id: Mapped[str] = mapped_column(
        # Lifecycle evidence is append-only through the application.  A parent
        # tenant/user deletion may still cascade its credential rows during
        # retention cleanup, so database-level cleanup must not deadlock that
        # legitimate cascade.
        ForeignKey("external_api_keys.id", ondelete="CASCADE"), index=True, nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    subject_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # issued / revoked.  There is intentionally no update/delete management
    # route for audit events.
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
