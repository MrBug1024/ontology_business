"""Protocol-neutral capability invocation, validation, and audit state machine."""
from __future__ import annotations

import inspect
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import CapabilityInvocation, RunInputBinding
from .capability_contracts import (
    Actor,
    CapabilityRef,
    CapabilityContractError,
    Receipt,
    Request,
    ResolvedDataHandle,
    ResolvedDeployment,
    RuntimeDataContext,
    canonical_hash,
    canonical_json,
)
from .capability_registry import (
    CapabilityProvider,
    CapabilityProviderRegistry,
    CapabilityRegistryError,
    ProviderRecovery,
    bind_provider,
    builtin_provider_key,
    default_provider_registry,
)
from .deployment_service import (
    DeploymentResolutionError,
    require_request_matches_deployment,
)
from .runtime_input_service import (
    RuntimeInputResolution,
    RuntimeInputResolutionError,
    resolve_runtime_inputs,
)


_MODES = frozenset({"execute", "preview", "confirm"})
_INVOCATION_SOURCES = frozenset({"internal", "agent", "rest", "mcp"})
_SECRET_KEY_RE = re.compile(
    r"(?:password|passwd|secret|credential|token|authorization|cookie|dsn|database[_-]?url|api[_-]?key|connection[_-]?string)",
    re.IGNORECASE,
)
_CONFIRMATION_FIELDS = frozenset(
    {
        "preview_invocation_id",
        "confirmation_token",
        "confirmed",
        "expires_at",
        "expired",
        "required",
    }
)
_MAX_AUDITED_OUTPUT_BYTES = 1_000_000
_MAX_CONFIRMATION_BYTES = 16_384
_MAX_STRUCTURED_INPUT_BYTES = 1_000_000
_DEFAULT_CONFIRMATION_TTL_SECONDS = 900
_MAX_CONFIRMATION_TTL_SECONDS = 86_400
_MIN_CONFIRMATION_TTL_SECONDS = 30
_SAFE_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_CONFIRMATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_CONFIRMATION_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")


