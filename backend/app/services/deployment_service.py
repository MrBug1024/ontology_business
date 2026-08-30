"""Pure deployment and runtime-data resolution for capability execution.

This module intentionally has no ORM, router, connector, or database imports.
Callers provide an already-authorized definition plus duck-typed, credential-
free binding records.  Only server-computed binding signatures enter the
deployment fingerprint; connector ids and other audit references remain
separate and never influence that identity directly.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .capability_contracts import (
    BindingOverride,
    CapabilityContractError,
    DataPort,
    Request,
    ResolvedDataHandle,
    ResolvedDeployment,
    RuntimeDataContext,
)


class DeploymentResolutionError(CapabilityContractError):
    """A definition and its governed bindings cannot form a deployment."""


_MISSING = object()


def _read(value: Any, *names: str, default: Any = _MISSING) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    if default is not _MISSING:
        return default
    raise DeploymentResolutionError(
        "runtime object is missing required field: " + " or ".join(names)
    )


def _text(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise DeploymentResolutionError(f"{label} is required")
    return normalized


def _coerce_port(value: Any) -> DataPort:
    if isinstance(value, DataPort):
        return value
    binding_kinds = _read(value, "binding_kinds", default=()) or ()
    if isinstance(binding_kinds, str):
        binding_kinds = (binding_kinds,)
    override_policy = _read(value, "override_policy", default=None)
    if override_policy in (None, ""):
        override_policy = (
            "managed-reference"
            if bool(_read(value, "allow_override", default=False))
            else "forbidden"
        )
    try:
        return DataPort(
            key=_read(value, "key", "port_key", "binding_key"),
            modality=_read(value, "modality", "kind", default="object"),
            schema=_read(value, "schema", "contract", default={}) or {},
            schema_hash=_read(value, "schema_hash", default="") or "",
            required=bool(_read(value, "required", "is_required", default=True)),
            binding_kinds=tuple(binding_kinds),
            override_policy=str(override_policy),
            description=str(_read(value, "description", default="") or ""),
        )
    except CapabilityContractError as exc:
        raise DeploymentResolutionError(str(exc)) from exc


def _coerce_handle(value: Any) -> ResolvedDataHandle:
    if isinstance(value, ResolvedDataHandle):
        return value
    try:
        return ResolvedDataHandle(
            port_key=_read(value, "port_key", "binding_key"),
            binding_kind=_read(
                value, "binding_kind", "connector_kind", "kind"
            ),
            reference_id=_read(
                value,
                "reference_id",
                "connector_id",
                "dataset_version_id",
                "artifact_id",
                "id",
            ),
            # Never derive a signature from config or serialize the binding
            # object.  A missing server-computed signature is a hard failure.
            signature=_read(
                value,
                "signature",
                "connector_signature",
                "binding_signature",
            ),
            version_id=_read(
                value,
                "version_id",
                "dataset_version_id",
                default=None,
            ),
        )
    except CapabilityContractError as exc:
        raise DeploymentResolutionError(str(exc)) from exc


def _override_handle(value: BindingOverride) -> ResolvedDataHandle:
    if not isinstance(value, BindingOverride):
        raise DeploymentResolutionError(
            "binding overrides must be immutable BindingOverride values"
        )
    if value.reference_id is None or value.signature is None or value.binding_key is not None:
        raise DeploymentResolutionError(
            "deployment overrides require an immutable reference id and signature"
        )
    return ResolvedDataHandle(
        port_key=value.port_key,
        binding_kind=value.binding_kind,
        reference_id=value.reference_id,
        signature=value.signature,
        version_id=value.version_id,
    )


def normalize_data_ports(values: Iterable[Any]) -> tuple[DataPort, ...]:
    ports = tuple(_coerce_port(value) for value in values)
    keys = [item.key for item in ports]
    if len(keys) != len(set(keys)):
        raise DeploymentResolutionError("deployment declares duplicate data ports")
    return tuple(sorted(ports, key=lambda item: item.key))


def resolve_runtime_data_context(
    data_ports: Iterable[Any],
    bindings: Iterable[Any] = (),
    *,
    overrides: Iterable[BindingOverride] = (),
) -> RuntimeDataContext:
    """Resolve deployment bindings and allowed invocation overrides.

    The function accepts dictionaries, ``SimpleNamespace`` objects, ORM-like
    rows, or the immutable contracts themselves.  It reads only a small fixed
    set of safe identity fields and never inspects a connector ``config``.
    """

    ports = normalize_data_ports(data_ports)
    by_port = {item.key: item for item in ports}
    resolved: dict[str, ResolvedDataHandle] = {}

    for raw_binding in bindings:
        handle = _coerce_handle(raw_binding)
        port = by_port.get(handle.port_key)
        if port is None:
            raise DeploymentResolutionError(
                f"binding targets undeclared data port: {handle.port_key}"
            )
        if handle.port_key in resolved:
            raise DeploymentResolutionError(
                f"multiple bindings target data port: {handle.port_key}"
            )
        if port.binding_kinds and handle.binding_kind not in port.binding_kinds:
            raise DeploymentResolutionError(
                f"binding kind {handle.binding_kind} is not allowed for port {port.key}"
            )
        resolved[handle.port_key] = handle

    seen_overrides: set[str] = set()
    for raw_override in overrides:
        handle = _override_handle(raw_override)
        if handle.port_key in seen_overrides:
            raise DeploymentResolutionError(
                f"multiple overrides target data port: {handle.port_key}"
            )
        seen_overrides.add(handle.port_key)
        port = by_port.get(handle.port_key)
        if port is None:
            raise DeploymentResolutionError(
                f"override targets undeclared data port: {handle.port_key}"
            )
        if not port.allows_override:
            raise DeploymentResolutionError(
                f"data port does not allow invocation override: {handle.port_key}"
            )
        if port.binding_kinds and handle.binding_kind not in port.binding_kinds:
            raise DeploymentResolutionError(
                f"override kind {handle.binding_kind} is not allowed for port {port.key}"
            )
        resolved[handle.port_key] = handle

    missing = [
        item.key for item in ports if item.required and item.key not in resolved
    ]
    if missing:
        raise DeploymentResolutionError(
            "required data ports are not bound: " + ", ".join(sorted(missing))
        )
    return RuntimeDataContext(tuple(resolved.values()))


def _definition_scenario(definition: Any) -> Any | None:
    return _read(definition, "scenario", default=None)


def build_resolved_deployment(
    definition: Any,
    *,
    data_ports: Iterable[Any] = (),
    bindings: Iterable[Any] = (),
    overrides: Iterable[BindingOverride] = (),
    scenario_id: str | None = None,
    tenant_id: str | None = None,
    environment: str | None = None,
) -> ResolvedDeployment:
    """Build an immutable deployment from a duck-typed runtime definition."""

    if definition is None:
        raise DeploymentResolutionError("runtime definition is required")
    scenario = _definition_scenario(definition)
    resolved_scenario_id = scenario_id or _read(
        definition, "scenario_id", default=None
    ) or (_read(scenario, "id", default=None) if scenario is not None else None)
    resolved_tenant_id = tenant_id or _read(
        definition, "tenant_id", default=None
    ) or (
        _read(scenario, "tenant_id", default=None)
        if scenario is not None
        else None
    )
    resolved_environment = environment or _read(
        definition, "environment", default=None
    )
    ports = normalize_data_ports(data_ports)
    context = resolve_runtime_data_context(
        ports,
        bindings,
        overrides=overrides,
    )
    try:
        return ResolvedDeployment(
            scenario_id=_text(resolved_scenario_id, "deployment scenario id"),
            tenant_id=_text(resolved_tenant_id, "deployment tenant id"),
            environment=_text(resolved_environment, "deployment environment"),
            definition_hash=_read(definition, "definition_hash"),
            definition=definition,
            data_ports=ports,
            data_context=context,
            definition_source=_read(definition, "source", default="live") or "live",
            snapshot_id=_read(definition, "snapshot_id", default=None),
            release_id=_read(definition, "release_id", default=None),
        )
    except CapabilityContractError as exc:
        raise DeploymentResolutionError(str(exc)) from exc


resolve_deployment = build_resolved_deployment


def require_request_matches_deployment(
    request: Request,
    deployment: ResolvedDeployment,
) -> None:
    """Apply optional optimistic definition/deployment pins from a request."""

    if not isinstance(request, Request):
        raise DeploymentResolutionError("invocation request must be a Request")
    if not isinstance(deployment, ResolvedDeployment):
        raise DeploymentResolutionError(
            "invocation deployment must be a ResolvedDeployment"
        )
    if (
        request.expected_definition_hash is not None
        and request.expected_definition_hash != deployment.definition_hash
    ):
        raise DeploymentResolutionError(
            "runtime definition changed after the invocation was prepared"
        )
    if (
        request.expected_deployment_fingerprint is not None
        and request.expected_deployment_fingerprint != deployment.fingerprint
    ):
        raise DeploymentResolutionError(
            "runtime deployment bindings changed after the invocation was prepared"
        )


__all__ = [
    "DeploymentResolutionError",
    "build_resolved_deployment",
    "normalize_data_ports",
    "require_request_matches_deployment",
    "resolve_deployment",
    "resolve_runtime_data_context",
]
