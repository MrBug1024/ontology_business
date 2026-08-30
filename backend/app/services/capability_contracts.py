"""Immutable, protocol-neutral contracts for capability execution.

The contracts in this module deliberately know nothing about HTTP, MCP,
SQLAlchemy models, connector credentials, or any particular business domain.
They form the small boundary shared by a future invoker, protocol adapters,
and trusted capability providers.

Only JSON-like, deterministic values are accepted in hashed or persisted
contract fields.  Runtime definitions remain opaque: ``ResolvedDeployment``
pins their already-computed definition hash without attempting to serialize a
mutable ORM object.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any
from uuid import UUID


_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HASH_DOMAIN_PREFIX = b"ontology-platform/capability-kernel/"


class CapabilityContractError(ValueError):
    """A protocol-neutral capability contract is invalid."""


def _text(value: Any, label: str, *, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum:
        raise CapabilityContractError(
            f"{label} must contain between 1 and {maximum} characters"
        )
    return normalized


def _optional_text(value: Any, label: str, *, maximum: int) -> str | None:
    if value in (None, ""):
        return None
    return _text(value, label, maximum=maximum)


def _token(value: Any, label: str) -> str:
    normalized = _text(value, label, maximum=128).casefold()
    if not _TOKEN_RE.fullmatch(normalized):
        raise CapabilityContractError(
            f"{label} must be a lowercase portable token"
        )
    return normalized


def _sha256(value: Any, label: str, *, optional: bool = False) -> str:
    if optional and value in (None, ""):
        return ""
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise CapabilityContractError(f"{label} must be a lowercase SHA-256 digest")
    return normalized


def _canonical_value(value: Any) -> Any:
    """Return a deterministic JSON-compatible representation.

    Arbitrary objects are rejected instead of being stringified.  Calling
    ``str(object)`` would make hashes depend on memory addresses or accidentally
    include a model/connector representation containing credentials.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CapabilityContractError("canonical values cannot contain NaN or infinity")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CapabilityContractError("canonical values cannot contain non-finite decimals")
        return {"$decimal": format(value, "f")}
    if isinstance(value, datetime):
        normalized = value
        if value.tzinfo is not None:
            normalized = value.astimezone(timezone.utc)
        return {"$datetime": normalized.isoformat().replace("+00:00", "Z")}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, UUID):
        return {"$uuid": str(value)}
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _canonical_value(getattr(value, item.name))
            for item in dataclasses.fields(value)
            if item.init
        }
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _canonical_value(model_dump(mode="json"))
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise CapabilityContractError("canonical object keys must be strings")
            if raw_key in normalized:
                raise CapabilityContractError("canonical object keys must be unique")
            normalized[raw_key] = _canonical_value(raw_value)
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, Set) and not isinstance(value, (str, bytes, bytearray)):
        items = [_canonical_value(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_canonical_value(item) for item in value]
    raise CapabilityContractError(
        f"unsupported canonical value type: {type(value).__name__}"
    )


def canonical_json(value: Any) -> str:
    """Serialize a value with stable ordering and no implicit coercions."""

    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_hash(value: Any, *, domain: str = "contract-v1") -> str:
    """Return a domain-separated SHA-256 digest of a canonical value."""

    normalized_domain = _token(domain, "hash domain")
    payload = canonical_json(value).encode("utf-8")
    return hashlib.sha256(
        _HASH_DOMAIN_PREFIX
        + normalized_domain.encode("ascii")
        + b"\0"
        + payload
    ).hexdigest()


def _freeze(value: Any) -> Any:
    """Recursively make JSON-like contract values immutable."""

    if value is None or isinstance(
        value, (str, bool, int, Decimal, datetime, date, UUID, Enum)
    ):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CapabilityContractError("contract values cannot contain NaN or infinity")
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _freeze(model_dump(mode="json"))
    if isinstance(value, Mapping):
        items: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise CapabilityContractError("contract object keys must be strings")
            items[raw_key] = _freeze(raw_value)
        return MappingProxyType({key: items[key] for key in sorted(items)})
    if isinstance(value, Set) and not isinstance(value, (str, bytes, bytearray)):
        frozen = [_freeze(item) for item in value]
        return tuple(sorted(frozen, key=canonical_json))
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return tuple(_freeze(item) for item in value)
    raise CapabilityContractError(
        f"unsupported immutable contract value type: {type(value).__name__}"
    )


def _tokens(values: Sequence[str] | Set[str], label: str) -> tuple[str, ...]:
    return tuple(sorted({_token(value, label) for value in values}))


@dataclass(frozen=True, slots=True)
class CapabilityRef:
    """Stable reference to a capability inside a resolved definition."""

    kind: str
    resource_id: str
    api_name: str | None = None
    provider_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _token(self.kind, "capability kind"))
        object.__setattr__(
            self,
            "resource_id",
            _text(self.resource_id, "capability resource id", maximum=240),
        )
        api_name = _optional_text(self.api_name, "capability api name", maximum=240)
        provider_key = (
            _token(self.provider_key, "provider key")
            if self.provider_key not in (None, "")
            else None
        )
        object.__setattr__(self, "api_name", api_name)
        object.__setattr__(self, "provider_key", provider_key)

    @property
    def public_name(self) -> str:
        return self.api_name or self.resource_id


