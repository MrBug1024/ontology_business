"""Resolve governed runtime inputs and persist credential-free invocation audit.

This service is the database-aware boundary between versioned capability ports
and invocation-time data.  Capability definitions declare requirements; this
module resolves those requirements to immutable catalog versions or a checked
connector binding for one tenant, scenario, and environment.

Only managed references are accepted.  Arbitrary inline documents and physical
connection details are deliberately outside this API.  Dataset heads are read
under a row lock and immediately pinned to a ready ``DatasetVersion``.  The
caller owns the surrounding transaction; this service flushes but never commits.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    BusinessScenario,
    CapabilityInvocation,
    ConnectorBinding,
    DataAsset,
    DataAssetVersion,
    DatasetHead,
    DatasetSchema,
    DatasetVersion,
    LogicalDataset,
    RunInputBinding,
    ScenarioCapabilityPort,
)
from . import connector_service
from .capability_contracts import (
    Actor,
    BindingOverride,
    CapabilityRef,
    DataPort,
    Request,
    ResolvedDataHandle,
    ResolvedDeployment,
    RuntimeDataContext,
    canonical_hash,
)
from .deployment_service import (
    DeploymentResolutionError,
    require_request_matches_deployment,
    resolve_runtime_data_context,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ENVIRONMENTS = frozenset({"dev", "staging", "prod"})
_INVOCATION_SOURCES = frozenset({"internal", "agent", "rest", "mcp"})
_MANAGED_KINDS = frozenset(
    {"dataset_version", "dataset_head", "asset_version", "connector_binding"}
)
_REFERENCE_FIELDS = MappingProxyType(
    {
        "dataset_version_id": "dataset_version",
        "dataset_head_id": "dataset_head",
        "asset_version_id": "asset_version",
        # Until a separate execution-artifact authority exists, an artifact
        # reference means an immutable catalog asset version.  It is persisted
        # through the existing asset_version_id foreign key.
        "artifact_id": "asset_version",
        "connector_binding_id": "connector_binding",
    }
)
_OVERRIDE_FIELDS = frozenset(
    {
        "port_key",
        "kind",
        "binding_kind",
        "source_kind",
        "reference_id",
        "signature",
        "binding_key",
        *_REFERENCE_FIELDS.keys(),
    }
)
_RUNTIME_ROLES = frozenset({"invocation_input", "reference", "rules"})


class RuntimeInputResolutionError(ValueError):
    """Structured, protocol-neutral failure while resolving runtime inputs."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        port_key: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = _safe_code(code)
        self.message = str(message or "runtime input resolution failed").strip()
        self.port_key = str(port_key).strip() if port_key not in (None, "") else None
        self.details = MappingProxyType(_safe_error_details(details or {}))

    def as_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.port_key is not None:
            document["port_key"] = self.port_key
        if self.details:
            document["details"] = dict(self.details)
        return document


RuntimeInputError = RuntimeInputResolutionError


@dataclass(frozen=True, slots=True)
class RuntimeInputResolution:
    """Resolved immutable context plus the newly-created audit records."""

    invocation: CapabilityInvocation
    input_bindings: tuple[RunInputBinding, ...]
    runtime_data_context: RuntimeDataContext
    data_ports: tuple[DataPort, ...]

    @property
    def context(self) -> RuntimeDataContext:
        return self.runtime_data_context

    @property
    def bindings(self) -> tuple[RunInputBinding, ...]:
        return self.input_bindings


@dataclass(frozen=True, slots=True)
class DeploymentInputResolution:
    """Current environment defaults resolved without creating an invocation."""

    runtime_data_context: RuntimeDataContext
    data_ports: tuple[DataPort, ...]
    port_records: tuple[Any, ...]
    missing_required_ports: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManagedInputOption:
    """Credential-free reference that has passed the runtime input contract.

    This DTO intentionally contains only logical catalog identities and the
    server-computed optimistic signature required by an invocation. Physical
    source ids, object locators, manifests, schemas and connector credentials
    are never represented here, so REST/Agent adapters can reuse it safely.
    """

    binding_kind: str
    port_key: str
    label: str
    signature: str
    reference_id: str | None = None
    binding_key: str | None = None
    version_number: int | None = None
    environment: str | None = None
    connector_kind: str | None = None
    updated_at: datetime | None = None

    def safe_document(self) -> dict[str, Any]:
        managed_input: dict[str, Any] = {
            "port_key": self.port_key,
            "expected_signature": self.signature,
        }
        selector_field = {
            "dataset_version": "dataset_version_id",
            "dataset_head": "dataset_head_id",
            "asset_version": "asset_version_id",
        }.get(self.binding_kind)
        if selector_field is not None:
            managed_input[selector_field] = self.reference_id
        else:
            managed_input["binding_key"] = self.binding_key
        document: dict[str, Any] = {
            "binding_kind": self.binding_kind,
            "label": self.label,
            "managed_input": managed_input,
        }
        if self.version_number is not None:
            document["version_number"] = self.version_number
        if self.environment is not None:
            document["environment"] = self.environment
        if self.connector_kind is not None:
            document["connector_kind"] = self.connector_kind
        if self.updated_at is not None:
            document["updated_at"] = self.updated_at
        return document


@dataclass(frozen=True, slots=True)
class _ReleasedPortContract:
    """Frozen release fields plus the durable live-row audit anchor."""

    id: str
    tenant_id: str
    scenario_id: str
    capability_kind: str
    capability_key: str
    port_key: str
    name: str
    description: str
    direction: str
    role: str
    media_kind: str
    schema_document: Mapping[str, Any]
    dataset_schema_hash: str
    is_required: bool
    cardinality: str
    binding_policy: str
    config: Mapping[str, Any]
    status: str = "active"
    dataset_id: None = None
    dataset_schema_id: None = None


@dataclass(frozen=True, slots=True)
class _ManagedReference:
    port_key: str
    kind: str
    reference_id: str
    expected_signature: str | None = None
    lookup_by_key: bool = False
    requested_kind: str = ""


@dataclass(frozen=True, slots=True)
class _ResolvedInput:
    port: Any
    handle: ResolvedDataHandle
    resolution_source: str
    requested_kind: str
    source_kind: str
    asset_version_id: str | None = None
    source_dataset_version_id: str | None = None
    dataset_head_id: str | None = None
    source_dataset_id: str | None = None
    connector_binding_id: str | None = None
    resolved_dataset_version_id: str | None = None
    content_hash: str = ""
    schema_hash: str = ""
    default_binding_id: str | None = None

    def safe_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "port_key": self.handle.port_key,
            "requested_kind": self.requested_kind,
            "resolved_kind": self.handle.binding_kind,
            "resolution_source": self.resolution_source,
            "signature": self.handle.signature,
        }
        if self.handle.version_id is not None:
            document["resolved_version_id"] = self.handle.version_id
        if self.default_binding_id is not None:
            document["default_binding_id"] = self.default_binding_id
        if self.dataset_head_id is not None:
            document["head_frozen_at_invocation"] = True
        return document


def _safe_code(value: Any) -> str:
    normalized = str(value or "runtime_input_error").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", normalized):
        return "runtime_input_error"
    return normalized


