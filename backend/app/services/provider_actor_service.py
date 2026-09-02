"""Shared authenticated-actor checks for trusted in-process Providers."""
from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy.orm import Session

from .capability_contracts import Actor


class ProviderActorError(PermissionError):
    """The Provider actor does not match the authenticated server session."""


def require_actor_session(db: Session, actor: Actor) -> str | None:
    """Verify protocol-neutral actor facts against server-authenticated state."""

    session_tenant = str(db.info.get("tenant_id") or "").strip()
    if session_tenant and session_tenant != actor.tenant_id:
        raise ProviderActorError(
            "authenticated session and capability actor use different tenants"
        )
    user_id = str(db.info.get("user_id") or "").strip() or None
    if actor.user_id is not None and actor.user_id != user_id:
        raise ProviderActorError(
            "authenticated session and capability actor do not match"
        )
    if actor.actor_type == "user":
        if user_id != actor.principal_id:
            raise ProviderActorError(
                "authenticated session and capability actor do not match"
            )
        return user_id
    if actor.actor_type == "agent":
        audit = db.info.get("action_audit_context")
        audit_agent_id = (
            str(audit.get("agent_id") or "").strip()
            if isinstance(audit, Mapping)
            else ""
        )
        if (
            actor.agent_id is None
            or actor.client_id is not None
            or actor.principal_id != actor.agent_id
            or actor.user_id != user_id
            or audit_agent_id != actor.agent_id
        ):
            raise ProviderActorError(
                "authenticated agent context does not match capability actor"
            )
        return user_id
    if actor.actor_type in {"client", "external_api", "service"}:
        if (
            actor.client_id is None
            or actor.agent_id is not None
            or actor.principal_id != actor.client_id
            or actor.user_id != user_id
        ):
            raise ProviderActorError(
                "authenticated client context does not match capability actor"
            )
        return user_id
    raise ProviderActorError(
        "non-user capability actor lacks a server-verifiable identity binding"
    )


__all__ = ["ProviderActorError", "require_actor_session"]