@dataclass(frozen=True, slots=True)
class DataPort:
    """A definition-time requirement, never a physical data-source binding."""

    key: str
    modality: str
    schema: Mapping[str, Any] = field(default_factory=dict)
    schema_hash: str = ""
    required: bool = True
    binding_kinds: tuple[str, ...] = ()
    override_policy: str = "forbidden"
    description: str = ""

    def __post_init__(self) -> None:
        key = _token(self.key, "data port key")
        modality = _token(self.modality, "data port modality")
        frozen_schema = _freeze(self.schema)
        schema_hash = (
            _sha256(self.schema_hash, "data port schema hash")
            if self.schema_hash
            else canonical_hash(frozen_schema, domain="data-port-schema-v1")
        )
        kinds = _tokens(self.binding_kinds, "binding kind")
        policy = _token(self.override_policy, "override policy")
        if policy not in {"forbidden", "managed-reference"}:
            raise CapabilityContractError(
                "override policy must be forbidden or managed-reference"
            )
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "modality", modality)
        object.__setattr__(self, "schema", frozen_schema)
        object.__setattr__(self, "schema_hash", schema_hash)
        object.__setattr__(self, "binding_kinds", kinds)
        object.__setattr__(self, "override_policy", policy)
        object.__setattr__(
            self,
            "description",
            str(self.description or "").strip()[:2_000],
        )

    @property
    def allows_override(self) -> bool:
        return self.override_policy == "managed-reference"


@dataclass(frozen=True, slots=True)
class DataBindingOverride:
    """Invocation-time selection of an already governed data reference."""

    port_key: str
    binding_kind: str
    reference_id: str | None = None
    signature: str | None = None
    version_id: str | None = None
    binding_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "port_key", _token(self.port_key, "data port key"))
        binding_kind = _token(self.binding_kind, "binding kind")
        object.__setattr__(self, "binding_kind", binding_kind)
        reference_id = _optional_text(
            self.reference_id,
            "managed reference id",
            maximum=240,
        )
        binding_key = _optional_text(
            self.binding_key,
            "managed binding key",
            maximum=180,
        )
        if (reference_id is None) == (binding_key is None):
            raise CapabilityContractError(
                "managed binding override requires exactly one selector"
            )
        if binding_key is not None and binding_kind != "connector_binding":
            raise CapabilityContractError(
                "managed binding key can select only a connector binding"
            )
        object.__setattr__(
            self,
            "reference_id",
            reference_id,
        )
        object.__setattr__(
            self,
            "signature",
            _sha256(
                self.signature,
                "expected managed binding signature",
                optional=True,
            )
            or None,
        )
        object.__setattr__(
            self,
            "version_id",
            _optional_text(self.version_id, "managed reference version", maximum=240),
        )
        object.__setattr__(self, "binding_key", binding_key)

    @property
    def selector(self) -> str:
        return "binding_key" if self.binding_key is not None else "reference_id"

    @property
    def selector_value(self) -> str:
        return self.binding_key or self.reference_id or ""


