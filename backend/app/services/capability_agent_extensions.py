"""Optional Agent-facing hooks implemented by trusted capability providers.

The capability kernel remains the execution authority for versioned calls.
These hooks exist only for the legacy validation-Agent loop while deployments
migrate to that kernel.  The loop knows tool and grounding contracts, but it
never selects providers by scenario names, business fields, or tool aliases.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .capability_contracts import canonical_json

if TYPE_CHECKING:
    from .capability_registry import CapabilityProviderRegistry
    from .capability_contracts import RuntimeDataContext


_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_SAFE_ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_PROVIDER_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


class AgentProviderExtensionError(ValueError):
    """A trusted Provider supplied an invalid Agent extension contract."""


class AgentProviderToolError(ValueError):
    """A safe, model-correctable failure returned by a Provider tool."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        normalized_code = str(code or "TOOL_EXECUTION_FAILED").strip().upper()
        if not _SAFE_ERROR_CODE_RE.fullmatch(normalized_code):
            normalized_code = "TOOL_EXECUTION_FAILED"
        super().__init__(str(message or "Provider tool execution failed").strip())
        self.code = normalized_code
        self.message = str(message or "Provider tool execution failed").strip()[:500]
        self.retryable = bool(retryable)


def _provider_key(value: Any) -> str:
    key = str(value or "").strip().casefold()
    if not _PROVIDER_KEY_RE.fullmatch(key):
        raise AgentProviderExtensionError("provider key is invalid")
    return key


