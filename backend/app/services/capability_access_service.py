"""Credential-free publication manifest shared by REST and MCP adapters."""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.orm import Session

from ..config import get_settings
from . import (
    capability_application_service,
    permission_service,
    release_service,
    tenant_service,
)
from .capability_contracts import canonical_hash


class CapabilityAccessError(ValueError):
    """Safe failure while resolving a publication manifest."""


def _schema_hash(value: Any, *, domain: str) -> str:
    document = value if isinstance(value, Mapping) else {}
    return canonical_hash(document, domain=domain)


def _capability(document: Mapping[str, Any]) -> dict[str, Any]:
    readiness = document.get("readiness") if isinstance(document.get("readiness"), Mapping) else {}
    issues = readiness.get("issues") if isinstance(readiness.get("issues"), list) else []
    blocking_codes = sorted({
        str(issue.get("code") or "capability_not_ready")
        for issue in issues
        if isinstance(issue, Mapping) and bool(issue.get("blocking", True))
    })
    ports: list[dict[str, Any]] = []
    for raw in document.get("data_ports") or []:
        if not isinstance(raw, Mapping):
            continue
        ports.append({
            "key": str(raw.get("key") or ""),
            "name": str(raw.get("name") or raw.get("key") or ""),
            "direction": str(raw.get("direction") or "input"),
            "role": str(raw.get("role") or "invocation_input"),
            "media_kind": str(raw.get("media_kind") or "structured"),
            "schema_hash": str(raw.get("schema_hash") or ""),
            "required": bool(raw.get("required", True)),
            "cardinality": str(raw.get("cardinality") or "one"),
            "binding_policy": str(raw.get("binding_policy") or "per_invocation"),
        })
    return {
        "kind": str(document.get("kind") or ""),
        "key": str(document.get("key") or ""),
        "name": str(document.get("name") or document.get("key") or ""),
        # Publication manifests carry only hashes. Full schemas remain
        # discoverable at the authenticated protocol endpoint.
        "input_schema_hash": _schema_hash(
            document.get("input_schema"),
            domain="capability-access-input-schema-v1",
        ),
        "output_schema_hash": _schema_hash(
            document.get("output_schema"),
            domain="capability-access-output-schema-v1",
        ),
        "side_effect": bool(document.get("side_effect", False)),
        "requires_confirmation": bool(document.get("requires_confirmation", False)),
        "idempotency_required": bool(document.get("idempotency_required", False)),
        "ready": bool(readiness.get("ready", False)),
        "blocking_codes": blocking_codes,
        "data_ports": ports,
    }


def build_manifest(
    db: Session,
    scenario_id: str,
    *,
    environment: str,
) -> dict[str, Any]:
    scenario = tenant_service.require_scenario(db, scenario_id)
    permission_service.require_scenario_permission(db, scenario, "read")
    try:
        capabilities = capability_application_service.list_capabilities(
            db,
            scenario,
            environment=environment,
        )
        deployment, _inputs = capability_application_service.resolve_deployment(
            db,
            scenario,
            environment=environment,
        )
        releases = release_service.list_releases(
            db,
            scenario.id,
            environment=environment,
        )
    except (
        capability_application_service.CapabilityApplicationError,
        release_service.ReleaseValidationError,
    ) as exc:
        raise CapabilityAccessError(str(exc)) from exc
    if any(
        str(item.get("definition_hash") or "") != deployment.definition_hash
        for item in capabilities
    ):
        raise CapabilityAccessError("能力目录在生成接入清单期间发生变化，请重试")

    settings = get_settings()
    api_prefix = settings.api_prefix.rstrip("/")
    scenario_path = f"{api_prefix}/external/v2/scenarios/{scenario.id}"
    mcp_endpoint = settings.agent_mcp_public_url.strip() or "/mcp"
    capability_documents = [_capability(item) for item in capabilities]
    document: dict[str, Any] = {
        "manifest_version": "capability-access-manifest/v1",
        "scenario": {"id": scenario.id, "name": scenario.name},
        "deployment": {
            "environment": deployment.environment,
            "definition_source": deployment.definition_source,
            "release_id": deployment.release_id,
            "snapshot_id": deployment.snapshot_id,
            "definition_hash": deployment.definition_hash,
        },
        "capabilities": capability_documents,
        "adapters": [
            {
                "protocol": "rest",
                "endpoint": f"{scenario_path}/capabilities",
                "discovery": f"{scenario_path}/capabilities?environment={deployment.environment}",
                "invocation": f"{scenario_path}/capabilities/{{kind}}/{{capability_key}}/invoke",
                "receipt": f"{api_prefix}/external/v2/invocations/{{invocation_id}}",
                "managed_input_upload": f"{api_prefix}/external/v2/assets/upload",
                "authentication": {"scheme": "api_key", "header": "X-API-Key"},
                "required_scopes": ["capabilities:read", "capabilities:invoke"],
                "optional_scopes": ["assets:write"],
                "tools": [],
            },
            {
                "protocol": "mcp",
                "endpoint": mcp_endpoint,
                "authentication": {"scheme": "bearer", "header": "Authorization"},
                "required_scopes": ["capabilities:read", "capabilities:invoke"],
                "tools": [
                    "list_capabilities",
                    "invoke_capability",
                    "get_capability_receipt",
                ],
            },
        ],
        "release_history": [
            {
                "id": release.id,
                "snapshot_id": release.snapshot_id,
                "environment": release.environment,
                "status": release.status,
                "created_at": release.created_at,
            }
            for release in releases
        ],
        "checks": [
            {"code": "definition_resolved", "passed": True},
            {
                "code": "release_pinned",
                "passed": deployment.environment == "dev" or bool(deployment.release_id),
            },
            {
                "code": "capabilities_ready",
                "passed": all(item["ready"] for item in capability_documents),
                "count": sum(not item["ready"] for item in capability_documents),
            },
            {"code": "runtime_bindings_excluded", "passed": True},
            {"code": "credentials_excluded", "passed": True},
        ],
    }
    document["manifest_id"] = canonical_hash(
        document,
        domain="capability-access-manifest-v1",
    )
    return document


__all__ = ["CapabilityAccessError", "build_manifest"]