@dataclass(frozen=True, slots=True)
class Actor:
    """Authenticated identity facts supplied by an adapter, without secrets."""

    actor_type: str
    principal_id: str
    tenant_id: str
    user_id: str | None = None
    agent_id: str | None = None
    client_id: str | None = None
    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "actor_type", _token(self.actor_type, "actor type"))
        object.__setattr__(
            self, "principal_id", _text(self.principal_id, "principal id", maximum=240)
        )
        object.__setattr__(
            self, "tenant_id", _text(self.tenant_id, "tenant id", maximum=240)
        )
        for name in ("user_id", "agent_id", "client_id"):
            object.__setattr__(
                self,
                name,
                _optional_text(getattr(self, name), name.replace("_", " "), maximum=240),
            )
        object.__setattr__(self, "roles", _tokens(self.roles, "actor role"))
        object.__setattr__(self, "scopes", _tokens(self.scopes, "actor scope"))


BindingOverride = DataBindingOverride


@dataclass(frozen=True, slots=True)
class CapabilityInvocationRequest:
    """Protocol-neutral request accepted by a future CapabilityInvoker."""

    capability: CapabilityRef
    inputs: Mapping[str, Any] = field(default_factory=dict)
    binding_overrides: tuple[DataBindingOverride, ...] = ()
    mode: str = "execute"
    idempotency_key: str | None = None
    correlation_id: str | None = None
    expected_definition_hash: str | None = None
    expected_deployment_fingerprint: str | None = None
    confirmation: Mapping[str, Any] = field(default_factory=dict)
    request_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.capability, CapabilityRef):
            raise CapabilityContractError("request capability must be a CapabilityRef")
        frozen_inputs = _freeze(self.inputs)
        frozen_confirmation = _freeze(self.confirmation)
        overrides = tuple(self.binding_overrides)
        if any(not isinstance(item, DataBindingOverride) for item in overrides):
            raise CapabilityContractError(
                "request binding overrides must be BindingOverride values"
            )
        port_keys = [item.port_key for item in overrides]
        if len(port_keys) != len(set(port_keys)):
            raise CapabilityContractError("request contains duplicate data-port overrides")
        object.__setattr__(self, "inputs", frozen_inputs)
        object.__setattr__(self, "confirmation", frozen_confirmation)
        object.__setattr__(
            self,
            "binding_overrides",
            tuple(sorted(overrides, key=lambda item: item.port_key)),
        )
        object.__setattr__(self, "mode", _token(self.mode, "invocation mode"))
        object.__setattr__(
            self,
            "idempotency_key",
            _optional_text(self.idempotency_key, "idempotency key", maximum=240),
        )
        object.__setattr__(
            self,
            "correlation_id",
            _optional_text(self.correlation_id, "correlation id", maximum=240),
        )
        object.__setattr__(
            self,
            "expected_definition_hash",
            _sha256(
                self.expected_definition_hash,
                "expected definition hash",
                optional=True,
            )
            or None,
        )
        object.__setattr__(
            self,
            "expected_deployment_fingerprint",
            _sha256(
                self.expected_deployment_fingerprint,
                "expected deployment fingerprint",
                optional=True,
            )
            or None,
        )
        object.__setattr__(
            self,
            "request_id",
            _optional_text(self.request_id, "request id", maximum=64),
        )