def _plain_mapping(value: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    try:
        plain = json.loads(canonical_json(value))
    except Exception as exc:  # noqa: BLE001 - invalid Provider data stays internal.
        raise AgentProviderExtensionError(f"{label} must be stable JSON") from exc
    if not isinstance(plain, dict):
        raise AgentProviderExtensionError(f"{label} must be an object")
    return MappingProxyType(plain)


@dataclass(frozen=True, slots=True)
class AgentToolSpec:
    """One provider-owned tool exposed to the legacy validation Agent."""

    name: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        description = str(self.description or "").strip()
        if not _TOOL_NAME_RE.fullmatch(name):
            raise AgentProviderExtensionError("provider Agent tool name is invalid")
        if not description or len(description) > 4_000:
            raise AgentProviderExtensionError(
                "provider Agent tool description must contain 1 to 4000 characters"
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", description)
        object.__setattr__(
            self,
            "parameters",
            _plain_mapping(self.parameters, "provider Agent tool parameters"),
        )


@dataclass(frozen=True, slots=True)
class LegacyCapabilityMatch:
    """Trusted proof that one legacy tool call represents one capability call.

    Providers own legacy aliases and any compatibility normalization.  The
    migration kernel receives only the normalized capability identity, typed
    inputs and comparison value; it never contains scenario or tool aliases.
    """

    owner_key: str
    owner_version: str
    capability_kind: str
    capability_key: str
    inputs: Mapping[str, Any]
    comparison_result: Any

    def __post_init__(self) -> None:
        owner_key = _provider_key(self.owner_key)
        owner_version = str(self.owner_version or "").strip()
        capability_kind = str(self.capability_kind or "").strip().casefold()
        capability_key = str(self.capability_key or "").strip()
        if not owner_version or len(owner_version) > 80:
            raise AgentProviderExtensionError("legacy match owner version is invalid")
        if capability_kind not in {"function", "action", "workflow"}:
            raise AgentProviderExtensionError("legacy match capability kind is invalid")
        if not capability_key or len(capability_key) > 240:
            raise AgentProviderExtensionError("legacy match capability key is invalid")
        try:
            inputs = json.loads(canonical_json(self.inputs))
            comparison_result = json.loads(canonical_json(self.comparison_result))
        except Exception as exc:  # noqa: BLE001 - invalid Provider data stays internal.
            raise AgentProviderExtensionError(
                "legacy match values must be stable JSON"
            ) from exc
        if not isinstance(inputs, dict):
            raise AgentProviderExtensionError("legacy match inputs must be an object")
        object.__setattr__(self, "owner_key", owner_key)
        object.__setattr__(self, "owner_version", owner_version)
        object.__setattr__(self, "capability_kind", capability_kind)
        object.__setattr__(self, "capability_key", capability_key)
        object.__setattr__(self, "inputs", MappingProxyType(inputs))
        object.__setattr__(self, "comparison_result", comparison_result)


@dataclass(frozen=True, slots=True)
class GroundingResult:
    """Provider-neutral deterministic evidence consumed by an Agent loop."""

    provider_key: str
    provider_version: str
    verified: bool
    status_lines: tuple[str, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        key = _provider_key(self.provider_key)
        version = str(self.provider_version or "").strip()
        if not version or len(version) > 80:
            raise AgentProviderExtensionError("provider grounding version is invalid")
        lines = tuple(
            line
            for raw in self.status_lines
            if (line := " ".join(str(raw or "").split())[:1_000])
        )
        object.__setattr__(self, "provider_key", key)
        object.__setattr__(self, "provider_version", version)
        object.__setattr__(self, "verified", bool(self.verified))
        object.__setattr__(self, "status_lines", lines)
        object.__setattr__(
            self,
            "provenance",
            _plain_mapping(self.provenance, "provider grounding provenance"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract": "grounding-result/v1",
            "provider_key": self.provider_key,
            "provider_version": self.provider_version,
            "verified": self.verified,
            "status_lines": list(self.status_lines),
            "provenance": json.loads(canonical_json(self.provenance)),
        }


@runtime_checkable
class BoundAgentProvider(Protocol):
    """Request-local legacy Agent extension returned by a trusted Provider."""

    provider_key: str
    provider_version: str

    def agent_tools(self) -> Sequence[AgentToolSpec]: ...

    def execute_agent_tool(self, name: str, arguments: Mapping[str, Any]) -> Any: ...

    def authorize_historic_tool_result(
        self,
        name: str,
        arguments: Mapping[str, Any],
        result: Any,
    ) -> bool: ...

    def match_legacy_capability(
        self,
        name: str,
        arguments: Mapping[str, Any],
        result: Any,
    ) -> LegacyCapabilityMatch | None: ...

    def normalize_capability_shadow_result(
        self,
        match: LegacyCapabilityMatch,
        result: Any,
    ) -> Any: ...

    def verify_shadow_data_context(
        self,
        match: LegacyCapabilityMatch,
        data_context: RuntimeDataContext,
    ) -> str | None: ...

    def prepare_grounding(self, user_message: str) -> Any: ...

    def ground(
        self,
        user_message: str,
        tool_outcomes: Sequence[Mapping[str, Any]],
        prepared: Any,
    ) -> GroundingResult: ...


def _validate_extension(
    value: Any,
    expected_key: str,
    expected_version: str,
) -> BoundAgentProvider:
    if value is None:
        raise AgentProviderExtensionError("bound Agent provider is missing")
    if _provider_key(getattr(value, "provider_key", "")) != expected_key:
        raise AgentProviderExtensionError("bound Agent provider key does not match")
    actual_version = str(getattr(value, "provider_version", "") or "").strip()
    if not actual_version:
        raise AgentProviderExtensionError("bound Agent provider version is missing")
    if actual_version != expected_version:
        raise AgentProviderExtensionError("bound Agent provider version does not match")
    for method in (
        "agent_tools",
        "execute_agent_tool",
        "authorize_historic_tool_result",
        "match_legacy_capability",
        "normalize_capability_shadow_result",
        "verify_shadow_data_context",
        "prepare_grounding",
        "ground",
    ):
        if not callable(getattr(value, method, None)):
            raise AgentProviderExtensionError(
                f"bound Agent provider must implement {method}()"
            )
    tools = tuple(value.agent_tools())
    if any(not isinstance(tool, AgentToolSpec) for tool in tools):
        raise AgentProviderExtensionError(
            "bound Agent provider returned an invalid tool contract"
        )
    if len({tool.name for tool in tools}) != len(tools):
        raise AgentProviderExtensionError(
            "bound Agent provider returned duplicate tool names"
        )
    return value


def _definition_provider_bindings(
    context: Any,
) -> tuple[set[tuple[str, str]], set[str]]:
    """Read exact Provider identities from the current governed definition."""

    definition = getattr(context, "runtime_definition", None)
    exact: set[tuple[str, str]] = set()
    referenced_keys: set[str] = set()
    for collection_name in ("functions", "actions", "workflows"):
        collection = getattr(definition, collection_name, ()) if definition else ()
        values = collection.values() if isinstance(collection, Mapping) else collection
        if not isinstance(values, Sequence) and not hasattr(values, "__iter__"):
            continue
        for resource in values:
            for config_name in ("runtime_config", "executor_config", "config"):
                config = (
                    resource.get(config_name)
                    if isinstance(resource, Mapping)
                    else getattr(resource, config_name, None)
                )
                if not isinstance(config, Mapping) or not config.get("provider_key"):
                    continue
                key = _provider_key(config.get("provider_key"))
                referenced_keys.add(key)
                version = str(config.get("provider_version") or "").strip()
                if version:
                    exact.add((key, version))
                break
    return exact, referenced_keys


def bind_agent_providers(
    context: Any,
    *,
    registry: CapabilityProviderRegistry | None = None,
) -> tuple[BoundAgentProvider, ...]:
    """Bind Definition-pinned extensions plus unambiguous legacy adapters."""

    from .capability_registry import (
        CapabilityRegistryError,
        default_provider_registry,
    )

    trusted_registry = registry or default_provider_registry
    result: list[BoundAgentProvider] = []
    tool_owner: dict[str, str] = {}
    exact, referenced_keys = _definition_provider_bindings(context)
    providers: list[tuple[str, str, Any]] = []
    for provider_key, provider_version in sorted(exact):
        try:
            provider = trusted_registry.resolve(provider_key, provider_version)
        except CapabilityRegistryError as exc:
            raise AgentProviderExtensionError(
                "Definition-pinned Provider could not be resolved for the Agent"
            ) from exc
        providers.append((provider_key, provider_version, provider))

    # Legacy contexts may predate explicit Provider bindings.  Auto-binding is
    # retained only when one installed version makes the choice deterministic.
    # A key referenced by the Definition but lacking a version is never guessed.
    for provider_key in trusted_registry.keys():
        if provider_key in referenced_keys:
            continue
        identities = tuple(
            identity
            for identity in trusted_registry.identities()
            if identity[0] == provider_key
        )
        if len(identities) != 1:
            continue
        provider_version = identities[0][1]
        try:
            provider = trusted_registry.resolve(provider_key, provider_version)
        except CapabilityRegistryError as exc:
            raise AgentProviderExtensionError(
                "Legacy Provider could not be resolved for the Agent"
            ) from exc
        providers.append((provider_key, provider_version, provider))

    for provider_key, provider_version, provider in providers:
        binder = getattr(provider, "bind_agent_runtime", None)
        if not callable(binder):
            continue
        try:
            bound = binder(context)
        except Exception as exc:  # noqa: BLE001 - Provider internals stay hidden.
            raise AgentProviderExtensionError(
                "Provider could not bind the Agent runtime"
            ) from exc
        if bound is None:
            continue
        extension = _validate_extension(bound, provider_key, provider_version)
        for tool in extension.agent_tools():
            owner = tool_owner.get(tool.name)
            if owner is not None:
                raise AgentProviderExtensionError(
                    "multiple Providers declared the same Agent tool"
                )
            tool_owner[tool.name] = provider_key
        result.append(extension)
    return tuple(
        sorted(result, key=lambda item: (item.provider_key, item.provider_version))
    )


__all__ = [
    "AgentProviderExtensionError",
    "AgentProviderToolError",
    "AgentToolSpec",
    "BoundAgentProvider",
    "GroundingResult",
    "LegacyCapabilityMatch",
    "bind_agent_providers",
]
