"""Protocol-neutral application service for capability discovery and invocation.

REST, MCP, validation Agents, and SDK-facing adapters share this service.  It
resolves the server-owned runtime definition and governed data bindings, but it
never accepts physical source configuration or embeds protocol response types.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BusinessScenario, CapabilityInvocation
from . import (
    capability_readiness_service,
    permission_service,
    runtime_definition_service,
    runtime_input_service,
)
from .capability_contracts import (
    Actor,
    CapabilityContractError,
    CapabilityRef,
    Receipt,
    Request,
    ResolvedDeployment,
    RuntimeDataContext,
    canonical_hash,
    canonical_json,
)
from .capability_invoker import (
    CapabilityInvocationError,
    CapabilityInvoker,
    resolve_capability_contract,
)


DISCOVERABLE_CAPABILITY_KINDS = ("function", "action", "workflow")
_MANAGED_REFERENCE_FIELDS = {
    "dataset_version_id": "dataset_version",
    "dataset_head_id": "dataset_head",
    "asset_version_id": "asset_version",
    "artifact_id": "artifact",
    "binding_key": "connector_binding",
}


class CapabilityApplicationError(ValueError):
    """Stable adapter-facing failure that contains no framework dependency."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 409,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "capability_application_error").strip().lower()
        self.message = str(message or "capability application request failed").strip()
        self.status_code = int(status_code)
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            result["details"] = dict(self.details)
        return result