Request = CapabilityInvocationRequest
CapabilityRequest = CapabilityInvocationRequest


@dataclass(frozen=True, slots=True)
class ResolvedDataHandle:
    """Credential-free runtime resolution of one declared data port."""

    port_key: str
    binding_kind: str
    reference_id: str
    signature: str
    version_id: str | None = None

    def __post_init__(self) -> None:
        normalized = DataBindingOverride(
            port_key=self.port_key,
            binding_kind=self.binding_kind,
            reference_id=self.reference_id,
            signature=self.signature,
            version_id=self.version_id,
        )
        if normalized.reference_id is None or normalized.signature is None:
            raise CapabilityContractError(
                "resolved data handle requires a server-computed reference signature"
            )
        for name in (
            "port_key",
            "binding_kind",
            "reference_id",
            "signature",
            "version_id",
        ):
            object.__setattr__(self, name, getattr(normalized, name))

    def signature_fact(self) -> Mapping[str, str]:
        """Return the only binding facts admitted to deployment hashes."""

        return MappingProxyType(
            {
                "binding_kind": self.binding_kind,
                "port_key": self.port_key,
                "signature": self.signature,
            }
        )

    def audit_fact(self) -> Mapping[str, str | None]:
        return MappingProxyType(
            {
                "binding_kind": self.binding_kind,
                "port_key": self.port_key,
                "reference_id": self.reference_id,
                "signature": self.signature,
                "version_id": self.version_id,
            }
        )


@dataclass(frozen=True, slots=True)
class RuntimeDataContext:
    """Resolved data handles for one deployment or invocation."""

    handles: tuple[ResolvedDataHandle, ...] = ()
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        handles = tuple(self.handles)
        if any(not isinstance(item, ResolvedDataHandle) for item in handles):
            raise CapabilityContractError(
                "runtime data context handles must be ResolvedDataHandle values"
            )
        port_keys = [item.port_key for item in handles]
        if len(port_keys) != len(set(port_keys)):
            raise CapabilityContractError("runtime data context contains duplicate ports")
        ordered = tuple(sorted(handles, key=lambda item: item.port_key))
        object.__setattr__(self, "handles", ordered)
        object.__setattr__(
            self,
            "fingerprint",
            canonical_hash(
                [dict(item.signature_fact()) for item in ordered],
                domain="runtime-data-context-v1",
            ),
        )

    def get(self, port_key: str) -> ResolvedDataHandle | None:
        normalized = _token(port_key, "data port key")
        return next((item for item in self.handles if item.port_key == normalized), None)

    def signature_facts(self) -> tuple[Mapping[str, str], ...]:
        return tuple(item.signature_fact() for item in self.handles)


