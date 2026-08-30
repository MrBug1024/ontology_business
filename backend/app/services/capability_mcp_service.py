"""MCP adapter service for external capability credentials."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from ..database import SessionLocal
from ..models import BusinessScenario
from . import capability_application_service, external_api_service, permission_service
from .capability_contracts import Actor, CapabilityContractError, CapabilityRef, Request
from .capability_invoker import CapabilityInvocationError


class CapabilityMCPError(ValueError):
    """Safe MCP-facing capability failure."""


@dataclass(frozen=True)
class AuthenticatedCapabilityMCP:
    key_id: str
    tenant_id: str
    user_id: str
    scopes: frozenset[str]


def authenticate_token(raw_token: str) -> AuthenticatedCapabilityMCP | None:
    if not str(raw_token or "").startswith("ont_sk_"):
        return None
    db = SessionLocal()
    try:
        try:
            context = external_api_service.authenticate_token(raw_token, db)
        except HTTPException:
            return None
        return AuthenticatedCapabilityMCP(
            key_id=context.key_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            scopes=context.scopes,
        )
    finally:
        db.close()


def _scope(auth: AuthenticatedCapabilityMCP, required: str) -> None:
    if required not in auth.scopes:
        raise CapabilityMCPError("API key lacks the required capability scope")


def _scenario(db, auth: AuthenticatedCapabilityMCP, scenario_id: str) -> BusinessScenario:
    scenario = db.execute(
        select(BusinessScenario).where(
            BusinessScenario.id == scenario_id,
            BusinessScenario.tenant_id == auth.tenant_id,
        )
    ).scalar_one_or_none()
    if scenario is None or not permission_service.check_scenario(db, scenario, "read").allowed:
        raise CapabilityMCPError("business scenario is unavailable")
    return scenario


def _actor(db, auth: AuthenticatedCapabilityMCP) -> Actor:
    principal = permission_service.require_principal(db)
    scopes = set(auth.scopes)
    if "capabilities:read" in scopes:
        scopes.add("capability:read")
    if "capabilities:invoke" in scopes:
        scopes.add("capability:invoke")
    return Actor(
        actor_type="external_api",
        principal_id=auth.key_id,
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        client_id=auth.key_id,
        roles=(principal.role_key,),
        scopes=tuple(scopes),
    )


def _bind(db, auth: AuthenticatedCapabilityMCP) -> None:
    db.info["tenant_id"] = auth.tenant_id
    db.info["user_id"] = auth.user_id
    principal = permission_service.require_principal(db)
    if principal.tenant_id != auth.tenant_id or principal.user_id != auth.user_id:
        raise CapabilityMCPError("capability MCP principal is no longer active")


def list_capabilities(
    auth: AuthenticatedCapabilityMCP,
    *,
    scenario_id: str,
    environment: str,
) -> list[dict[str, Any]]:
    _scope(auth, "capabilities:read")
    db = SessionLocal()
    try:
        _bind(db, auth)
        return capability_application_service.list_capabilities(
            db,
            _scenario(db, auth, scenario_id),
            environment=environment,
        )
    except capability_application_service.CapabilityApplicationError as exc:
        raise CapabilityMCPError(f"{exc.code}: {exc.message}") from None
    finally:
        db.close()


def invoke_capability(
    auth: AuthenticatedCapabilityMCP,
    *,
    scenario_id: str,
    capability_kind: str,
    capability_key: str,
    environment: str,
    inputs: dict[str, Any] | None = None,
    managed_inputs: list[dict[str, Any]] | None = None,
    mode: str = "execute",
    idempotency_key: str | None = None,
    correlation_id: str | None = None,
    request_id: str | None = None,
    expected_definition_hash: str | None = None,
    expected_deployment_fingerprint: str | None = None,
    confirmation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _scope(auth, "capabilities:invoke")
    db = SessionLocal()
    try:
        _bind(db, auth)
        scenario = _scenario(db, auth, scenario_id)
        request = Request(
            capability=CapabilityRef(
                kind=capability_kind,
                resource_id=capability_key,
            ),
            inputs=inputs or {},
            binding_overrides=tuple(
                capability_application_service.managed_binding_override(item)
                for item in (managed_inputs or [])
            ),
            mode=mode,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id or f"mcp:{uuid4().hex}",
            request_id=request_id,
            expected_definition_hash=expected_definition_hash,
            expected_deployment_fingerprint=expected_deployment_fingerprint,
            confirmation=confirmation or {},
        )
        receipt = capability_application_service.invoke(
            db,
            scenario,
            _actor(db, auth),
            request,
            environment=environment,
            invocation_source="mcp",
        )
        db.commit()
        return capability_application_service.receipt_document(receipt)
    except capability_application_service.CapabilityApplicationError as exc:
        db.rollback()
        raise CapabilityMCPError(f"{exc.code}: {exc.message}") from None
    except CapabilityInvocationError as exc:
        db.rollback()
        raise CapabilityMCPError(f"{exc.code}: {exc.message}") from None
    except CapabilityContractError as exc:
        db.rollback()
        raise CapabilityMCPError(f"invalid_capability_request: {exc}") from None
    finally:
        db.close()


def get_receipt(
    auth: AuthenticatedCapabilityMCP,
    *,
    invocation_id: str,
) -> dict[str, Any]:
    _scope(auth, "capabilities:read")
    db = SessionLocal()
    try:
        _bind(db, auth)
        return capability_application_service.get_receipt(
            db,
            _actor(db, auth),
            invocation_id,
        )
    except capability_application_service.CapabilityApplicationError as exc:
        raise CapabilityMCPError(f"{exc.code}: {exc.message}") from None
    finally:
        db.close()


__all__ = [
    "AuthenticatedCapabilityMCP",
    "CapabilityMCPError",
    "authenticate_token",
    "get_receipt",
    "invoke_capability",
    "list_capabilities",
]