def _read(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _plain(value: Any) -> Any:
    return json.loads(canonical_json(value))


def managed_binding_override(document: Mapping[str, Any]) -> Any:
    """Convert one public governed-reference document into a kernel override."""

    from .capability_contracts import DataBindingOverride

    allowed = {"port_key", "signature", "expected_signature", *_MANAGED_REFERENCE_FIELDS}
    unknown = sorted(str(key) for key in set(document) - allowed)
    if unknown:
        raise CapabilityApplicationError(
            "invalid_managed_input",
            "managed input contains unsupported fields",
            status_code=422,
            details={"fields": unknown},
        )
    selectors = [
        (field, kind, document.get(field))
        for field, kind in _MANAGED_REFERENCE_FIELDS.items()
        if document.get(field) not in (None, "")
    ]
    if len(selectors) != 1:
        raise CapabilityApplicationError(
            "invalid_managed_input",
            "managed input requires exactly one governed reference",
            status_code=422,
        )
    port_key = str(document.get("port_key") or "").strip()
    if not port_key:
        raise CapabilityApplicationError(
            "invalid_managed_input",
            "managed input requires a data port key",
            status_code=422,
        )
    field, binding_kind, selector = selectors[0]
    signature = document.get("expected_signature", document.get("signature"))
    values: dict[str, Any] = {
        "port_key": port_key,
        "binding_kind": binding_kind,
        "signature": signature,
    }
    if field == "binding_key":
        values["binding_key"] = selector
    else:
        values["reference_id"] = selector
    try:
        return DataBindingOverride(**values)
    except CapabilityContractError as exc:
        raise CapabilityApplicationError(
            "invalid_managed_input",
            str(exc),
            status_code=422,
        ) from None


def _resources(definition: Any, kind: str) -> Mapping[str, Any]:
    group = {
        "function": "functions",
        "action": "actions",
        "workflow": "workflows",
    }.get(kind)
    if group is None:
        raise CapabilityApplicationError(
            "capability_kind_unsupported",
            "capability kind is not available through this application service",
            status_code=404,
        )
    resources = _read(definition, group, {})
    if not isinstance(resources, Mapping):
        raise CapabilityApplicationError(
            "capability_catalog_invalid",
            "resolved definition contains an invalid capability catalog",
        )
    return resources


def _resource(definition: Any, kind: str, key: str) -> Any:
    resource = _resources(definition, kind).get(str(key))
    if resource is None:
        raise CapabilityApplicationError(
            "capability_not_found",
            "capability is not present in the resolved definition",
            status_code=404,
        )
    return resource


def _permission_allowed(db: Session, definition: Any, kind: str, resource: Any, verb: str) -> bool:
    if kind == "action":
        return permission_service.check_action(db, resource, verb).allowed
    if kind == "workflow":
        return permission_service.check_workflow(db, resource, verb).allowed
    scenario_verb = "write" if verb == "execute" else "read"
    return permission_service.check_scenario(
        db,
        definition.scenario,
        scenario_verb,
    ).allowed


def _require_permission(
    db: Session,
    definition: Any,
    kind: str,
    resource: Any,
    verb: str,
) -> None:
    if not _permission_allowed(db, definition, kind, resource, verb):
        raise CapabilityApplicationError(
            "capability_forbidden",
            "authenticated principal is not allowed to access this capability",
            status_code=403,
        )


def resolve_deployment(
    db: Session,
    scenario: BusinessScenario,
    *,
    environment: str,
    capability: CapabilityRef | None = None,
) -> tuple[ResolvedDeployment, runtime_input_service.DeploymentInputResolution]:
    """Resolve one definition and current safe binding identities.

    Missing per-invocation ports remain discoverable.  They are validated only
    after the caller supplies governed references to the invocation request.
    """

    try:
        definition = runtime_definition_service.resolve_active(
            db,
            scenario,
            environment=environment,
        )
        if capability is None:
            inputs = runtime_input_service.DeploymentInputResolution(
                runtime_data_context=RuntimeDataContext(),
                data_ports=(),
                port_records=(),
                missing_required_ports=(),
            )
        else:
            _resource(definition, capability.kind, capability.resource_id)
            inputs = runtime_input_service.resolve_deployment_inputs(
                db,
                tenant_id=str(scenario.tenant_id or ""),
                scenario_id=scenario.id,
                environment=definition.environment,
                capability=capability,
                definition=definition,
            )
        deployment = ResolvedDeployment(
            scenario_id=scenario.id,
            tenant_id=str(scenario.tenant_id or ""),
            environment=definition.environment,
            definition_hash=definition.definition_hash,
            definition=definition,
            data_ports=inputs.data_ports,
            data_context=inputs.runtime_data_context,
            definition_source=definition.source,
            snapshot_id=definition.snapshot_id,
            release_id=definition.release_id,
        )
    except runtime_definition_service.RuntimeDefinitionError as exc:
        raise CapabilityApplicationError(
            "deployment_definition_unavailable",
            str(exc),
        ) from None
    except runtime_input_service.RuntimeInputResolutionError as exc:
        raise CapabilityApplicationError(
            exc.code,
            exc.message,
            details=exc.details,
        ) from None
    except CapabilityContractError as exc:
        raise CapabilityApplicationError(
            "deployment_contract_invalid",
            str(exc),
        ) from None
    return deployment, inputs


def _port_schema_hash(port: Any, schema_document: Mapping[str, Any]) -> str:
    explicit = str(_read(port, "dataset_schema_hash", "") or "").strip().lower()
    if explicit:
        return explicit
    dataset_schema = _read(port, "dataset_schema")
    schema_hash = str(_read(dataset_schema, "schema_hash", "") or "").strip().lower()
    return schema_hash or canonical_hash(schema_document, domain="data-port-schema-v1")


def public_ports(definition: Any, capability: CapabilityRef) -> list[dict[str, Any]]:
    raw_ports = _read(definition, "capability_ports", {}) or {}
    values = raw_ports.values() if isinstance(raw_ports, Mapping) else raw_ports
    if not isinstance(values, Sequence) and not hasattr(values, "__iter__"):
        raise CapabilityApplicationError(
            "capability_port_catalog_invalid",
            "resolved definition contains an invalid capability port catalog",
        )
    result: list[dict[str, Any]] = []
    for port in values:
        if (
            str(_read(port, "capability_kind", "") or "").strip().lower()
            != capability.kind
            or str(_read(port, "capability_key", "") or "").strip()
            != capability.resource_id
        ):
            continue
        key = str(_read(port, "port_key", "") or "").strip()
        if not key:
            raise CapabilityApplicationError(
                "capability_port_contract_invalid",
                "resolved definition contains a capability port without a key",
            )
        schema_document = _read(port, "schema_document", {}) or {}
        if not isinstance(schema_document, Mapping):
            raise CapabilityApplicationError(
                "capability_port_contract_invalid",
                "capability port schema must be an object",
            )
        plain_schema = _plain(schema_document)
        result.append(
            {
                "key": key,
                "name": str(_read(port, "name", "") or key),
                "description": str(_read(port, "description", "") or ""),
                "direction": str(_read(port, "direction", "input") or "input"),
                "role": str(_read(port, "role", "invocation_input") or "invocation_input"),
                "media_kind": str(_read(port, "media_kind", "structured") or "structured"),
                "schema_document": plain_schema,
                "schema_hash": _port_schema_hash(port, plain_schema),
                "required": bool(_read(port, "is_required", True)),
                "cardinality": str(_read(port, "cardinality", "one") or "one"),
                "binding_policy": str(
                    _read(port, "binding_policy", "per_invocation") or "per_invocation"
                ),
                "binding_kinds": list(
                    runtime_input_service.allowed_binding_kinds(port)
                ),
                "allow_override": (
                    runtime_input_service.allows_invocation_override(port)
                ),
            }
        )
    return sorted(result, key=lambda item: (item["direction"], item["key"]))


def _runtime_issues(
    inputs: runtime_input_service.DeploymentInputResolution,
) -> list[dict[str, Any]]:
    missing = set(inputs.missing_required_ports)
    issues: list[dict[str, Any]] = []
    for port in inputs.port_records:
        key = str(_read(port, "port_key", "") or "").strip().lower()
        if key not in missing:
            continue
        invocation_supplied = runtime_input_service.allows_invocation_override(port)
        issues.append(
            {
                "axis": "runtime",
                "blocking": not invocation_supplied,
                "code": (
                    "invocation_input_required"
                    if invocation_supplied
                    else "runtime_binding_missing"
                ),
                "message": (
                    "required managed input must be supplied with the invocation"
                    if invocation_supplied
                    else "required managed input has no environment binding"
                ),
                "port_key": key,
            }
        )
    return issues


def _capability_document(
    db: Session,
    deployment: ResolvedDeployment,
    inputs: runtime_input_service.DeploymentInputResolution,
    *,
    kind: str,
    resource: Any,
) -> dict[str, Any]:
    key = str(_read(resource, "id", "") or "").strip()
    capability = CapabilityRef(kind=kind, resource_id=key)
    issues: list[dict[str, Any]] = []
    contract: dict[str, Any] = {
        "input_schema": {},
        "output_schema": {},
        "side_effect": False,
        "requires_confirmation": False,
        "idempotency_required": False,
    }
    try:
        contract.update(resolve_capability_contract(db, deployment, capability))
    except CapabilityInvocationError as exc:
        issues.append(
            {
                "axis": "definition",
                "blocking": True,
                **exc.as_dict(),
            }
        )

    readiness = capability_readiness_service.capability_readiness(
        kind,
        resource,
        definition=deployment.definition,
        db=db,
    )
    issues.extend(
        {
            "axis": "validation",
            "blocking": True,
            "code": "capability_not_ready",
            "message": reason,
        }
        for reason in readiness.blocked_reasons
    )
    issues.extend(_runtime_issues(inputs))
    return {
        "scenario_id": deployment.scenario_id,
        "environment": deployment.environment,
        "kind": kind,
        "key": key,
        "name": str(_read(resource, "name", "") or key),
        "description": str(_read(resource, "description", "") or ""),
        "definition_hash": deployment.definition_hash,
        "deployment_fingerprint": deployment.fingerprint,
        "input_schema": _plain(contract.get("input_schema") or {}),
        "output_schema": _plain(contract.get("output_schema") or {}),
        "side_effect": bool(contract.get("side_effect", False)),
        "requires_confirmation": bool(contract.get("requires_confirmation", False)),
        "idempotency_required": bool(contract.get("idempotency_required", False)),
        "data_ports": public_ports(deployment.definition, capability),
        "readiness": {
            "ready": not any(bool(issue.get("blocking", True)) for issue in issues),
            "issues": issues,
        },
    }


def list_capabilities(
    db: Session,
    scenario: BusinessScenario,
    *,
    environment: str,
) -> list[dict[str, Any]]:
    base_deployment, _inputs = resolve_deployment(
        db, scenario, environment=environment
    )
    result: list[dict[str, Any]] = []
    for kind in DISCOVERABLE_CAPABILITY_KINDS:
        for key, resource in sorted(_resources(base_deployment.definition, kind).items()):
            if not _permission_allowed(db, base_deployment.definition, kind, resource, "read"):
                continue
            capability = CapabilityRef(kind=kind, resource_id=str(key))
            deployment, inputs = resolve_deployment(
                db,
                scenario,
                environment=environment,
                capability=capability,
            )
            result.append(
                _capability_document(
                    db,
                    deployment,
                    inputs,
                    kind=kind,
                    resource=resource,
                )
            )
    return result


def get_capability(
    db: Session,
    scenario: BusinessScenario,
    *,
    environment: str,
    kind: str,
    key: str,
) -> dict[str, Any]:
    capability = CapabilityRef(kind=kind, resource_id=key)
    deployment, inputs = resolve_deployment(
        db,
        scenario,
        environment=environment,
        capability=capability,
    )
    resource = _resource(deployment.definition, kind, key)
    _require_permission(db, deployment.definition, kind, resource, "read")
    return _capability_document(
        db,
        deployment,
        inputs,
        kind=kind,
        resource=resource,
    )


def list_managed_input_options(
    db: Session,
    scenario: BusinessScenario,
    *,
    environment: str,
    kind: str,
    key: str,
    port_key: str,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Discover governed references for one authoritative input-port contract."""

    capability = CapabilityRef(kind=kind, resource_id=key)
    deployment, inputs = resolve_deployment(
        db,
        scenario,
        environment=environment,
        capability=capability,
    )
    resource = _resource(deployment.definition, kind, key)
    _require_permission(db, deployment.definition, kind, resource, "read")
    if not permission_service.check_tenant_permission(db, "read").allowed:
        raise CapabilityApplicationError(
            "catalog_forbidden",
            "authenticated principal is not allowed to read governed catalog options",
            status_code=403,
        )

    capability_document = _capability_document(
        db,
        deployment,
        inputs,
        kind=kind,
        resource=resource,
    )
    if not bool(capability_document["readiness"]["ready"]):
        blocking_codes = sorted(
            {
                str(issue.get("code") or "capability_not_ready")
                for issue in capability_document["readiness"]["issues"]
                if bool(issue.get("blocking", True))
            }
        )
        raise CapabilityApplicationError(
            "capability_not_ready",
            "capability is not ready in the selected environment",
            details={"blocking_codes": blocking_codes},
        )

    normalized_port_key = str(port_key or "").strip()
    if not normalized_port_key:
        raise CapabilityApplicationError(
            "runtime_input_port_not_found",
            "managed input port is unavailable in the capability contract",
            status_code=404,
        )
    matching_ports = tuple(
        port
        for port in inputs.port_records
        if str(_read(port, "port_key", "") or "") == normalized_port_key
    )
    if len(matching_ports) != 1:
        raise CapabilityApplicationError(
            "runtime_input_port_not_found",
            "managed input port is unavailable in the capability contract",
            status_code=404,
        )
    port = matching_ports[0]
    if not runtime_input_service.allows_invocation_override(port):
        raise CapabilityApplicationError(
            "runtime_input_override_forbidden",
            "managed input port does not allow per-invocation selection",
        )

    try:
        options = runtime_input_service.list_managed_input_options(
            db,
            tenant_id=str(scenario.tenant_id or ""),
            scenario_id=scenario.id,
            environment=deployment.environment,
            port=port,
        )
    except runtime_input_service.RuntimeInputResolutionError as exc:
        raise CapabilityApplicationError(
            exc.code,
            exc.message,
            status_code=(
                404 if exc.code == "runtime_input_port_not_found" else 409
            ),
            details=exc.details,
        ) from None

    normalized_limit = max(1, min(int(limit), 200))
    normalized_offset = max(0, int(offset))
    selected = options[normalized_offset : normalized_offset + normalized_limit]
    return {
        "scenario_id": deployment.scenario_id,
        "environment": deployment.environment,
        "kind": kind,
        "key": key,
        "port_key": normalized_port_key,
        "definition_hash": deployment.definition_hash,
        "deployment_fingerprint": deployment.fingerprint,
        "binding_kinds": list(runtime_input_service.allowed_binding_kinds(port)),
        "allow_override": True,
        "items": [item.safe_document() for item in selected],
        "total": len(options),
        "limit": normalized_limit,
        "offset": normalized_offset,
        "has_more": normalized_offset + len(selected) < len(options),
    }


def invoke(
    db: Session,
    scenario: BusinessScenario,
    actor: Actor,
    request: Request,
    *,
    environment: str,
    invocation_source: str,
    invoker: CapabilityInvoker | None = None,
) -> Receipt:
    deployment, _inputs = resolve_deployment(
        db,
        scenario,
        environment=environment,
        capability=request.capability,
    )
    resource = _resource(
        deployment.definition,
        request.capability.kind,
        request.capability.resource_id,
    )
    _require_permission(
        db,
        deployment.definition,
        request.capability.kind,
        resource,
        "execute",
    )
    return (invoker or CapabilityInvoker()).invoke(
        db,
        deployment,
        actor,
        request,
        invocation_source=invocation_source,
    )


def receipt_document(receipt: Receipt, *, replayed: bool | None = None) -> dict[str, Any]:
    audit_ref = _plain(receipt.audit_ref)
    if replayed is not None:
        audit_ref["replayed"] = replayed
    return {
        "invocation_id": receipt.invocation_id,
        "status": receipt.status,
        "capability": {
            "kind": receipt.capability.kind,
            "key": receipt.capability.resource_id,
        },
        "definition_hash": receipt.definition_hash,
        "deployment_fingerprint": receipt.deployment_fingerprint,
        "data_context_fingerprint": receipt.data_context_fingerprint,
        "output": _plain(receipt.output),
        "audit_ref": audit_ref,
        "confirmation": _plain(receipt.confirmation),
        "error": (
            {
                "code": receipt.error_code,
                "message": receipt.error_message,
            }
            if receipt.error_code
            else None
        ),
    }


def get_receipt(
    db: Session,
    actor: Actor,
    invocation_id: str,
) -> dict[str, Any]:
    invocation = db.execute(
        select(CapabilityInvocation).where(
            CapabilityInvocation.id == invocation_id,
            CapabilityInvocation.tenant_id == actor.tenant_id,
            CapabilityInvocation.principal_type == actor.actor_type,
            CapabilityInvocation.principal_id == actor.principal_id,
        )
    ).scalar_one_or_none()
    if invocation is None:
        raise CapabilityApplicationError(
            "invocation_not_found",
            "capability invocation receipt is unavailable",
            status_code=404,
        )
    scenario = db.get(BusinessScenario, invocation.scenario_id)
    if scenario is None or not permission_service.check_scenario(
        db,
        scenario,
        "read",
    ).allowed:
        # Use the same response as an unknown receipt so revoked ACL state
        # cannot be used to enumerate historical capability executions.
        raise CapabilityApplicationError(
            "invocation_not_found",
            "capability invocation receipt is unavailable",
            status_code=404,
        )
    result = invocation.result_document if isinstance(invocation.result_document, dict) else {}
    receipt = Receipt(
        invocation_id=invocation.id,
        status=invocation.status,
        capability=CapabilityRef(
            kind=invocation.capability_kind,
            resource_id=invocation.capability_key,
        ),
        definition_hash=invocation.definition_hash,
        deployment_fingerprint=invocation.deployment_fingerprint,
        data_context_fingerprint=invocation.data_context_fingerprint,
        output=result.get("output", {}),
        audit_ref={"invocation_id": invocation.id, "replayed": False},
        confirmation=(
            result.get("confirmation", {})
            if isinstance(result.get("confirmation", {}), Mapping)
            else {}
        ),
        error_code=invocation.error_code or None,
        error_message=invocation.error_message or "",
    )
    return receipt_document(receipt, replayed=False)


__all__ = [
    "CapabilityApplicationError",
    "DISCOVERABLE_CAPABILITY_KINDS",
    "get_capability",
    "get_receipt",
    "invoke",
    "list_capabilities",
    "managed_binding_override",
    "public_ports",
    "receipt_document",
    "resolve_deployment",
]