@dataclass(frozen=True, slots=True)
class ResolvedDeployment:
    """Immutable wrapper around one definition and its sanitized bindings."""

    scenario_id: str
    tenant_id: str
    environment: str
    definition_hash: str
    definition: Any = field(repr=False, compare=False)
    data_ports: tuple[DataPort, ...] = ()
    data_context: RuntimeDataContext = field(default_factory=RuntimeDataContext)
    definition_source: str = "live"
    snapshot_id: str | None = None
    release_id: str | None = None
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        scenario_id = _text(self.scenario_id, "deployment scenario id", maximum=240)
        tenant_id = _text(self.tenant_id, "deployment tenant id", maximum=240)
        environment = _token(self.environment, "deployment environment")
        definition_hash = _sha256(self.definition_hash, "definition hash")
        definition_source = _token(self.definition_source, "definition source")
        ports = tuple(self.data_ports)
        if any(not isinstance(item, DataPort) for item in ports):
            raise CapabilityContractError("deployment data ports must be DataPort values")
        port_keys = [item.key for item in ports]
        if len(port_keys) != len(set(port_keys)):
            raise CapabilityContractError("deployment contains duplicate data ports")
        ordered_ports = tuple(sorted(ports, key=lambda item: item.key))
        if not isinstance(self.data_context, RuntimeDataContext):
            raise CapabilityContractError(
                "deployment data context must be a RuntimeDataContext"
            )
        unknown = set(item.port_key for item in self.data_context.handles) - set(port_keys)
        if unknown:
            raise CapabilityContractError(
                "deployment data context contains undeclared ports: "
                + ", ".join(sorted(unknown))
            )
        object.__setattr__(self, "scenario_id", scenario_id)
        object.__setattr__(self, "tenant_id", tenant_id)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "definition_hash", definition_hash)
        object.__setattr__(self, "definition_source", definition_source)
        object.__setattr__(self, "data_ports", ordered_ports)
        object.__setattr__(
            self,
            "snapshot_id",
            _optional_text(self.snapshot_id, "snapshot id", maximum=240),
        )
        object.__setattr__(
            self,
            "release_id",
            _optional_text(self.release_id, "release id", maximum=240),
        )
        # Physical ids, revisions, connector configurations and credentials are
        # deliberately absent.  Bindings contribute only their sanitized,
        # server-computed signature facts.
        fingerprint_payload = {
            "definition_hash": definition_hash,
            "definition_source": definition_source,
            "environment": environment,
            "release_id": self.release_id,
            "scenario_id": scenario_id,
            "snapshot_id": self.snapshot_id,
            "tenant_id": tenant_id,
            "binding_signatures": [
                dict(item) for item in self.data_context.signature_facts()
            ],
        }
        object.__setattr__(
            self,
            "fingerprint",
            canonical_hash(fingerprint_payload, domain="resolved-deployment-v1"),
        )

    def port(self, key: str) -> DataPort | None:
        normalized = _token(key, "data port key")
        return next((item for item in self.data_ports if item.key == normalized), None)


@dataclass(frozen=True, slots=True)
class Receipt:
    """Protocol-neutral invocation result and its safe provenance."""

    invocation_id: str
    status: str
    capability: CapabilityRef
    definition_hash: str
    deployment_fingerprint: str
    data_context_fingerprint: str
    output: Any = field(default_factory=dict)
    audit_ref: Mapping[str, Any] = field(default_factory=dict)
    confirmation: Mapping[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "invocation_id",
            _text(self.invocation_id, "invocation id", maximum=240),
        )
        object.__setattr__(self, "status", _token(self.status, "invocation status"))
        if not isinstance(self.capability, CapabilityRef):
            raise CapabilityContractError("receipt capability must be a CapabilityRef")
        object.__setattr__(
            self, "definition_hash", _sha256(self.definition_hash, "definition hash")
        )
        object.__setattr__(
            self,
            "deployment_fingerprint",
            _sha256(self.deployment_fingerprint, "deployment fingerprint"),
        )
        object.__setattr__(
            self,
            "data_context_fingerprint",
            _sha256(self.data_context_fingerprint, "data context fingerprint"),
        )
        object.__setattr__(self, "output", _freeze(self.output))
        object.__setattr__(self, "audit_ref", _freeze(self.audit_ref))
        object.__setattr__(self, "confirmation", _freeze(self.confirmation))
        object.__setattr__(
            self,
            "error_code",
            _token(self.error_code, "error code")
            if self.error_code not in (None, "")
            else None,
        )
        object.__setattr__(
            self, "error_message", str(self.error_message or "").strip()[:4_000]
        )


CapabilityReceipt = Receipt


__all__ = [
    "Actor",
    "BindingOverride",
    "CapabilityInvocationRequest",
    "CapabilityContractError",
    "CapabilityReceipt",
    "CapabilityRef",
    "CapabilityRequest",
    "DataBindingOverride",
    "DataPort",
    "Receipt",
    "Request",
    "ResolvedDataHandle",
    "ResolvedDeployment",
    "RuntimeDataContext",
    "canonical_hash",
    "canonical_json",
]