class CapabilityInvocationError(ValueError):
    """Structured failure raised before a durable failed Receipt exists."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = _code(code)
        self.message = str(message or "capability invocation failed").strip()
        self.details = MappingProxyType(_safe_details(details or {}))

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            result["details"] = dict(self.details)
        return result


CapabilityInvokerError = CapabilityInvocationError


@dataclass(frozen=True, slots=True)
class _Contract:
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    required_scopes: tuple[str, ...]
    required_roles: tuple[str, ...]
    side_effect: bool
    requires_confirmation: bool
    idempotency_required: bool
    confirmation_ttl_seconds: int
    contract_hash: str

    @property
    def gated(self) -> bool:
        return self.side_effect or self.requires_confirmation


@dataclass(frozen=True, slots=True)
class _InputIdentity:
    plain_inputs: Any
    structured_hash: str
    outline: Mapping[str, Any]
    managed_override_hash: str
    payload_hash: str
    idempotency_request_hash: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _code(value: Any) -> str:
    normalized = str(value or "capability_invocation_error").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", normalized):
        return "capability_invocation_error"
    return normalized


def _safe_field_name(value: Any) -> str:
    """Return an audit-safe field label without persisting attacker text."""

    text = str(value)
    digest = canonical_hash(text, domain="capability-field-name-v1")[:12]
    if _SECRET_KEY_RE.search(text):
        return f"[sensitive-field:{digest}]"
    if not _SAFE_FIELD_NAME_RE.fullmatch(text):
        return f"[field:{digest}]"
    return text


def _safe_details(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        normalized_key = str(key or "").strip()
        if not normalized_key:
            continue
        if item is None or isinstance(item, (str, bool, int, float)):
            result[normalized_key] = item
        elif isinstance(item, (tuple, list, set, frozenset)):
            result[normalized_key] = tuple(str(part) for part in item)
    return result


def _plain(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _outline(value: Any, *, depth: int = 0) -> Mapping[str, Any]:
    if depth >= 8:
        return {"type": "truncated"}
    if isinstance(value, Mapping):
        fields = {
            _safe_field_name(key): _outline(item, depth=depth + 1)
            for key, item in sorted(value.items(), key=lambda part: str(part[0]))[:256]
        }
        result: dict[str, Any] = {"type": "object", "fields": fields}
        if len(value) > len(fields):
            result["truncated"] = True
        return result
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        item_types = sorted({_json_type(item) for item in value[:256]})
        return {"type": "array", "item_types": item_types}
    return {"type": _json_type(value)}


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return "array"
    return type(value).__name__.lower()


def _sanitize_output(value: Any) -> Any:
    plain = _plain(value)

    def visit(item: Any) -> Any:
        if isinstance(item, dict):
            result: dict[str, Any] = {}
            for key, child in item.items():
                result[key] = "[redacted]" if _SECRET_KEY_RE.search(key) else visit(child)
            return result
        if isinstance(item, list):
            return [visit(child) for child in item]
        return item

    sanitized = visit(plain)
    if len(canonical_json(sanitized).encode("utf-8")) > _MAX_AUDITED_OUTPUT_BYTES:
        raise ValueError("provider output exceeds the auditable result limit")
    return sanitized


def _tokens(value: Any, label: str) -> tuple[str, ...]:
    if value in (None, "", (), []):
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise CapabilityInvocationError(
            "provider_contract_invalid",
            f"provider {label} must be a list",
        )
    normalized: set[str] = set()
    for item in value:
        token = str(item or "").strip().lower()
        if not token or len(token) > 128 or not re.fullmatch(
            r"[a-z][a-z0-9_.:-]{0,127}", token
        ):
            raise CapabilityInvocationError(
                "provider_contract_invalid",
                f"provider {label} contains an invalid value",
            )
        normalized.add(token)
    return tuple(sorted(normalized))


def _bool(document: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = document.get(key, default)
    if not isinstance(value, bool):
        raise CapabilityInvocationError(
            "provider_contract_invalid",
            f"provider contract field {key} must be boolean",
        )
    return value


def _confirmation_ttl(document: Mapping[str, Any]) -> int:
    value = document.get(
        "confirmation_ttl_seconds",
        _DEFAULT_CONFIRMATION_TTL_SECONDS,
    )
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < _MIN_CONFIRMATION_TTL_SECONDS
        or value > _MAX_CONFIRMATION_TTL_SECONDS
    ):
        raise CapabilityInvocationError(
            "provider_contract_invalid",
            "provider confirmation_ttl_seconds is invalid",
        )
    return value


def _reject_remote_references(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef"} and isinstance(item, str):
                if not item.startswith("#"):
                    raise CapabilityInvocationError(
                        "provider_contract_invalid",
                        "provider input schema may use only local references",
                    )
            _reject_remote_references(item)
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for item in value:
            _reject_remote_references(item)


def _provider_contract(
    provider: CapabilityProvider,
    capability: CapabilityRef,
    deployment: ResolvedDeployment,
) -> _Contract:
    try:
        raw = provider.contract(capability, deployment)
    except Exception:  # noqa: BLE001 - provider internals are never exposed.
        raise CapabilityInvocationError(
            "provider_contract_failed",
            "capability provider contract could not be loaded",
        ) from None
    if not isinstance(raw, Mapping):
        raise CapabilityInvocationError(
            "provider_contract_invalid",
            "capability provider contract must be an object",
        )
    schema = raw.get("input_schema")
    if not isinstance(schema, Mapping):
        raise CapabilityInvocationError(
            "provider_contract_invalid",
            "capability provider contract must declare input_schema",
        )
    plain_schema = _plain(schema)
    output_schema = _definition_output_schema(deployment, capability)
    _reject_remote_references(plain_schema)
    _reject_remote_references(output_schema)
    try:
        Draft202012Validator.check_schema(plain_schema)
        Draft202012Validator.check_schema(output_schema)
    except SchemaError:
        raise CapabilityInvocationError(
            "provider_contract_invalid",
            "capability input or output schema is invalid",
        ) from None
    idempotency_required = _bool(raw, "idempotency_required")
    requires_confirmation = _bool(raw, "requires_confirmation")
    side_effect = _bool(raw, "side_effect")
    if side_effect and (not requires_confirmation or not idempotency_required):
        raise CapabilityInvocationError(
            "provider_contract_invalid",
            "side-effecting capability must require confirmation and idempotency",
        )
    normalized = {
        "confirmation_ttl_seconds": _confirmation_ttl(raw),
        "idempotency_required": idempotency_required,
        "input_schema": plain_schema,
        "output_schema": output_schema,
        "required_roles": list(_tokens(raw.get("required_roles"), "required_roles")),
        "required_scopes": list(
            _tokens(raw.get("required_scopes"), "required_scopes")
        ),
        "requires_confirmation": requires_confirmation,
        "side_effect": side_effect,
    }
    provider_version = str(getattr(provider, "provider_version", "") or "").strip()
    if provider_version:
        normalized["provider_identity"] = {
            "key": str(getattr(provider, "provider_key", "") or "").strip().lower(),
            "version": provider_version,
        }
    return _Contract(
        input_schema=MappingProxyType(plain_schema),
        output_schema=MappingProxyType(output_schema),
        required_scopes=tuple(normalized["required_scopes"]),
        required_roles=tuple(normalized["required_roles"]),
        side_effect=normalized["side_effect"],
        requires_confirmation=normalized["requires_confirmation"],
        idempotency_required=normalized["idempotency_required"],
        confirmation_ttl_seconds=normalized["confirmation_ttl_seconds"],
        contract_hash=canonical_hash(normalized, domain="capability-provider-contract-v1"),
    )


def _safe_schema_path(path: Sequence[Any]) -> tuple[str, ...]:
    return tuple(
        str(part) if isinstance(part, int) and not isinstance(part, bool)
        else _safe_field_name(part)
        for part in path
    )


def _validate_schema(contract: _Contract, inputs: Any) -> Any:
    serialized_inputs = canonical_json(inputs)
    if len(serialized_inputs.encode("utf-8")) > _MAX_STRUCTURED_INPUT_BYTES:
        raise CapabilityInvocationError(
            "structured_input_too_large",
            "structured capability inputs exceed the accepted size limit",
        )
    plain_inputs = json.loads(serialized_inputs)
    validator = Draft202012Validator(dict(contract.input_schema))
    errors = sorted(
        validator.iter_errors(plain_inputs),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    )
    if errors:
        first = errors[0]
        raise CapabilityInvocationError(
            "input_schema_invalid",
            "structured capability inputs do not satisfy the provider contract",
            details={
                "path": _safe_schema_path(tuple(first.absolute_path)),
                "rule": str(first.validator or "schema"),
            },
        )
    return plain_inputs


def _validate_output_schema(contract: _Contract, output: Any) -> None:
    validator = Draft202012Validator(dict(contract.output_schema))
    errors = sorted(
        validator.iter_errors(output),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    )
    if errors:
        first = errors[0]
        raise CapabilityInvocationError(
            "output_schema_invalid",
            "capability output does not satisfy the published definition contract",
            details={
                "path": _safe_schema_path(tuple(first.absolute_path)),
                "rule": str(first.validator or "schema"),
            },
        )


def _managed_override_facts(request: Request) -> list[dict[str, Any]]:
    return [
        {
            "binding_kind": item.binding_kind,
            "binding_key": item.binding_key,
            "port_key": item.port_key,
            "reference_id": item.reference_id,
            "signature": item.signature,
            "selector": item.selector,
            "version_id": item.version_id,
        }
        for item in request.binding_overrides
    ]


def _input_identity(request: Request, contract: _Contract) -> _InputIdentity:
    plain_inputs = _validate_schema(contract, request.inputs)
    structured_hash = canonical_hash(
        plain_inputs,
        domain="capability-structured-input-v1",
    )
    managed_override_hash = canonical_hash(
        _managed_override_facts(request),
        domain="capability-managed-override-intent-v1",
    )
    payload_hash = canonical_hash(
        {
            "managed_override_hash": managed_override_hash,
            "structured_input_hash": structured_hash,
        },
        domain="capability-invocation-payload-v1",
    )
    idempotency_request_hash = canonical_hash(
        {"mode": request.mode, "payload_hash": payload_hash},
        domain="capability-idempotency-request-v1",
    )
    return _InputIdentity(
        plain_inputs=plain_inputs,
        structured_hash=structured_hash,
        outline=_outline(plain_inputs),
        managed_override_hash=managed_override_hash,
        payload_hash=payload_hash,
        idempotency_request_hash=idempotency_request_hash,
    )


def _require_runtime_scope(
    db: Session,
    deployment: ResolvedDeployment,
    actor: Actor,
    invocation_source: str,
) -> None:
    if actor.tenant_id != deployment.tenant_id:
        raise CapabilityInvocationError(
            "principal_scope_mismatch",
            "authenticated actor and deployment belong to different tenants",
        )
    db_tenant = getattr(db, "info", {}).get("tenant_id")
    if db_tenant not in (None, "") and str(db_tenant) != deployment.tenant_id:
        raise CapabilityInvocationError(
            "database_scope_mismatch",
            "database tenant scope does not match the resolved deployment",
        )
    if invocation_source not in _INVOCATION_SOURCES:
        raise CapabilityInvocationError(
            "invalid_invocation_source",
            "invocation source is unsupported",
        )


def _require_contract_scope(
    actor: Actor,
    request: Request,
    contract: _Contract,
) -> None:
    missing_scopes = sorted(set(contract.required_scopes) - set(actor.scopes))
    if missing_scopes:
        raise CapabilityInvocationError(
            "capability_scope_forbidden",
            "authenticated actor lacks required capability scopes",
            details={"missing_scopes": missing_scopes},
        )
    missing_roles = sorted(set(contract.required_roles) - set(actor.roles))
    if missing_roles:
        raise CapabilityInvocationError(
            "capability_role_forbidden",
            "authenticated actor lacks required capability roles",
            details={"missing_roles": missing_roles},
        )
    if contract.idempotency_required and not request.idempotency_key:
        raise CapabilityInvocationError(
            "idempotency_key_required",
            "capability contract requires an idempotency key",
        )


def _require_request_correlation(request: Request) -> None:
    if not request.correlation_id:
        raise CapabilityInvocationError(
            "correlation_id_required",
            "capability request must provide a correlation id",
        )


def _request_id(request: Request) -> str:
    return request.request_id or uuid4().hex


def _read(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _capability_resources(definition: Any, kind: str) -> Any:
    groups = {
        "action": "actions",
        "event": "events",
        "function": "functions",
        "provider": "capabilities",
        "query": "queries",
        "workflow": "workflows",
    }
    group = groups.get(kind, f"{kind}s")
    resources = _read(definition, group)
    if resources is None and kind == "provider":
        resources = _read(definition, "providers")
    return resources


def _find_capability_resource(resources: Any, resource_id: str) -> Any | None:
    if isinstance(resources, Mapping):
        direct = resources.get(resource_id)
        if direct is not None:
            return direct
        values = resources.values()
    elif isinstance(resources, Sequence) and not isinstance(
        resources, (str, bytes, bytearray)
    ):
        values = resources
    else:
        return None
    for resource in values:
        identities = {
            str(_read(resource, field) or "").strip()
            for field in ("id", "key", "api_name", "resource_id")
        }
        if resource_id in identities:
            return resource
    return None


def _definition_output_schema(
    deployment: ResolvedDeployment,
    capability: CapabilityRef,
) -> dict[str, Any]:
    resources = _capability_resources(deployment.definition, capability.kind)
    resource = _find_capability_resource(resources, capability.resource_id)
    if resource is None:
        raise CapabilityInvocationError(
            "capability_not_found",
            "capability is not present in the resolved deployment definition",
        )
    if capability.kind == "rule":
        return {
            "type": "object",
            "properties": {
                "rule_id": {"type": "string"},
                "rule_name": {"type": "string"},
                "matched": {"type": "boolean"},
                "severity": {
                    "type": "string",
                    "enum": ["info", "warning", "critical"],
                },
                "action_on_match": {"type": "string"},
                "trigger_action_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "trigger_actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action_id": {"type": "string"},
                            "action_name": {"type": "string"},
                            "status": {"type": "string"},
                            "executable": {"type": "boolean"},
                            "blocked_reasons": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "requires_confirmation": {"type": "boolean"},
                            "input_schema": {"type": "object"},
                            "precondition": {"type": "string"},
                            "postcondition": {"type": "string"},
                        },
                        "required": [
                            "action_id",
                            "status",
                            "executable",
                            "blocked_reasons",
                        ],
                        "additionalProperties": False,
                    },
                },
                "side_effects_executed": {"const": False},
            },
            "required": [
                "rule_id",
                "rule_name",
                "matched",
                "severity",
                "action_on_match",
                "trigger_action_ids",
                "trigger_actions",
                "side_effects_executed",
            ],
            "additionalProperties": False,
        }
    schema = _read(resource, "output_schema", {}) or {}
    if not isinstance(schema, Mapping):
        raise CapabilityInvocationError(
            "provider_contract_invalid",
            "published capability output schema must be an object",
        )
    return _plain(schema)


def resolve_provider_binding(
    deployment: ResolvedDeployment,
    capability: CapabilityRef,
) -> str:
    """Resolve the server-authoritative provider key for one capability."""

    if not isinstance(deployment, ResolvedDeployment) or not isinstance(
        capability, CapabilityRef
    ):
        raise CapabilityInvocationError(
            "invalid_provider_binding_request",
            "provider binding requires a resolved deployment and capability",
        )
    definition = deployment.definition
    resources = _capability_resources(definition, capability.kind)
    if resources is None:
        raise CapabilityInvocationError(
            "provider_binding_missing",
            "resolved definition has no authoritative capability catalog",
        )
    resource = _find_capability_resource(
        resources,
        capability.resource_id,
    )
    if resource is None:
        raise CapabilityInvocationError(
            "capability_not_found",
            "capability is not present in the resolved deployment definition",
        )
    runtime_config = _read(resource, "runtime_config")
    provider_runtime = (
        capability.kind == "function"
        and str(_read(resource, "runtime_kind", "") or "").strip().lower()
        == "provider"
    )
    provider_key = ""
    if provider_runtime and isinstance(runtime_config, Mapping):
        provider_key = str(runtime_config.get("provider_key") or "").strip().lower()
    if not provider_key and not provider_runtime:
        provider_key = builtin_provider_key(capability.kind) or ""
    if not provider_key:
        provider_key = str(_read(resource, "provider_key") or "").strip().lower()
    if not provider_key:
        for field in ("runtime_config", "executor_config", "config"):
            config = _read(resource, field)
            if isinstance(config, Mapping):
                provider_key = str(config.get("provider_key") or "").strip().lower()
                if provider_key:
                    break
    if not provider_key:
        bindings = _read(definition, "capability_provider_keys")
        if isinstance(bindings, Mapping):
            provider_key = str(
                bindings.get(
                    f"{capability.kind}:{capability.resource_id}",
                    bindings.get(capability.resource_id, ""),
                )
                or ""
            ).strip().lower()
    if not provider_key:
        raise CapabilityInvocationError(
            "provider_binding_missing",
            "resolved capability has no trusted provider binding",
        )
    requested_key = capability.provider_key
    if requested_key is not None and requested_key != provider_key:
        raise CapabilityInvocationError(
            "provider_binding_mismatch",
            "requested provider does not match the resolved capability definition",
        )
    return provider_key


def _expected_provider_version(
    deployment: ResolvedDeployment,
    capability: CapabilityRef,
) -> str:
    resources = _capability_resources(deployment.definition, capability.kind)
    resource = _find_capability_resource(resources, capability.resource_id)
    if resource is None:
        return ""
    for field in ("runtime_config", "executor_config", "config"):
        config = _read(resource, field)
        if isinstance(config, Mapping):
            version = str(config.get("provider_version") or "").strip()
            if version:
                return version
    return str(_read(resource, "provider_version", "") or "").strip()


def _provider_version_for_resolution(
    deployment: ResolvedDeployment,
    capability: CapabilityRef,
    provider_key: str,
) -> str | None:
    expected = _expected_provider_version(deployment, capability)
    if expected:
        return expected

    resources = _capability_resources(deployment.definition, capability.kind)
    resource = _find_capability_resource(resources, capability.resource_id)
    explicit_provider_runtime = (
        resource is not None
        and str(_read(resource, "runtime_kind", "") or "").strip().lower()
        == "provider"
    )
    static_builtin_key = builtin_provider_key(capability.kind)
    if (
        not explicit_provider_runtime
        and static_builtin_key is not None
        and provider_key == static_builtin_key
    ):
        # Legacy built-in definitions predate version metadata. This exception
        # applies only to the platform's static kind-to-provider binding.
        return None
    raise CapabilityInvocationError(
        "provider_version_missing",
        "resolved capability Provider version is missing",
    )


def _require_provider_version(
    deployment: ResolvedDeployment,
    capability: CapabilityRef,
    provider: CapabilityProvider,
) -> None:
    expected = _expected_provider_version(deployment, capability)
    if not expected:
        return
    actual = str(getattr(provider, "provider_version", "") or "").strip()
    if actual != expected:
        raise CapabilityInvocationError(
            "provider_version_mismatch",
            "registered Provider version does not match the resolved definition",
        )


def _raise_provider_resolution_error(
    registry: CapabilityProviderRegistry,
    provider_key: str,
    expected_version: str | None,
) -> None:
    key_is_registered = any(
        registered_key == provider_key
        for registered_key, _registered_version in registry.identities()
    )
    if expected_version and key_is_registered:
        raise CapabilityInvocationError(
            "provider_version_mismatch",
            "registered Provider version does not match the resolved definition",
        )
    raise CapabilityInvocationError(
        "provider_not_registered",
        "capability provider is not registered",
    )


def resolve_capability_contract(
    db: Session,
    deployment: ResolvedDeployment,
    capability: CapabilityRef,
    *,
    registry: CapabilityProviderRegistry | None = None,
) -> dict[str, Any]:
    """Return a normalized public contract without invoking the capability."""

    provider_key = resolve_provider_binding(deployment, capability)
    trusted_registry = registry or default_provider_registry
    expected_version = _provider_version_for_resolution(
        deployment,
        capability,
        provider_key,
    )
    try:
        provider = trusted_registry.resolve(
            provider_key,
            expected_version or None,
        )
    except CapabilityRegistryError:
        _raise_provider_resolution_error(
            trusted_registry,
            provider_key,
            expected_version,
        )
    _require_provider_version(deployment, capability, provider)
    try:
        provider = bind_provider(provider, db)
    except CapabilityRegistryError:
        raise CapabilityInvocationError(
            "provider_binding_failed",
            "capability provider could not bind the discovery context",
        ) from None
    contract = _provider_contract(provider, capability, deployment)
    return {
        "confirmation_ttl_seconds": contract.confirmation_ttl_seconds,
        "contract_hash": contract.contract_hash,
        "idempotency_required": contract.idempotency_required,
        "input_schema": _plain(contract.input_schema),
        "output_schema": _plain(contract.output_schema),
        "required_roles": list(contract.required_roles),
        "required_scopes": list(contract.required_scopes),
        "requires_confirmation": contract.requires_confirmation,
        "side_effect": contract.side_effect,
    }


def _idempotency_statement(
    deployment: ResolvedDeployment,
    request: Request,
) -> Any:
    return select(CapabilityInvocation).where(
        CapabilityInvocation.tenant_id == deployment.tenant_id,
        CapabilityInvocation.scenario_id == deployment.scenario_id,
        CapabilityInvocation.capability_kind == request.capability.kind,
        CapabilityInvocation.capability_key == request.capability.resource_id,
        CapabilityInvocation.definition_hash == deployment.definition_hash,
        CapabilityInvocation.deployment_fingerprint == deployment.fingerprint,
        CapabilityInvocation.idempotency_key == request.idempotency_key,
    )


def _find_idempotent(
    db: Session,
    deployment: ResolvedDeployment,
    request: Request,
    *,
    lock: bool,
) -> CapabilityInvocation | None:
    if not request.idempotency_key:
        return None
    statement = _idempotency_statement(deployment, request)
    if lock:
        statement = statement.with_for_update()
    return db.execute(statement).scalar_one_or_none()


def _find_request_id(
    db: Session,
    deployment: ResolvedDeployment,
    request_id: str,
    *,
    lock: bool,
) -> CapabilityInvocation | None:
    statement = select(CapabilityInvocation).where(
        CapabilityInvocation.tenant_id == deployment.tenant_id,
        CapabilityInvocation.request_id == request_id,
    )
    if lock:
        statement = statement.with_for_update()
    return db.execute(statement).scalar_one_or_none()


def _assert_idempotent_match(
    invocation: CapabilityInvocation,
    actor: Actor,
    request: Request,
    identity: _InputIdentity,
    contract: _Contract,
) -> None:
    document = invocation.request_document if isinstance(invocation.request_document, dict) else {}
    if document.get("idempotency_request_hash") != identity.idempotency_request_hash:
        raise CapabilityInvocationError(
            "idempotency_conflict",
            "idempotency key was already used with different inputs",
        )
    if document.get("provider_contract_hash") != contract.contract_hash:
        raise CapabilityInvocationError(
            "idempotency_contract_conflict",
            "provider contract changed after the idempotent invocation",
        )
    if invocation.principal_type != actor.actor_type or invocation.principal_id != actor.principal_id:
        raise CapabilityInvocationError(
            "idempotency_scope_conflict",
            "idempotency key belongs to another authenticated principal",
        )


def _assert_replay_request_id(
    invocation: CapabilityInvocation,
    request: Request,
    request_id_owner: CapabilityInvocation | None,
) -> None:
    if request.request_id is None:
        return
    if request_id_owner is not None and request_id_owner.id != invocation.id:
        raise CapabilityInvocationError(
            "request_id_conflict",
            "request id was already used for another invocation",
        )
    if request_id_owner is None:
        raise CapabilityInvocationError(
            "idempotency_request_id_conflict",
            "idempotent retry must use the original request id",
        )


def _receipt(
    invocation: CapabilityInvocation,
    request: Request,
    *,
    replayed: bool,
) -> Receipt:
    result = invocation.result_document if isinstance(invocation.result_document, dict) else {}
    output = result.get("output", {})
    confirmation = result.get("confirmation", {})
    if not isinstance(confirmation, Mapping):
        confirmation = {}
    return Receipt(
        invocation_id=invocation.id,
        status=invocation.status,
        capability=request.capability,
        definition_hash=invocation.definition_hash,
        deployment_fingerprint=invocation.deployment_fingerprint,
        data_context_fingerprint=invocation.data_context_fingerprint,
        output=output,
        audit_ref={
            "invocation_id": invocation.id,
            "replayed": replayed,
        },
        confirmation=confirmation,
        error_code=invocation.error_code or None,
        error_message=invocation.error_message or "",
    )


def _final_input_hash(
    resolution: RuntimeInputResolution,
    identity: _InputIdentity,
    contract: _Contract,
    request: Request,
    deployment: ResolvedDeployment,
) -> str:
    return canonical_hash(
        {
            "capability": {
                "kind": request.capability.kind,
                "key": request.capability.resource_id,
            },
            "definition_hash": deployment.definition_hash,
            "deployment_fingerprint": deployment.fingerprint,
            "managed_input_hash": resolution.invocation.input_hash,
            "provider_contract_hash": contract.contract_hash,
            "runtime_data_context_fingerprint": resolution.runtime_data_context.fingerprint,
            "structured_input_hash": identity.structured_hash,
        },
        domain="capability-final-input-v1",
    )


def _update_request_audit(
    invocation: CapabilityInvocation,
    identity: _InputIdentity,
    contract: _Contract,
    final_input_hash: str,
    request: Request,
) -> None:
    document = dict(invocation.request_document or {})
    document.update(
        {
            "idempotency_request_hash": identity.idempotency_request_hash,
            "managed_override_hash": identity.managed_override_hash,
            "mode": request.mode,
            "payload_hash": identity.payload_hash,
            "provider_contract_hash": contract.contract_hash,
            "structured_inputs": {
                "hash": identity.structured_hash,
                "outline": dict(identity.outline),
            },
        }
    )
    invocation.request_document = document
    invocation.input_hash = final_input_hash


def _result_document(
    output: Any,
    contract: _Contract,
    *,
    validate_output: bool = True,
) -> dict[str, Any]:
    try:
        safe_output = _sanitize_output(output)
    except Exception:  # noqa: BLE001 - Provider values never enter public errors.
        raise CapabilityInvocationError(
            "provider_output_invalid",
            "capability provider returned an invalid or oversized output",
        ) from None
    if validate_output:
        _validate_output_schema(contract, safe_output)
    return {
        "output": safe_output,
        "output_hash": canonical_hash(
            safe_output,
            domain="capability-output-v1",
        ),
        "output_outline": dict(_outline(safe_output)),
    }


def _mark_failed(
    db: Session,
    invocation: CapabilityInvocation,
    *,
    code: str = "provider_execution_failed",
    message: str = "capability provider execution failed",
    confirmation: Mapping[str, Any] | None = None,
) -> None:
    invocation.status = "failed"
    invocation.completed_at = _now()
    invocation.error_code = _code(code)
    invocation.error_message = str(message or "capability invocation failed")[:4_000]
    result_document: dict[str, Any] = {
        "failure": {"code": invocation.error_code},
        "output": {},
    }
    if confirmation:
        result_document["confirmation"] = dict(confirmation)
    invocation.result_document = result_document
    db.flush()


def _invoke_provider(
    provider: CapabilityProvider,
    request: Request,
    actor: Actor,
    deployment: ResolvedDeployment,
    data_context: RuntimeDataContext,
) -> Any:
    result = provider.invoke(request, actor, deployment, data_context)
    if inspect.isawaitable(result):
        close = getattr(result, "close", None)
        if callable(close):
            close()
        raise TypeError("asynchronous provider returned to synchronous invoker")
    return result


def _preview_provider(
    provider: CapabilityProvider,
    request: Request,
    actor: Actor,
    deployment: ResolvedDeployment,
    data_context: RuntimeDataContext,
    contract: _Contract,
) -> Any:
    preview = getattr(provider, "preview", None)
    if not callable(preview):
        return {
            "capability": {
                "key": request.capability.resource_id,
                "kind": request.capability.kind,
            },
            "data_context_fingerprint": data_context.fingerprint,
            "preview": True,
            "requires_confirmation": contract.gated,
            "side_effect": contract.side_effect,
        }
    result = preview(request, actor, deployment, data_context)
    if inspect.isawaitable(result):
        close = getattr(result, "close", None)
        if callable(close):
            close()
        raise TypeError("asynchronous provider returned to synchronous invoker")
    return result


def _recover_provider(
    provider: CapabilityProvider,
    request: Request,
    actor: Actor,
    deployment: ResolvedDeployment,
    data_context: RuntimeDataContext,
) -> ProviderRecovery:
    """Reconcile a durable running invocation without redispatching it."""

    recover = getattr(provider, "recover", None)
    if not callable(recover):
        return ProviderRecovery(state="indeterminate")
    try:
        result = recover(request, actor, deployment, data_context)
        if inspect.isawaitable(result):
            close = getattr(result, "close", None)
            if callable(close):
                close()
            return ProviderRecovery(state="indeterminate")
    except Exception:  # noqa: BLE001 - reconciliation must remain fail closed.
        return ProviderRecovery(state="indeterminate")
    if not isinstance(result, ProviderRecovery):
        return ProviderRecovery(state="indeterminate")
    return result


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc_timestamp(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text or len(text) > 64:
        raise CapabilityInvocationError(
            "confirmation_state_invalid",
            "server-issued confirmation expiry is invalid",
        )
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise CapabilityInvocationError(
            "confirmation_state_invalid",
            "server-issued confirmation expiry is invalid",
        ) from None
    if parsed.tzinfo is None:
        raise CapabilityInvocationError(
            "confirmation_state_invalid",
            "server-issued confirmation expiry is invalid",
        )
    return parsed.astimezone(timezone.utc)


def _expire_awaiting_confirmation(
    db: Session,
    invocation: CapabilityInvocation,
) -> bool:
    if invocation.status != "awaiting_confirmation":
        return False
    result = (
        invocation.result_document
        if isinstance(invocation.result_document, dict)
        else {}
    )
    stored_confirmation = result.get("confirmation", {})
    if not isinstance(stored_confirmation, Mapping):
        stored_confirmation = {}
    closed_confirmation = dict(stored_confirmation)
    closed_confirmation.update({"confirmed": False, "required": False})
    try:
        expires_at = _parse_utc_timestamp(stored_confirmation.get("expires_at"))
    except CapabilityInvocationError:
        _mark_failed(
            db,
            invocation,
            code="confirmation_state_invalid",
            message="server-issued confirmation state is invalid",
            confirmation=closed_confirmation,
        )
        return True
    now = _now()
    if now < expires_at:
        return False
    closed_confirmation["expired"] = True
    expired_result = dict(result)
    expired_result["confirmation"] = closed_confirmation
    invocation.result_document = expired_result
    invocation.status = "timed_out"
    invocation.completed_at = now
    invocation.error_code = "confirmation_expired"
    invocation.error_message = "server-issued confirmation expired"
    db.flush()
    return True


def _confirmation_token(
    invocation: CapabilityInvocation,
    *,
    expires_at: str,
    output_hash: str,
    contract_hash: str,
) -> str:
    return canonical_hash(
        {
            "capability_key": invocation.capability_key,
            "capability_kind": invocation.capability_kind,
            "definition_hash": invocation.definition_hash,
            "deployment_fingerprint": invocation.deployment_fingerprint,
            "expires_at": expires_at,
            "input_hash": invocation.input_hash,
            "invocation_id": invocation.id,
            "output_hash": output_hash,
            "principal_id": invocation.principal_id,
            "principal_type": invocation.principal_type,
            "provider_contract_hash": contract_hash,
        },
        domain="capability-confirmation-v1",
    )


def _runtime_context_from_audit(
    db: Session,
    invocation: CapabilityInvocation,
) -> RuntimeDataContext:
    rows = tuple(
        db.execute(
            select(RunInputBinding)
            .where(RunInputBinding.invocation_id == invocation.id)
            .order_by(RunInputBinding.capability_port_id, RunInputBinding.ordinal)
        )
        .scalars()
        .all()
    )
    handles: list[ResolvedDataHandle] = []
    for row in rows:
        if row.tenant_id != invocation.tenant_id or row.scenario_id != invocation.scenario_id:
            raise CapabilityContractError("runtime input audit scope mismatch")
        if row.status != "ready" or not row.content_hash:
            raise CapabilityContractError("runtime input audit is not ready")
        document = row.binding_document if isinstance(row.binding_document, dict) else {}
        port_key = str(document.get("port_key") or "")
        if row.source_kind == "dataset_version":
            reference_id = row.source_dataset_version_id
            version_id = row.resolved_dataset_version_id
        elif row.source_kind == "dataset_head":
            reference_id = row.dataset_head_id
            version_id = row.resolved_dataset_version_id
        elif row.source_kind == "asset_version":
            reference_id = row.asset_version_id
            version_id = row.asset_version_id
        elif row.source_kind == "connector_binding":
            reference_id = row.connector_binding_id
            version_id = None
        else:
            raise CapabilityContractError("runtime input audit kind is unsupported")
        if not port_key or not reference_id:
            raise CapabilityContractError("runtime input audit reference is incomplete")
        handles.append(
            ResolvedDataHandle(
                port_key=port_key,
                binding_kind=row.source_kind,
                reference_id=reference_id,
                signature=row.content_hash,
                version_id=version_id,
            )
        )
    return RuntimeDataContext(tuple(handles))


def runtime_context_from_invocation_audit(
    db: Session,
    invocation: CapabilityInvocation,
) -> RuntimeDataContext:
    """Rebuild a credential-free context solely from immutable input audit rows."""

    return _runtime_context_from_audit(db, invocation)


class CapabilityInvoker:
    """Single synchronous entry point for trusted capability providers."""

    def __init__(
        self,
        registry: CapabilityProviderRegistry | None = None,
    ) -> None:
        self._registry = registry or default_provider_registry

    def invoke(
        self,
        db: Session,
        deployment: ResolvedDeployment,
        actor: Actor,
        request: Request,
        *,
        invocation_source: str,
    ) -> Receipt:
        if not isinstance(deployment, ResolvedDeployment):
            raise CapabilityInvocationError(
                "invalid_resolved_deployment",
                "capability invocation requires a resolved deployment",
            )
        if not isinstance(actor, Actor):
            raise CapabilityInvocationError(
                "invalid_invocation_principal",
                "capability invocation requires an authenticated actor",
            )
        if not isinstance(request, Request):
            raise CapabilityInvocationError(
                "invalid_invocation_request",
                "capability invocation requires an immutable request",
            )
        if request.mode not in _MODES:
            raise CapabilityInvocationError(
                "invalid_invocation_mode",
                "invocation mode must be execute, preview, or confirm",
            )
        _require_request_correlation(request)
        if request.mode != "confirm" and request.confirmation:
            raise CapabilityInvocationError(
                "unexpected_confirmation",
                "confirmation data is accepted only in confirm mode",
            )
        try:
            require_request_matches_deployment(request, deployment)
        except DeploymentResolutionError as exc:
            raise CapabilityInvocationError(
                "invocation_deployment_changed",
                str(exc),
            ) from None
        _require_runtime_scope(
            db,
            deployment,
            actor,
            invocation_source,
        )
        provider_key = resolve_provider_binding(deployment, request.capability)
        expected_version = _provider_version_for_resolution(
            deployment,
            request.capability,
            provider_key,
        )
        try:
            provider = self._registry.resolve(
                provider_key,
                expected_version or None,
            )
        except CapabilityRegistryError:
            _raise_provider_resolution_error(
                self._registry,
                provider_key,
                expected_version,
            )
        _require_provider_version(deployment, request.capability, provider)
        try:
            provider = bind_provider(provider, db)
        except CapabilityRegistryError:
            raise CapabilityInvocationError(
                "provider_binding_failed",
                "capability provider could not bind the invocation context",
            ) from None
        contract = _provider_contract(provider, request.capability, deployment)
        _require_contract_scope(
            actor,
            request,
            contract,
        )
        identity = _input_identity(request, contract)

        if request.mode == "execute" and contract.gated:
            raise CapabilityInvocationError(
                "preview_required",
                "capability requires a server-issued preview before confirmation",
            )
        if request.mode == "confirm":
            if not contract.gated:
                raise CapabilityInvocationError(
                    "confirmation_not_supported",
                    "capability contract does not require confirmation",
                )
            return self._confirm(
                db,
                deployment,
                actor,
                request,
                provider,
                contract,
                identity,
            )

        request_id_owner = (
            _find_request_id(
                db,
                deployment,
                request.request_id,
                lock=True,
            )
            if request.request_id is not None
            else None
        )
        existing = _find_idempotent(
            db,
            deployment,
            request,
            lock=True,
        )
        if existing is not None:
            _assert_replay_request_id(existing, request, request_id_owner)
            _assert_idempotent_match(existing, actor, request, identity, contract)
            _expire_awaiting_confirmation(db, existing)
            return _receipt(existing, request, replayed=True)

        runtime_request_id = _request_id(request)
        if request_id_owner is not None:
            raise CapabilityInvocationError(
                "request_id_conflict",
                "request id was already used for another invocation",
            )

        try:
            with db.begin_nested():
                resolution = resolve_runtime_inputs(
                    db,
                    request=request,
                    deployment=deployment,
                    actor=actor,
                    invocation_source=invocation_source,
                    request_id=runtime_request_id,
                    agent_id=actor.agent_id,
                    requested_by_user_id=actor.user_id,
                )
        except RuntimeInputResolutionError as exc:
            raise CapabilityInvocationError(
                exc.code,
                exc.message,
                details=exc.details,
            ) from None
        except IntegrityError:
            request_id_owner = (
                _find_request_id(
                    db,
                    deployment,
                    runtime_request_id,
                    lock=True,
                )
                if request.request_id is not None
                else None
            )
            existing = _find_idempotent(
                db,
                deployment,
                request,
                lock=True,
            )
            if existing is None:
                if request_id_owner is not None:
                    raise CapabilityInvocationError(
                        "request_id_conflict",
                        "request id was already used for another invocation",
                    ) from None
                raise CapabilityInvocationError(
                    "invocation_audit_conflict",
                    "capability invocation audit could not be created",
                ) from None
            _assert_replay_request_id(existing, request, request_id_owner)
            _assert_idempotent_match(existing, actor, request, identity, contract)
            _expire_awaiting_confirmation(db, existing)
            return _receipt(existing, request, replayed=True)

        invocation = resolution.invocation
        _update_request_audit(
            invocation,
            identity,
            contract,
            _final_input_hash(
                resolution,
                identity,
                contract,
                request,
                deployment,
            ),
            request,
        )
        invocation.status = "running"
        invocation.started_at = _now()
        invocation.error_code = ""
        invocation.error_message = ""
        db.flush()
        try:
            if request.mode == "preview":
                output = _preview_provider(
                    provider,
                    request,
                    actor,
                    deployment,
                    resolution.runtime_data_context,
                    contract,
                )
            else:
                output = _invoke_provider(
                    provider,
                    request,
                    actor,
                    deployment,
                    resolution.runtime_data_context,
                )
            result_document = _result_document(
                output,
                contract,
                validate_output=request.mode != "preview",
            )
        except CapabilityInvocationError as exc:
            _mark_failed(db, invocation, code=exc.code, message=exc.message)
            return _receipt(invocation, request, replayed=False)
        except Exception:  # noqa: BLE001 - return only the safe failed Receipt.
            _mark_failed(db, invocation)
            return _receipt(invocation, request, replayed=False)

        if request.mode == "preview" and contract.gated:
            expires_at = _utc_timestamp(
                _now() + timedelta(seconds=contract.confirmation_ttl_seconds)
            )
            token = _confirmation_token(
                invocation,
                expires_at=expires_at,
                output_hash=result_document["output_hash"],
                contract_hash=contract.contract_hash,
            )
            result_document["confirmation"] = {
                "confirmation_token": token,
                "expires_at": expires_at,
                "preview_invocation_id": invocation.id,
                "required": True,
            }
            invocation.status = "awaiting_confirmation"
            invocation.completed_at = None
        else:
            invocation.status = "succeeded"
            invocation.completed_at = _now()
        invocation.result_document = result_document
        db.flush()
        return _receipt(invocation, request, replayed=False)

    def _confirm(
        self,
        db: Session,
        deployment: ResolvedDeployment,
        actor: Actor,
        request: Request,
        provider: CapabilityProvider,
        contract: _Contract,
        identity: _InputIdentity,
    ) -> Receipt:
        confirmation = request.confirmation
        if not isinstance(confirmation, Mapping):
            raise CapabilityInvocationError(
                "invalid_confirmation",
                "confirmation must be a server-issued confirmation object",
            )
        if len(canonical_json(confirmation).encode("utf-8")) > _MAX_CONFIRMATION_BYTES:
            raise CapabilityInvocationError(
                "confirmation_too_large",
                "confirmation exceeds the accepted size limit",
            )
        unknown = sorted(
            _safe_field_name(item)
            for item in set(confirmation) - _CONFIRMATION_FIELDS
        )
        if unknown:
            raise CapabilityInvocationError(
                "invalid_confirmation",
                "confirmation contains unsupported fields",
                details={"fields": unknown},
            )
        preview_id = str(confirmation.get("preview_invocation_id") or "").strip()
        supplied_token = str(confirmation.get("confirmation_token") or "").strip()
        supplied_expiry = str(confirmation.get("expires_at") or "").strip()
        for field in ("required", "confirmed", "expired"):
            if field in confirmation and not isinstance(confirmation.get(field), bool):
                raise CapabilityInvocationError(
                    "invalid_confirmation",
                    "confirmation state metadata is invalid",
                )
        if (
            not _CONFIRMATION_ID_RE.fullmatch(preview_id)
            or not _CONFIRMATION_TOKEN_RE.fullmatch(supplied_token)
            or not supplied_expiry
            or len(supplied_expiry) > 64
        ):
            raise CapabilityInvocationError(
                "invalid_confirmation",
                "confirmation must reference a server-issued preview",
            )
        try:
            _parse_utc_timestamp(supplied_expiry)
        except CapabilityInvocationError:
            raise CapabilityInvocationError(
                "invalid_confirmation",
                "confirmation expiry is invalid",
            ) from None
        invocation = db.execute(
            select(CapabilityInvocation)
            .where(
                CapabilityInvocation.id == preview_id,
                CapabilityInvocation.tenant_id == deployment.tenant_id,
                CapabilityInvocation.scenario_id == deployment.scenario_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if invocation is None:
            raise CapabilityInvocationError(
                "confirmation_not_found",
                "server-issued preview is unavailable",
            )

        identity_matches = (
            invocation.tenant_id == deployment.tenant_id
            and invocation.scenario_id == deployment.scenario_id
            and invocation.environment == deployment.environment
            and invocation.capability_kind == request.capability.kind
            and invocation.capability_key == request.capability.resource_id
            and invocation.definition_hash == deployment.definition_hash
            and invocation.deployment_fingerprint == deployment.fingerprint
            and invocation.principal_type == actor.actor_type
            and invocation.principal_id == actor.principal_id
            and invocation.correlation_id == request.correlation_id
            and invocation.idempotency_key == request.idempotency_key
        )
        document = invocation.request_document if isinstance(invocation.request_document, dict) else {}
        result = invocation.result_document if isinstance(invocation.result_document, dict) else {}
        stored_confirmation = result.get("confirmation", {})
        if not isinstance(stored_confirmation, Mapping):
            stored_confirmation = {}
        if not identity_matches or document.get("mode") != "preview":
            raise CapabilityInvocationError(
                "confirmation_scope_mismatch",
                "confirmation does not match the authenticated preview scope",
            )
        if document.get("payload_hash") != identity.payload_hash:
            raise CapabilityInvocationError(
                "confirmation_input_mismatch",
                "confirmation inputs do not match the server-issued preview",
            )
        if document.get("provider_contract_hash") != contract.contract_hash:
            raise CapabilityInvocationError(
                "confirmation_contract_changed",
                "capability contract changed after preview",
            )
        if stored_confirmation.get("confirmation_token") != supplied_token:
            raise CapabilityInvocationError(
                "confirmation_token_mismatch",
                "confirmation token does not match the server-issued preview",
            )
        stored_expiry = str(stored_confirmation.get("expires_at") or "").strip()
        if stored_expiry != supplied_expiry:
            raise CapabilityInvocationError(
                "confirmation_expiry_mismatch",
                "confirmation expiry does not match the server-issued preview",
            )
        if confirmation.get("required", True) is not True:
            raise CapabilityInvocationError(
                "invalid_confirmation",
                "confirmation does not represent an awaiting server preview",
            )
        if invocation.status in {
            "succeeded",
            "failed",
            "timed_out",
            "cancelled",
            "rejected",
        }:
            return _receipt(invocation, request, replayed=True)
        if invocation.status not in {"awaiting_confirmation", "running"}:
            raise CapabilityInvocationError(
                "confirmation_state_conflict",
                "server-issued preview is not awaiting confirmation",
            )
        if (
            invocation.status == "awaiting_confirmation"
            and _expire_awaiting_confirmation(db, invocation)
        ):
            return _receipt(invocation, request, replayed=False)
        accepted_confirmation = dict(stored_confirmation)
        accepted_confirmation.update(
            {
                "confirmed": True,
                "required": False,
            }
        )
        is_recovery = invocation.status == "running"
        try:
            context = _runtime_context_from_audit(db, invocation)
        except CapabilityContractError:
            _mark_failed(
                db,
                invocation,
                code="runtime_input_audit_invalid",
                message="fixed runtime input audit is invalid",
                confirmation=accepted_confirmation,
            )
            return _receipt(
                invocation,
                request,
                replayed=is_recovery,
            )
        if context.fingerprint != invocation.data_context_fingerprint:
            _mark_failed(
                db,
                invocation,
                code="runtime_input_context_changed",
                message="fixed runtime input context no longer matches its audit",
                confirmation=accepted_confirmation,
            )
            return _receipt(
                invocation,
                request,
                replayed=is_recovery,
            )

        if is_recovery:
            recovery = _recover_provider(
                provider,
                request,
                actor,
                deployment,
                context,
            )
            if recovery.state == "succeeded":
                try:
                    recovered_result = _result_document(recovery.output, contract)
                except CapabilityInvocationError as exc:
                    _mark_failed(
                        db,
                        invocation,
                        code=exc.code,
                        message=exc.message,
                        confirmation=accepted_confirmation,
                    )
                    return _receipt(invocation, request, replayed=True)
                except Exception:  # noqa: BLE001 - persisted output is untrusted here.
                    recovery = ProviderRecovery(state="indeterminate")
                else:
                    recovered_result["confirmation"] = accepted_confirmation
                    invocation.result_document = recovered_result
                    invocation.status = "succeeded"
                    invocation.completed_at = _now()
                    invocation.error_code = ""
                    invocation.error_message = ""
                    db.flush()
                    return _receipt(invocation, request, replayed=True)
            if recovery.state == "failed":
                _mark_failed(
                    db,
                    invocation,
                    code=recovery.error_code or "provider_execution_failed",
                    message="capability provider reports a terminal execution failure",
                    confirmation=accepted_confirmation,
                )
                return _receipt(invocation, request, replayed=True)

            # The process may have stopped before or after an external effect.
            # Keep the claim non-terminal so a later reconciliation can observe
            # a durable provider result, but never call invoke() again here.
            running_result = dict(result)
            running_result["confirmation"] = accepted_confirmation
            running_result["recovery"] = {"state": "indeterminate"}
            invocation.result_document = running_result
            invocation.completed_at = None
            invocation.error_code = "execution_outcome_indeterminate"
            invocation.error_message = (
                "capability execution outcome requires provider reconciliation"
            )
            db.flush()
            return _receipt(invocation, request, replayed=True)

        invocation.status = "running"
        running_result = dict(result)
        running_result["confirmation"] = accepted_confirmation
        invocation.result_document = running_result
        db.flush()
        try:
            output = _invoke_provider(provider, request, actor, deployment, context)
            confirmed_result = _result_document(output, contract)
        except CapabilityInvocationError as exc:
            _mark_failed(
                db,
                invocation,
                code=exc.code,
                message=exc.message,
                confirmation=accepted_confirmation,
            )
            return _receipt(invocation, request, replayed=False)
        except Exception:  # noqa: BLE001 - return only the safe failed Receipt.
            _mark_failed(db, invocation, confirmation=accepted_confirmation)
            return _receipt(invocation, request, replayed=False)
        confirmed_result["confirmation"] = accepted_confirmation
        invocation.result_document = confirmed_result
        invocation.status = "succeeded"
        invocation.completed_at = _now()
        invocation.error_code = ""
        invocation.error_message = ""
        db.flush()
        return _receipt(invocation, request, replayed=False)


def invoke_capability(
    db: Session,
    deployment: ResolvedDeployment,
    actor: Actor,
    request: Request,
    *,
    invocation_source: str,
    registry: CapabilityProviderRegistry | None = None,
) -> Receipt:
    return CapabilityInvoker(registry).invoke(
        db,
        deployment,
        actor,
        request,
        invocation_source=invocation_source,
    )


__all__ = [
    "CapabilityInvocationError",
    "CapabilityInvoker",
    "CapabilityInvokerError",
    "invoke_capability",
    "resolve_capability_contract",
    "resolve_provider_binding",
    "runtime_context_from_invocation_audit",
]