def _safe_error_details(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep errors structured without copying arbitrary request values."""

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


def _text(value: Any, label: str, *, maximum: int = 240) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum:
        raise RuntimeInputResolutionError(
            "invalid_runtime_scope",
            f"{label} must contain between 1 and {maximum} characters",
        )
    return normalized


def _environment(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in _ENVIRONMENTS:
        raise RuntimeInputResolutionError(
            "invalid_environment",
            "environment must be dev, staging, or prod",
        )
    return normalized


def _signature(value: Any, *, code: str, message: str, port_key: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise RuntimeInputResolutionError(
            code,
            message,
            port_key=port_key,
        )
    return normalized


def _one(db: Session, statement: Any) -> Any | None:
    return db.execute(statement).scalar_one_or_none()


def _normalize_role(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return "invocation_input" if normalized == "input" else normalized


def _normalize_kind(value: Any) -> tuple[str, str]:
    requested = str(value or "").strip().lower().replace("-", "_")
    normalized = "asset_version" if requested == "artifact" else requested
    if normalized not in _MANAGED_KINDS:
        raise RuntimeInputResolutionError(
            "unsupported_managed_reference",
            "runtime inputs accept only governed catalog or connector references",
        )
    return normalized, requested


def _looks_like_reference(value: Mapping[str, Any]) -> bool:
    return bool(set(value).intersection(_OVERRIDE_FIELDS))


def _override_items(overrides: Any) -> tuple[Any, ...]:
    if overrides in (None, (), [], {}):
        return ()
    if isinstance(overrides, (BindingOverride,)):
        return (overrides,)
    if isinstance(overrides, Mapping):
        if _looks_like_reference(overrides):
            return (overrides,)
        expanded: list[Any] = []
        for port_key, value in overrides.items():
            if isinstance(value, BindingOverride):
                if value.port_key != str(port_key).strip().lower():
                    raise RuntimeInputResolutionError(
                        "invalid_override_shape",
                        "override map key and reference port key do not match",
                    )
                expanded.append(value)
            elif isinstance(value, Mapping):
                item = dict(value)
                if "port_key" in item and str(item["port_key"]) != str(port_key):
                    raise RuntimeInputResolutionError(
                        "invalid_override_shape",
                        "override map key and reference port key do not match",
                    )
                item["port_key"] = str(port_key)
                expanded.append(item)
            else:
                raise RuntimeInputResolutionError(
                    "invalid_override_shape",
                    "each override must be a managed-reference object",
                )
        return tuple(expanded)
    if isinstance(overrides, Sequence) and not isinstance(
        overrides, (str, bytes, bytearray)
    ):
        return tuple(overrides)
    if isinstance(overrides, Iterable) and not isinstance(
        overrides, (str, bytes, bytearray)
    ):
        return tuple(overrides)
    raise RuntimeInputResolutionError(
        "invalid_override_shape",
        "runtime input overrides must be a mapping or sequence",
    )


def _parse_override(value: Any) -> _ManagedReference:
    if isinstance(value, BindingOverride):
        kind, requested = _normalize_kind(value.binding_kind)
        return _ManagedReference(
            port_key=value.port_key,
            kind=kind,
            reference_id=value.selector_value,
            expected_signature=value.signature,
            lookup_by_key=value.selector == "binding_key",
            requested_kind=requested,
        )
    if not isinstance(value, Mapping):
        raise RuntimeInputResolutionError(
            "invalid_override_shape",
            "each runtime input override must be a managed-reference object",
        )
    unknown = sorted(str(key) for key in set(value) - _OVERRIDE_FIELDS)
    if unknown:
        raise RuntimeInputResolutionError(
            "invalid_override_shape",
            "runtime input override contains unsupported fields",
            details={"fields": unknown},
        )
    port_key = str(value.get("port_key") or "").strip().lower()
    if not port_key:
        raise RuntimeInputResolutionError(
            "invalid_override_shape",
            "runtime input override requires port_key",
        )

    kind_values = {
        str(value.get(name)).strip().lower()
        for name in ("kind", "binding_kind", "source_kind")
        if value.get(name) not in (None, "")
    }
    if len(kind_values) > 1:
        raise RuntimeInputResolutionError(
            "invalid_override_shape",
            "runtime input override declares conflicting reference kinds",
            port_key=port_key,
        )

    direct = [
        (field, kind)
        for field, kind in _REFERENCE_FIELDS.items()
        if value.get(field) not in (None, "")
    ]
    reference_id = value.get("reference_id")
    binding_key = value.get("binding_key")
    selectors = len(direct) + int(reference_id not in (None, "")) + int(
        binding_key not in (None, "")
    )
    if selectors != 1:
        raise RuntimeInputResolutionError(
            "invalid_override_shape",
            "runtime input override must contain exactly one managed reference",
            port_key=port_key,
        )

    lookup_by_key = False
    if direct:
        field, direct_kind = direct[0]
        if kind_values:
            declared, _ = _normalize_kind(next(iter(kind_values)))
            if declared != direct_kind:
                raise RuntimeInputResolutionError(
                    "invalid_override_shape",
                    "managed reference field does not match its declared kind",
                    port_key=port_key,
                )
        kind, requested = _normalize_kind(
            "artifact" if field == "artifact_id" else direct_kind
        )
        reference_id = value[field]
    elif binding_key not in (None, ""):
        if kind_values:
            declared, _ = _normalize_kind(next(iter(kind_values)))
            if declared != "connector_binding":
                raise RuntimeInputResolutionError(
                    "invalid_override_shape",
                    "binding_key can resolve only a connector binding",
                    port_key=port_key,
                )
        kind, requested = "connector_binding", "connector_binding"
        reference_id = binding_key
        lookup_by_key = True
    else:
        if not kind_values:
            raise RuntimeInputResolutionError(
                "invalid_override_shape",
                "reference_id requires a managed reference kind",
                port_key=port_key,
            )
        kind, requested = _normalize_kind(next(iter(kind_values)))

    normalized_reference = str(reference_id or "").strip()
    if not normalized_reference or len(normalized_reference) > 240:
        raise RuntimeInputResolutionError(
            "invalid_override_shape",
            "managed reference id is invalid",
            port_key=port_key,
        )
    expected = value.get("signature")
    expected_signature = None
    if expected not in (None, ""):
        expected_signature = _signature(
            expected,
            code="invalid_expected_signature",
            message="expected managed-reference signature is invalid",
            port_key=port_key,
        )
    return _ManagedReference(
        port_key=port_key,
        kind=kind,
        reference_id=normalized_reference,
        expected_signature=expected_signature,
        lookup_by_key=lookup_by_key,
        requested_kind=requested,
    )


def _normalize_overrides(overrides: Any) -> dict[str, _ManagedReference]:
    result: dict[str, _ManagedReference] = {}
    for raw in _override_items(overrides):
        item = _parse_override(raw)
        if item.port_key in result:
            raise RuntimeInputResolutionError(
                "duplicate_runtime_input",
                "multiple runtime input overrides target the same port",
                port_key=item.port_key,
            )
        result[item.port_key] = item
    return result


def _safe_port_config(port: Any) -> Mapping[str, Any]:
    value = getattr(port, "config", None)
    return value if isinstance(value, Mapping) else {}


def _allowed_kinds(port: Any) -> tuple[str, ...]:
    config = _safe_port_config(port)
    configured = config.get("allowed_binding_kinds", config.get("binding_kinds"))
    if configured not in (None, "", (), []):
        if isinstance(configured, str):
            configured = (configured,)
        if not isinstance(configured, Sequence):
            raise RuntimeInputResolutionError(
                "invalid_port_contract",
                "allowed binding kinds must be a list",
                port_key=port.port_key,
            )
        normalized = {_normalize_kind(value)[0] for value in configured}
        return tuple(sorted(normalized))

    media_kind = str(getattr(port, "media_kind", "structured") or "structured").lower()
    if media_kind == "dataset":
        return ("dataset_head", "dataset_version")
    if media_kind == "connector":
        return ("connector_binding",)
    if media_kind in {"artifact", "document"}:
        return ("asset_version",)
    # Structured ports can be backed by any governed representation.  Message
    # ports normally use binding_policy=none and are filtered before this point.
    return tuple(sorted(_MANAGED_KINDS))


def _allows_override(port: Any) -> bool:
    policy = str(getattr(port, "binding_policy", "none") or "none").lower()
    if policy == "none":
        return False
    configured = _safe_port_config(port).get("allow_override")
    if isinstance(configured, bool):
        return configured
    # Legacy policies used to imply that scenario/release data could become a
    # runtime default.  Runtime defaults are no longer resolved; every active
    # input port must therefore accept the invocation data supplied by the
    # calling Agent (attachments or its configured business connection).
    return True


def allowed_binding_kinds(port: Any) -> tuple[str, ...]:
    """Return the public, normalized reference kinds accepted by a port."""
    return _allowed_kinds(port)


def allows_invocation_override(port: Any) -> bool:
    """Expose whether callers may select a governed reference this turn."""
    return _allows_override(port)


def _data_port(port: Any) -> DataPort:
    released_schema_hash = str(
        getattr(port, "dataset_schema_hash", "") or ""
    ).strip().lower()
    return DataPort(
        key=port.port_key,
        modality=port.media_kind,
        schema=port.schema_document or {},
        schema_hash=released_schema_hash,
        required=bool(port.is_required),
        binding_kinds=_allowed_kinds(port),
        override_policy="managed-reference" if _allows_override(port) else "forbidden",
        description=port.description or "",
    )


def _require_scope(
    db: Session,
    *,
    tenant_id: str,
    scenario_id: str,
) -> BusinessScenario:
    scenario = _one(
        db,
        select(BusinessScenario).where(BusinessScenario.id == scenario_id),
    )
    if scenario is None:
        raise RuntimeInputResolutionError(
            "scenario_not_found",
            "scenario is unavailable in the current runtime scope",
        )
    if str(getattr(scenario, "tenant_id", "") or "") != tenant_id:
        raise RuntimeInputResolutionError(
            "scenario_scope_mismatch",
            "scenario is unavailable in the current runtime scope",
        )
    return scenario


def load_runtime_input_ports(
    db: Session,
    *,
    tenant_id: str,
    scenario_id: str,
    environment: str,
    capability: CapabilityRef,
    definition: Any | None = None,
) -> tuple[Any, ...]:
    """Load managed input ports owned by one capability in a validated scope."""

    normalized_tenant = _text(tenant_id, "tenant id")
    normalized_scenario = _text(scenario_id, "scenario id")
    _environment(environment)
    if not isinstance(capability, CapabilityRef):
        raise RuntimeInputResolutionError(
            "invalid_capability_reference",
            "runtime input resolution requires a capability reference",
        )
    capability_kind = str(capability.kind or "").strip().lower()
    capability_key = str(capability.resource_id or "").strip()
    _require_scope(
        db,
        tenant_id=normalized_tenant,
        scenario_id=normalized_scenario,
    )
    has_definition_ports = definition is not None and (
        (isinstance(definition, Mapping) and "capability_ports" in definition)
        or hasattr(definition, "capability_ports")
    )
    if not has_definition_ports:
        rows: tuple[Any, ...] = tuple(
            db.execute(
                select(ScenarioCapabilityPort)
                .where(
                    ScenarioCapabilityPort.tenant_id == normalized_tenant,
                    ScenarioCapabilityPort.scenario_id == normalized_scenario,
                    ScenarioCapabilityPort.capability_kind == capability_kind,
                    ScenarioCapabilityPort.capability_key == capability_key,
                    ScenarioCapabilityPort.direction == "input",
                    ScenarioCapabilityPort.status == "active",
                    ScenarioCapabilityPort.binding_policy != "none",
                )
                .order_by(ScenarioCapabilityPort.port_key)
            )
            .scalars()
            .all()
        )
    else:
        raw_ports = (
            definition.get("capability_ports", {})
            if isinstance(definition, Mapping)
            else getattr(definition, "capability_ports", {})
        ) or {}
        values = raw_ports.values() if isinstance(raw_ports, Mapping) else raw_ports
        if not isinstance(values, Iterable) or isinstance(
            values, (str, bytes, bytearray)
        ):
            raise RuntimeInputResolutionError(
                "invalid_port_contract",
                "resolved definition capability ports are invalid",
            )
        all_ports = tuple(values)
        invalid_owners = tuple(
            str(getattr(item, "id", "") or getattr(item, "port_key", ""))
            for item in all_ports
            if not str(getattr(item, "capability_kind", "") or "").strip()
            or not str(getattr(item, "capability_key", "") or "").strip()
        )
        if invalid_owners:
            raise RuntimeInputResolutionError(
                "invalid_port_contract",
                "resolved definition contains capability ports without ownership",
                details={"port_ids": sorted(invalid_owners)},
            )
        selected = tuple(
            item
            for item in all_ports
            if str(getattr(item, "capability_kind", "") or "").strip().lower()
            == capability_kind
            and str(getattr(item, "capability_key", "") or "").strip()
            == capability_key
            if str(getattr(item, "direction", "input") or "input").lower()
            == "input"
            and str(getattr(item, "binding_policy", "none") or "none").lower()
            != "none"
        )
        if str(getattr(definition, "source", "live") or "live") == "release":
            port_ids = tuple(str(getattr(item, "id", "") or "") for item in selected)
            if any(not item for item in port_ids) or len(port_ids) != len(set(port_ids)):
                raise RuntimeInputResolutionError(
                    "invalid_port_contract",
                    "released capability ports have invalid audit identities",
                )
            anchors = {
                item.id: item
                for item in db.execute(
                    select(ScenarioCapabilityPort).where(
                        ScenarioCapabilityPort.id.in_(port_ids or ("",)),
                        ScenarioCapabilityPort.tenant_id == normalized_tenant,
                        ScenarioCapabilityPort.scenario_id == normalized_scenario,
                    )
                )
                .scalars()
                .all()
            }
            missing_anchors = sorted(set(port_ids) - set(anchors))
            if missing_anchors:
                raise RuntimeInputResolutionError(
                    "released_port_audit_anchor_missing",
                    "released capability port audit anchor is unavailable",
                    details={"port_ids": missing_anchors},
                )
            rows = tuple(
                _ReleasedPortContract(
                    id=str(item.id),
                    tenant_id=normalized_tenant,
                    scenario_id=normalized_scenario,
                    capability_kind=capability_kind,
                    capability_key=capability_key,
                    port_key=str(item.port_key),
                    name=str(getattr(item, "name", "") or item.port_key),
                    description=str(getattr(item, "description", "") or ""),
                    direction="input",
                    role=str(getattr(item, "role", "invocation_input") or "invocation_input"),
                    media_kind=str(getattr(item, "media_kind", "structured") or "structured"),
                    schema_document=MappingProxyType(
                        dict(getattr(item, "schema_document", {}) or {})
                    ),
                    dataset_schema_hash=str(
                        getattr(item, "dataset_schema_hash", "") or ""
                    ).lower(),
                    is_required=bool(getattr(item, "is_required", True)),
                    cardinality=str(getattr(item, "cardinality", "one") or "one"),
                    binding_policy=str(
                        getattr(item, "binding_policy", "per_invocation")
                        or "per_invocation"
                    ),
                    config=MappingProxyType(dict(getattr(item, "config", {}) or {})),
                )
                for item in selected
            )
        else:
            rows = selected
    duplicate_keys = [
        key
        for key in {str(item.port_key).strip().lower() for item in rows}
        if sum(str(item.port_key).strip().lower() == key for item in rows) > 1
    ]
    if duplicate_keys:
        raise RuntimeInputResolutionError(
            "invalid_port_contract",
            "capability contains duplicate active input ports",
            details={"port_keys": sorted(duplicate_keys)},
        )
    return rows


def resolve_deployment_inputs(
    db: Session,
    *,
    tenant_id: str,
    scenario_id: str,
    environment: str,
    capability: CapabilityRef,
    definition: Any,
) -> DeploymentInputResolution:
    """Resolve only environment defaults for deployment identity/readiness."""

    ports = load_runtime_input_ports(
        db,
        tenant_id=tenant_id,
        scenario_id=scenario_id,
        environment=environment,
        capability=capability,
        definition=definition,
    )
    resolved: list[_ResolvedInput] = []
    missing: list[str] = []
    for port in ports:
        item = _scenario_default(
            db,
            tenant_id=tenant_id,
            scenario_id=scenario_id,
            environment=environment,
            port=port,
        )
        if item is None:
            if bool(port.is_required):
                missing.append(str(port.port_key))
            continue
        resolved.append(item)
    contracts = tuple(_data_port(port) for port in ports)
    context = RuntimeDataContext(tuple(item.handle for item in resolved))
    return DeploymentInputResolution(
        runtime_data_context=context,
        data_ports=contracts,
        port_records=ports,
        missing_required_ports=tuple(sorted(missing)),
    )


def _require_expected_signature(
    reference: _ManagedReference,
    actual: str,
) -> None:
    if (
        reference.expected_signature is not None
        and reference.expected_signature != actual
    ):
        raise RuntimeInputResolutionError(
            "managed_reference_changed",
            "managed reference changed after the invocation was prepared",
            port_key=reference.port_key,
        )


def _load_dataset_version(
    db: Session,
    *,
    reference: _ManagedReference,
    tenant_id: str,
    port: ScenarioCapabilityPort,
    version_id: str,
) -> tuple[DatasetVersion, str, str]:
    version = _one(
        db,
        select(DatasetVersion).where(DatasetVersion.id == version_id),
    )
    if version is None:
        raise RuntimeInputResolutionError(
            "managed_reference_not_found",
            "managed dataset version is unavailable",
            port_key=reference.port_key,
        )
    if str(version.tenant_id) != tenant_id:
        raise RuntimeInputResolutionError(
            "managed_reference_scope_mismatch",
            "managed dataset version is outside the invocation scope",
            port_key=reference.port_key,
        )
    if str(version.status or "").lower() != "ready":
        raise RuntimeInputResolutionError(
            "managed_reference_not_ready",
            "managed dataset version is not ready",
            port_key=reference.port_key,
        )
    expected_dataset = getattr(port, "dataset_id", None)
    expected_schema = getattr(port, "dataset_schema_id", None)
    if expected_dataset not in (None, "") and str(version.dataset_id) != str(
        expected_dataset
    ):
        raise RuntimeInputResolutionError(
            "dataset_contract_mismatch",
            "dataset version does not satisfy the port dataset contract",
            port_key=reference.port_key,
        )
    if expected_schema not in (None, "") and str(version.schema_id) != str(
        expected_schema
    ):
        raise RuntimeInputResolutionError(
            "dataset_contract_mismatch",
            "dataset version does not satisfy the port schema contract",
            port_key=reference.port_key,
        )
    content_hash = _signature(
        version.content_hash,
        code="invalid_managed_signature",
        message="dataset version has no valid content signature",
        port_key=reference.port_key,
    )
    schema = _one(
        db,
        select(DatasetSchema).where(
            DatasetSchema.id == version.schema_id,
            DatasetSchema.dataset_id == version.dataset_id,
            DatasetSchema.tenant_id == tenant_id,
        ),
    )
    if schema is None:
        raise RuntimeInputResolutionError(
            "dataset_contract_mismatch",
            "dataset version schema is unavailable in the invocation scope",
            port_key=reference.port_key,
        )
    schema_hash = _signature(
        schema.schema_hash,
        code="invalid_managed_signature",
        message="dataset schema has no valid signature",
        port_key=reference.port_key,
    )
    released_schema_hash = str(
        getattr(port, "dataset_schema_hash", "") or ""
    ).strip().lower()
    if released_schema_hash and released_schema_hash != schema_hash:
        raise RuntimeInputResolutionError(
            "dataset_contract_mismatch",
            "dataset version schema does not satisfy the released port contract",
            port_key=reference.port_key,
        )
    _require_expected_signature(reference, content_hash)
    return version, content_hash, schema_hash


def _resolve_dataset_version(
    db: Session,
    *,
    reference: _ManagedReference,
    tenant_id: str,
    port: ScenarioCapabilityPort,
    resolution_source: str,
    default_binding_id: str | None = None,
) -> _ResolvedInput:
    version, content_hash, schema_hash = _load_dataset_version(
        db,
        reference=reference,
        tenant_id=tenant_id,
        port=port,
        version_id=reference.reference_id,
    )
    handle = ResolvedDataHandle(
        port_key=reference.port_key,
        binding_kind="dataset_version",
        reference_id=version.id,
        version_id=version.id,
        signature=content_hash,
    )
    return _ResolvedInput(
        port=port,
        handle=handle,
        resolution_source=resolution_source,
        requested_kind=reference.requested_kind or "dataset_version",
        source_kind="dataset_version",
        source_dataset_version_id=version.id,
        resolved_dataset_version_id=version.id,
        content_hash=content_hash,
        schema_hash=schema_hash,
        default_binding_id=default_binding_id,
    )


def _resolve_dataset_head(
    db: Session,
    *,
    reference: _ManagedReference,
    tenant_id: str,
    environment: str,
    port: ScenarioCapabilityPort,
    resolution_source: str,
    default_binding_id: str | None = None,
    lock_reference: bool = True,
) -> _ResolvedInput:
    # The row lock makes the pointer read and the ensuing invocation audit one
    # atomic operation on PostgreSQL.  The immutable version id is persisted.
    statement = select(DatasetHead).where(DatasetHead.id == reference.reference_id)
    if lock_reference:
        statement = statement.with_for_update()
    head = _one(db, statement)
    if head is None:
        raise RuntimeInputResolutionError(
            "managed_reference_not_found",
            "managed dataset head is unavailable",
            port_key=reference.port_key,
        )
    if str(head.tenant_id) != tenant_id:
        raise RuntimeInputResolutionError(
            "managed_reference_scope_mismatch",
            "managed dataset head is outside the invocation scope",
            port_key=reference.port_key,
        )
    if str(head.environment or "").lower() != environment:
        raise RuntimeInputResolutionError(
            "managed_reference_environment_mismatch",
            "managed dataset head belongs to another environment",
            port_key=reference.port_key,
        )
    version_reference = _ManagedReference(
        port_key=reference.port_key,
        kind="dataset_version",
        reference_id=head.dataset_version_id,
        expected_signature=reference.expected_signature,
        requested_kind=reference.requested_kind or "dataset_head",
    )
    version, content_hash, schema_hash = _load_dataset_version(
        db,
        reference=version_reference,
        tenant_id=tenant_id,
        port=port,
        version_id=head.dataset_version_id,
    )
    if str(version.dataset_id) != str(head.dataset_id):
        raise RuntimeInputResolutionError(
            "dataset_contract_mismatch",
            "dataset head does not point to a version of its dataset",
            port_key=reference.port_key,
        )
    handle = ResolvedDataHandle(
        port_key=reference.port_key,
        binding_kind="dataset_head",
        reference_id=head.id,
        version_id=version.id,
        signature=content_hash,
    )
    return _ResolvedInput(
        port=port,
        handle=handle,
        resolution_source=resolution_source,
        requested_kind=reference.requested_kind or "dataset_head",
        source_kind="dataset_head",
        dataset_head_id=head.id,
        source_dataset_id=head.dataset_id,
        resolved_dataset_version_id=version.id,
        content_hash=content_hash,
        schema_hash=schema_hash,
        default_binding_id=default_binding_id,
    )


def _resolve_asset_version(
    db: Session,
    *,
    reference: _ManagedReference,
    tenant_id: str,
    port: ScenarioCapabilityPort,
    resolution_source: str,
) -> _ResolvedInput:
    version = _one(
        db,
        select(DataAssetVersion).where(DataAssetVersion.id == reference.reference_id),
    )
    if version is None:
        raise RuntimeInputResolutionError(
            "managed_reference_not_found",
            "managed asset version is unavailable",
            port_key=reference.port_key,
        )
    if str(version.tenant_id) != tenant_id:
        raise RuntimeInputResolutionError(
            "managed_reference_scope_mismatch",
            "managed asset version is outside the invocation scope",
            port_key=reference.port_key,
        )
    if str(version.status or "").lower() != "ready":
        raise RuntimeInputResolutionError(
            "managed_reference_not_ready",
            "managed asset version is not ready",
            port_key=reference.port_key,
        )
    asset = _one(
        db,
        select(DataAsset).where(
            DataAsset.id == version.asset_id,
            DataAsset.tenant_id == tenant_id,
        ),
    )
    if asset is None or str(asset.lifecycle_status or "").lower() != "active":
        raise RuntimeInputResolutionError(
            "managed_reference_not_ready",
            "managed asset is not active",
            port_key=reference.port_key,
        )
    lifecycle = (
        (version.version_document or {}).get("lifecycle", {})
        if isinstance(version.version_document, Mapping)
        else {}
    )
    if isinstance(lifecycle, Mapping) and bool(lifecycle.get("temporary")):
        raw_expiry = lifecycle.get("expires_at")
        try:
            expires_at = datetime.fromisoformat(
                str(raw_expiry or "").replace("Z", "+00:00")
            )
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            raise RuntimeInputResolutionError(
                "managed_reference_not_ready",
                "temporary managed attachment has no valid expiry",
                port_key=reference.port_key,
            ) from None
        if expires_at <= datetime.now(timezone.utc):
            raise RuntimeInputResolutionError(
                "managed_reference_expired",
                "temporary managed attachment has expired",
                port_key=reference.port_key,
            )
    content_hash = _signature(
        version.content_sha256,
        code="invalid_managed_signature",
        message="asset version has no valid content signature",
        port_key=reference.port_key,
    )
    _require_expected_signature(reference, content_hash)
    handle = ResolvedDataHandle(
        port_key=reference.port_key,
        binding_kind="asset_version",
        reference_id=version.id,
        version_id=version.id,
        signature=content_hash,
    )
    return _ResolvedInput(
        port=port,
        handle=handle,
        resolution_source=resolution_source,
        requested_kind=reference.requested_kind or "asset_version",
        source_kind="asset_version",
        asset_version_id=version.id,
        content_hash=content_hash,
        schema_hash=_data_port(port).schema_hash,
    )


def _load_connector(
    db: Session,
    *,
    reference: _ManagedReference,
    tenant_id: str,
    scenario_id: str,
    environment: str,
    lock_reference: bool = True,
) -> ConnectorBinding:
    statement = select(ConnectorBinding)
    if reference.lookup_by_key:
        statement = statement.where(
            ConnectorBinding.binding_key == reference.reference_id,
            ConnectorBinding.tenant_id == tenant_id,
            ConnectorBinding.scenario_id == scenario_id,
            ConnectorBinding.environment == environment,
        )
    else:
        statement = statement.where(ConnectorBinding.id == reference.reference_id)
    binding = _one(db, statement.with_for_update() if lock_reference else statement)
    if binding is None:
        raise RuntimeInputResolutionError(
            "managed_reference_not_found",
            "managed connector binding is unavailable",
            port_key=reference.port_key,
        )
    if str(binding.tenant_id) != tenant_id or str(binding.scenario_id) != scenario_id:
        raise RuntimeInputResolutionError(
            "managed_reference_scope_mismatch",
            "managed connector binding is outside the invocation scope",
            port_key=reference.port_key,
        )
    if str(binding.environment or "").lower() != environment:
        raise RuntimeInputResolutionError(
            "managed_reference_environment_mismatch",
            "managed connector binding belongs to another environment",
            port_key=reference.port_key,
        )
    if str(binding.health_status or "").lower() != "healthy":
        raise RuntimeInputResolutionError(
            "managed_reference_not_ready",
            "managed connector binding is not healthy",
            port_key=reference.port_key,
        )
    actual = _signature(
        binding.connector_signature,
        code="invalid_managed_signature",
        message="connector binding has no valid checked signature",
        port_key=reference.port_key,
    )
    _require_expected_signature(reference, actual)
    return binding


def _resolve_connector(
    db: Session,
    *,
    reference: _ManagedReference,
    tenant_id: str,
    scenario_id: str,
    environment: str,
    port: ScenarioCapabilityPort,
    resolution_source: str,
    lock_reference: bool = True,
) -> _ResolvedInput:
    binding = _load_connector(
        db,
        reference=reference,
        tenant_id=tenant_id,
        scenario_id=scenario_id,
        environment=environment,
        lock_reference=lock_reference,
    )
    actual = str(binding.connector_signature).lower()
    handle = ResolvedDataHandle(
        port_key=reference.port_key,
        binding_kind="connector_binding",
        reference_id=binding.id,
        signature=actual,
    )
    return _ResolvedInput(
        port=port,
        handle=handle,
        resolution_source=resolution_source,
        requested_kind=reference.requested_kind or "connector_binding",
        source_kind="connector_binding",
        connector_binding_id=binding.id,
        content_hash=actual,
        schema_hash=_data_port(port).schema_hash,
    )


def _resolve_reference(
    db: Session,
    *,
    reference: _ManagedReference,
    tenant_id: str,
    scenario_id: str,
    environment: str,
    port: ScenarioCapabilityPort,
    resolution_source: str,
    default_binding_id: str | None = None,
    lock_reference: bool = True,
) -> _ResolvedInput:
    allowed = set(_allowed_kinds(port))
    if reference.kind not in allowed:
        raise RuntimeInputResolutionError(
            "binding_kind_not_allowed",
            "managed reference kind is not allowed by the port contract",
            port_key=reference.port_key,
            details={"allowed_kinds": sorted(allowed)},
        )
    if reference.kind == "dataset_version":
        return _resolve_dataset_version(
            db,
            reference=reference,
            tenant_id=tenant_id,
            port=port,
            resolution_source=resolution_source,
            default_binding_id=default_binding_id,
        )
    if reference.kind == "dataset_head":
        return _resolve_dataset_head(
            db,
            reference=reference,
            tenant_id=tenant_id,
            environment=environment,
            port=port,
            resolution_source=resolution_source,
            default_binding_id=default_binding_id,
            lock_reference=lock_reference,
        )
    if reference.kind == "asset_version":
        return _resolve_asset_version(
            db,
            reference=reference,
            tenant_id=tenant_id,
            port=port,
            resolution_source=resolution_source,
        )
    return _resolve_connector(
        db,
        reference=reference,
        tenant_id=tenant_id,
        scenario_id=scenario_id,
        environment=environment,
        port=port,
        resolution_source=resolution_source,
        lock_reference=lock_reference,
    )


def list_managed_input_options(
    db: Session,
    *,
    tenant_id: str,
    scenario_id: str,
    environment: str,
    port: Any,
) -> tuple[ManagedInputOption, ...]:
    """List only logical references that this exact input port can resolve.

    Authorization is deliberately owned by the caller because catalog rows do
    not define a second, competing ACL model. This function enforces the
    runtime contract itself: tenant/scenario/environment scope, frozen schema
    hash, binding kinds, active lifecycle, readiness, connector freshness and
    temporary-asset expiry. It performs no writes and does not lock mutable
    heads or connector bindings while merely displaying choices.
    """

    normalized_tenant = _text(tenant_id, "tenant id")
    normalized_scenario = _text(scenario_id, "scenario id")
    normalized_environment = _environment(environment)
    scenario = _require_scope(
        db,
        tenant_id=normalized_tenant,
        scenario_id=normalized_scenario,
    )
    if str(getattr(port, "direction", "") or "").lower() != "input":
        raise RuntimeInputResolutionError(
            "runtime_input_port_not_found",
            "managed input options require an input port",
            port_key=str(getattr(port, "port_key", "") or "") or None,
        )
    if not _allows_override(port):
        raise RuntimeInputResolutionError(
            "runtime_input_override_forbidden",
            "runtime input port does not allow per-invocation selection",
            port_key=str(getattr(port, "port_key", "") or "") or None,
        )

    port_key = _text(getattr(port, "port_key", ""), "port key")
    allowed = set(_allowed_kinds(port))
    options: list[ManagedInputOption] = []
    skippable_candidate_errors = {
        "dataset_contract_mismatch",
        "invalid_managed_signature",
        "managed_reference_environment_mismatch",
        "managed_reference_expired",
        "managed_reference_not_found",
        "managed_reference_not_ready",
        "managed_reference_scope_mismatch",
    }

    def resolve(reference: _ManagedReference) -> _ResolvedInput | None:
        try:
            return _resolve_reference(
                db,
                reference=reference,
                tenant_id=normalized_tenant,
                scenario_id=normalized_scenario,
                environment=normalized_environment,
                port=port,
                resolution_source="discovery",
                lock_reference=False,
            )
        except RuntimeInputResolutionError as exc:
            # One corrupt, stale or expired catalog row must not make unrelated
            # safe choices unavailable. The caller cannot select skipped rows,
            # and invocation-time resolution independently fails closed.
            if exc.code in skippable_candidate_errors:
                return None
            raise

    dataset_id = getattr(port, "dataset_id", None)
    dataset_schema_id = getattr(port, "dataset_schema_id", None)
    if "dataset_version" in allowed:
        statement = (
            select(DatasetVersion, LogicalDataset)
            .join(LogicalDataset, LogicalDataset.id == DatasetVersion.dataset_id)
            .where(
                DatasetVersion.tenant_id == normalized_tenant,
                DatasetVersion.status == "ready",
                LogicalDataset.tenant_id == normalized_tenant,
                LogicalDataset.lifecycle_status == "active",
            )
            .order_by(
                LogicalDataset.name,
                DatasetVersion.version_number.desc(),
                DatasetVersion.id,
            )
        )
        if dataset_id not in (None, ""):
            statement = statement.where(DatasetVersion.dataset_id == str(dataset_id))
        if dataset_schema_id not in (None, ""):
            statement = statement.where(DatasetVersion.schema_id == str(dataset_schema_id))
        for version, dataset in db.execute(statement).all():
            resolved = resolve(
                _ManagedReference(
                    port_key=port_key,
                    kind="dataset_version",
                    reference_id=version.id,
                    requested_kind="dataset_version",
                )
            )
            if resolved is None:
                continue
            options.append(
                ManagedInputOption(
                    binding_kind="dataset_version",
                    port_key=port_key,
                    reference_id=version.id,
                    label=f"{dataset.name} · v{version.version_number}",
                    signature=resolved.handle.signature,
                    version_number=int(version.version_number),
                )
            )

    if "dataset_head" in allowed:
        statement = (
            select(DatasetHead, LogicalDataset, DatasetVersion)
            .join(LogicalDataset, LogicalDataset.id == DatasetHead.dataset_id)
            .join(DatasetVersion, DatasetVersion.id == DatasetHead.dataset_version_id)
            .where(
                DatasetHead.tenant_id == normalized_tenant,
                DatasetHead.environment == normalized_environment,
                LogicalDataset.tenant_id == normalized_tenant,
                LogicalDataset.lifecycle_status == "active",
                DatasetVersion.tenant_id == normalized_tenant,
                DatasetVersion.status == "ready",
            )
            .order_by(LogicalDataset.name, DatasetHead.id)
        )
        if dataset_id not in (None, ""):
            statement = statement.where(DatasetHead.dataset_id == str(dataset_id))
        if dataset_schema_id not in (None, ""):
            statement = statement.where(DatasetVersion.schema_id == str(dataset_schema_id))
        for head, dataset, version in db.execute(statement).all():
            resolved = resolve(
                _ManagedReference(
                    port_key=port_key,
                    kind="dataset_head",
                    reference_id=head.id,
                    requested_kind="dataset_head",
                )
            )
            if resolved is None:
                continue
            options.append(
                ManagedInputOption(
                    binding_kind="dataset_head",
                    port_key=port_key,
                    reference_id=head.id,
                    label=f"{dataset.name} · {normalized_environment}",
                    signature=resolved.handle.signature,
                    version_number=int(version.version_number),
                    environment=normalized_environment,
                    updated_at=head.updated_at,
                )
            )

    if "asset_version" in allowed:
        statement = (
            select(DataAssetVersion, DataAsset)
            .join(DataAsset, DataAsset.id == DataAssetVersion.asset_id)
            .where(
                DataAssetVersion.tenant_id == normalized_tenant,
                DataAssetVersion.status == "ready",
                DataAsset.tenant_id == normalized_tenant,
                DataAsset.lifecycle_status == "active",
            )
            .order_by(
                DataAsset.name,
                DataAssetVersion.version_number.desc(),
                DataAssetVersion.id,
            )
        )
        for version, asset in db.execute(statement).all():
            resolved = resolve(
                _ManagedReference(
                    port_key=port_key,
                    kind="asset_version",
                    reference_id=version.id,
                    requested_kind="asset_version",
                )
            )
            if resolved is None:
                continue
            options.append(
                ManagedInputOption(
                    binding_kind="asset_version",
                    port_key=port_key,
                    reference_id=version.id,
                    label=f"{asset.name} · v{version.version_number}",
                    signature=resolved.handle.signature,
                    version_number=int(version.version_number),
                )
            )

    if "connector_binding" in allowed:
        summaries = {
            str(item.get("binding_key") or ""): item
            for item in connector_service.list_bindings(
                db,
                scenario,
                environment=normalized_environment,
            )
            if bool(item.get("ready", False))
        }
        bindings = db.execute(
            select(ConnectorBinding)
            .where(
                ConnectorBinding.tenant_id == normalized_tenant,
                ConnectorBinding.scenario_id == normalized_scenario,
                ConnectorBinding.environment == normalized_environment,
            )
            .order_by(ConnectorBinding.binding_key, ConnectorBinding.id)
        ).scalars().all()
        for binding in bindings:
            summary = summaries.get(str(binding.binding_key))
            if summary is None:
                continue
            resolved = resolve(
                _ManagedReference(
                    port_key=port_key,
                    kind="connector_binding",
                    reference_id=binding.binding_key,
                    lookup_by_key=True,
                    requested_kind="connector_binding",
                )
            )
            if resolved is None:
                continue
            options.append(
                ManagedInputOption(
                    binding_kind="connector_binding",
                    port_key=port_key,
                    binding_key=binding.binding_key,
                    label=str(
                        summary.get("reference_label")
                        or binding.reference_label
                        or binding.binding_key
                    ),
                    signature=resolved.handle.signature,
                    environment=normalized_environment,
                    connector_kind=binding.connector_kind,
                    updated_at=binding.updated_at,
                )
            )

    return tuple(
        sorted(
            options,
            key=lambda item: (
                item.binding_kind,
                item.label.casefold(),
                item.reference_id or item.binding_key or "",
            ),
        )
    )


def _scenario_default(
    db: Session,
    *,
    tenant_id: str,
    scenario_id: str,
    environment: str,
    port: ScenarioCapabilityPort,
) -> _ResolvedInput | None:
    # Scenario dataset/connector bindings are modeling and release metadata,
    # never formal invocation data.  Runtime inputs must be supplied explicitly
    # by the caller (chat attachment, Agent-owned database, SDK/API reference).
    # Keeping this boundary in the resolver prevents any UI or legacy client
    # from accidentally reviving the old "scenario data source = business
    # data" semantics.
    return None


def _validate_audit_arguments(
    *,
    invocation_source: str,
    request_id: str | None,
    idempotency_key: str | None,
    release_id: str | None,
    definition_snapshot_id: str | None,
) -> tuple[str, str, str | None]:
    source = str(invocation_source or "internal").strip().lower()
    if source not in _INVOCATION_SOURCES:
        raise RuntimeInputResolutionError(
            "invalid_invocation_source",
            "invocation source is unsupported",
        )
    normalized_request = str(request_id or uuid4().hex).strip()
    if not normalized_request or len(normalized_request) > 64:
        raise RuntimeInputResolutionError(
            "invalid_request_id",
            "request id is invalid",
        )
    normalized_idempotency = None
    if idempotency_key not in (None, ""):
        normalized_idempotency = str(idempotency_key).strip()
        if not normalized_idempotency or len(normalized_idempotency) > 180:
            raise RuntimeInputResolutionError(
                "invalid_idempotency_key",
                "idempotency key is invalid",
            )
    if (release_id in (None, "")) != (definition_snapshot_id in (None, "")):
        raise RuntimeInputResolutionError(
            "invalid_definition_pin",
            "release id and definition snapshot id must be supplied together",
        )
    return source, normalized_request, normalized_idempotency


def _audit_binding(
    *,
    invocation: CapabilityInvocation,
    item: _ResolvedInput,
) -> RunInputBinding:
    return RunInputBinding(
        id=uuid4().hex,
        tenant_id=invocation.tenant_id,
        scenario_id=invocation.scenario_id,
        invocation_id=invocation.id,
        capability_port_id=item.port.id,
        ordinal=0,
        source_kind=item.source_kind,
        inline_document=None,
        asset_version_id=item.asset_version_id,
        source_dataset_version_id=item.source_dataset_version_id,
        dataset_head_id=item.dataset_head_id,
        source_dataset_id=item.source_dataset_id,
        connector_binding_id=item.connector_binding_id,
        resolved_dataset_version_id=item.resolved_dataset_version_id,
        content_hash=item.content_hash,
        schema_hash=item.schema_hash,
        status="ready",
        binding_document=item.safe_document(),
        error="",
    )


def resolve_runtime_inputs(
    db: Session,
    *,
    request: Request,
    deployment: ResolvedDeployment,
    actor: Actor,
    scenario_id: str | None = None,
    environment: str | None = None,
    tenant_id: str | None = None,
    overrides: Any = None,
    invocation_id: str | None = None,
    request_id: str | None = None,
    idempotency_key: str | None = None,
    invocation_source: str = "internal",
    agent_id: str | None = None,
    requested_by_user_id: str | None = None,
    release_id: str | None = None,
    definition_snapshot_id: str | None = None,
) -> RuntimeInputResolution:
    """Resolve all active managed ports and create invocation input audit.

    Resolution priority is invocation override, scenario/environment dataset
    binding, then scenario/environment connector binding.  No legacy physical
    source fallback is performed here; that remains outside the new kernel.
    """

    if not isinstance(request, Request):
        raise RuntimeInputResolutionError(
            "invalid_invocation_request",
            "request must use the immutable capability request contract",
        )
    if not isinstance(deployment, ResolvedDeployment):
        raise RuntimeInputResolutionError(
            "invalid_resolved_deployment",
            "runtime input resolution requires a resolved deployment",
        )
    if not isinstance(actor, Actor):
        raise RuntimeInputResolutionError(
            "invalid_invocation_principal",
            "runtime input resolution requires an authenticated actor",
        )
    try:
        require_request_matches_deployment(request, deployment)
    except DeploymentResolutionError as exc:
        raise RuntimeInputResolutionError(
            "invocation_deployment_changed",
            str(exc),
        ) from exc

    normalized_tenant = _text(deployment.tenant_id, "tenant id")
    normalized_scenario = _text(deployment.scenario_id, "scenario id")
    normalized_environment = _environment(deployment.environment)
    if actor.tenant_id != normalized_tenant:
        raise RuntimeInputResolutionError(
            "principal_scope_mismatch",
            "authenticated actor and deployment belong to different tenants",
        )
    supplied_tenant = tenant_id or getattr(db, "info", {}).get("tenant_id")
    if supplied_tenant not in (None, "") and str(supplied_tenant) != normalized_tenant:
        raise RuntimeInputResolutionError(
            "runtime_scope_mismatch",
            "database tenant scope does not match the resolved deployment",
        )
    if scenario_id not in (None, "") and str(scenario_id) != normalized_scenario:
        raise RuntimeInputResolutionError(
            "runtime_scope_mismatch",
            "requested scenario does not match the resolved deployment",
        )
    if environment not in (None, "") and _environment(environment) != normalized_environment:
        raise RuntimeInputResolutionError(
            "runtime_scope_mismatch",
            "requested environment does not match the resolved deployment",
        )

    capability_kind = str(request.capability.kind or "").strip().lower()
    capability_key = str(request.capability.resource_id or "").strip()
    if not capability_kind or len(capability_kind) > 40:
        raise RuntimeInputResolutionError(
            "invalid_capability_identity",
            "capability kind cannot be represented by the invocation audit",
        )
    if not capability_key or len(capability_key) > 240:
        raise RuntimeInputResolutionError(
            "invalid_capability_identity",
            "capability key cannot be represented by the invocation audit",
        )
    correlation_id = str(request.correlation_id or "").strip()
    if not correlation_id or len(correlation_id) > 240:
        raise RuntimeInputResolutionError(
            "missing_invocation_correlation",
            "capability request must provide a correlation id",
        )
    principal_type = str(actor.actor_type or "").strip().lower()
    principal_id = str(actor.principal_id or "").strip()
    if not principal_type or len(principal_type) > 40:
        raise RuntimeInputResolutionError(
            "invalid_invocation_principal",
            "actor type cannot be represented by the invocation audit",
        )
    if not principal_id or len(principal_id) > 240:
        raise RuntimeInputResolutionError(
            "invalid_invocation_principal",
            "actor principal id cannot be represented by the invocation audit",
        )
    if idempotency_key not in (None, "") and idempotency_key != request.idempotency_key:
        raise RuntimeInputResolutionError(
            "invocation_request_mismatch",
            "idempotency key must come from the immutable capability request",
        )
    normalized_request_seed = request_id or correlation_id
    source, normalized_request_id, normalized_idempotency = _validate_audit_arguments(
        invocation_source=invocation_source,
        request_id=normalized_request_seed,
        idempotency_key=request.idempotency_key,
        release_id=deployment.release_id,
        definition_snapshot_id=deployment.snapshot_id,
    )
    if release_id not in (None, "") and str(release_id) != str(deployment.release_id or ""):
        raise RuntimeInputResolutionError(
            "runtime_scope_mismatch",
            "release id does not match the resolved deployment",
        )
    if (
        definition_snapshot_id not in (None, "")
        and str(definition_snapshot_id) != str(deployment.snapshot_id or "")
    ):
        raise RuntimeInputResolutionError(
            "runtime_scope_mismatch",
            "definition snapshot id does not match the resolved deployment",
        )
    if overrides in (None, (), [], {}):
        overrides = request.binding_overrides
    elif request.binding_overrides:
        raise RuntimeInputResolutionError(
            "invalid_override_shape",
            "supply overrides either directly or through request, not both",
        )

    ports = load_runtime_input_ports(
        db,
        tenant_id=normalized_tenant,
        scenario_id=normalized_scenario,
        environment=normalized_environment,
        capability=request.capability,
        definition=deployment.definition,
    )
    port_by_key = {str(item.port_key).strip().lower(): item for item in ports}
    override_by_key = _normalize_overrides(overrides)
    unknown_ports = sorted(set(override_by_key) - set(port_by_key))
    if unknown_ports:
        raise RuntimeInputResolutionError(
            "runtime_input_port_not_found",
            "runtime input override targets an unavailable port",
            details={"port_keys": unknown_ports},
        )

    resolved: list[_ResolvedInput] = []
    missing: list[str] = []
    for port_key, port in port_by_key.items():
        override = override_by_key.get(port_key)
        if override is not None:
            if not _allows_override(port):
                raise RuntimeInputResolutionError(
                    "runtime_input_override_forbidden",
                    "port policy does not allow invocation-time override",
                    port_key=port_key,
                )
            item = _resolve_reference(
                db,
                reference=override,
                tenant_id=normalized_tenant,
                scenario_id=normalized_scenario,
                environment=normalized_environment,
                port=port,
                resolution_source="invocation_override",
            )
        else:
            item = _scenario_default(
                db,
                tenant_id=normalized_tenant,
                scenario_id=normalized_scenario,
                environment=normalized_environment,
                port=port,
            )
        if item is None:
            if bool(port.is_required):
                missing.append(port_key)
            continue
        resolved.append(item)

    if missing:
        raise RuntimeInputResolutionError(
            "required_runtime_inputs_missing",
            "required runtime input ports are not bound",
            details={"missing_ports": sorted(missing)},
        )

    contracts = tuple(_data_port(port) for port in ports)
    try:
        context = resolve_runtime_data_context(
            contracts,
            tuple(item.handle for item in resolved),
        )
    except DeploymentResolutionError as exc:
        raise RuntimeInputResolutionError(
            "runtime_data_context_invalid",
            str(exc),
        ) from exc

    safe_inputs = [item.safe_document() for item in resolved]
    input_hash = canonical_hash(
        {
            "environment": normalized_environment,
            "managed_inputs": safe_inputs,
            "scenario_id": normalized_scenario,
            "tenant_id": normalized_tenant,
        },
        domain="capability-invocation-input-v1",
    )
    invocation = CapabilityInvocation(
        id=str(invocation_id or uuid4().hex),
        tenant_id=normalized_tenant,
        scenario_id=normalized_scenario,
        agent_id=agent_id,
        requested_by_user_id=requested_by_user_id,
        release_id=deployment.release_id,
        definition_snapshot_id=deployment.snapshot_id,
        environment=normalized_environment,
        capability_kind=capability_kind,
        capability_key=capability_key,
        definition_hash=deployment.definition_hash,
        deployment_fingerprint=deployment.fingerprint,
        data_context_fingerprint=context.fingerprint,
        correlation_id=correlation_id,
        principal_type=principal_type,
        principal_id=principal_id,
        invocation_source=source,
        request_id=normalized_request_id,
        idempotency_key=normalized_idempotency,
        input_hash=input_hash,
        status="pending",
        request_document={
            "contract": "managed-runtime-inputs/v1",
            "capability": {
                "kind": capability_kind,
                "key": capability_key,
            },
            "correlation_id": correlation_id,
            "definition_hash": deployment.definition_hash,
            "deployment_fingerprint": deployment.fingerprint,
            "managed_inputs": safe_inputs,
            "principal": {
                "id": principal_id,
                "type": principal_type,
            },
            "runtime_data_context_fingerprint": context.fingerprint,
        },
        result_document={},
        error_code="",
        error_message="",
    )
    db.add(invocation)
    db.flush()
    bindings = tuple(_audit_binding(invocation=invocation, item=item) for item in resolved)
    if bindings:
        db.add_all(bindings)
        db.flush()
    return RuntimeInputResolution(
        invocation=invocation,
        input_bindings=bindings,
        runtime_data_context=context,
        data_ports=contracts,
    )


resolve_invocation_inputs = resolve_runtime_inputs
resolve_and_audit_runtime_inputs = resolve_runtime_inputs


class BindingResolver:
    """Small injectable facade for callers that keep a database session."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def resolve(self, **kwargs: Any) -> RuntimeInputResolution:
        return resolve_runtime_inputs(self._db, **kwargs)


__all__ = [
    "BindingResolver",
    "DeploymentInputResolution",
    "ManagedInputOption",
    "RuntimeInputError",
    "RuntimeInputResolution",
    "RuntimeInputResolutionError",
    "load_runtime_input_ports",
    "list_managed_input_options",
    "resolve_and_audit_runtime_inputs",
    "resolve_deployment_inputs",
    "resolve_invocation_inputs",
    "resolve_runtime_inputs",
]
