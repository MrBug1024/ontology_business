"""Trusted in-process capability-provider registry.

Providers can only be registered as concrete instances or explicit Python
factories supplied by application code.  There is intentionally no import-path,
database-row, entry-point, or reflection-based loading API in this module.
"""
from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from .capability_contracts import (
    Actor,
    CapabilityRef,
    Request,
    ResolvedDeployment,
    RuntimeDataContext,
)
from .capability_provider_keys import (
    builtin_provider_key,
    derive_provider_execution_key,
)


_PROVIDER_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_PROVIDER_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,79}$")


class CapabilityRegistryError(ValueError):
    """A provider registration or resolution request is invalid."""


@dataclass(frozen=True, slots=True)
class ProviderRecovery:
    """Trusted, side-effect-free reconciliation of an interrupted invocation."""

    state: Literal["succeeded", "failed", "indeterminate"]
    output: Any = None
    error_code: str = ""
    error_message: str = ""

    def __post_init__(self) -> None:
        if self.state not in {"succeeded", "failed", "indeterminate"}:
            raise CapabilityRegistryError("provider recovery state is invalid")


@runtime_checkable
class CapabilityProvider(Protocol):
    """Minimal boundary implemented by a trusted capability provider."""

    provider_key: str
    provider_version: str

    def contract(
        self,
        capability: CapabilityRef,
        deployment: ResolvedDeployment,
    ) -> Mapping[str, Any]:
        """Return the protocol-neutral public contract for a capability."""

    def invoke(
        self,
        request: Request,
        actor: Actor,
        deployment: ResolvedDeployment,
        data_context: RuntimeDataContext,
    ) -> Any:
        """Execute or confirm through the fixed runtime data context.

        The invoker never calls this method for preview mode.  Providers may
        optionally expose ``preview(request, actor, deployment, data_context)``;
        otherwise the invoker produces a generic platform preview.

        A side-effecting provider must use ``derive_provider_execution_key``
        for its durable execution lineage and propagate the final scoped key to
        any governed downstream idempotency carrier. It must never forward the
        caller's raw ``request.idempotency_key`` as that carrier. Database audit
        state and an external effect cannot be committed atomically by this
        boundary.

        Providers may expose a side-effect-free
        ``recover(request, actor, deployment, data_context)`` hook returning a
        ``ProviderRecovery``. The invoker uses it only after a fully validated
        confirmation is found durably ``running`` and never calls ``invoke``
        again during reconciliation.
        """


ProviderFactory = Callable[[], CapabilityProvider]
ProviderIdentity = tuple[str, str]


def normalize_provider_key(value: Any) -> str:
    key = str(value or "").strip().casefold()
    if not _PROVIDER_KEY_RE.fullmatch(key):
        raise CapabilityRegistryError(
            "provider key must be a lowercase portable token"
        )
    return key


def normalize_provider_version(value: Any) -> str:
    version = str(value or "").strip()
    if not _PROVIDER_VERSION_RE.fullmatch(version):
        raise CapabilityRegistryError(
            "provider version must be a portable explicit version"
        )
    return version


def _validate_provider(
    provider: Any,
    expected_key: str,
    expected_version: str,
) -> CapabilityProvider:
    if provider is None or isinstance(provider, (str, bytes, bytearray, Mapping)):
        raise CapabilityRegistryError("provider must be an explicit trusted object")
    actual_key = normalize_provider_key(getattr(provider, "provider_key", ""))
    if actual_key != expected_key:
        raise CapabilityRegistryError(
            f"provider key mismatch: expected {expected_key}, received {actual_key}"
        )
    actual_version = normalize_provider_version(
        getattr(provider, "provider_version", "")
    )
    if actual_version != expected_version:
        raise CapabilityRegistryError(
            "provider version mismatch: "
            f"expected {expected_version}, received {actual_version}"
        )
    if not callable(getattr(provider, "contract", None)):
        raise CapabilityRegistryError("provider must implement contract()")
    if not callable(getattr(provider, "invoke", None)):
        raise CapabilityRegistryError("provider must implement invoke()")
    return provider


def bind_provider(
    provider: CapabilityProvider,
    invocation_context: Any,
) -> CapabilityProvider:
    """Bind optional request-local state without mutating registry singletons."""

    key = normalize_provider_key(getattr(provider, "provider_key", ""))
    version = normalize_provider_version(
        getattr(provider, "provider_version", "")
    )
    binder = getattr(provider, "bind_invocation", None)
    if not callable(binder):
        return provider
    try:
        bound = binder(invocation_context)
    except Exception as exc:  # noqa: BLE001 - never expose provider internals.
        raise CapabilityRegistryError("provider invocation binding failed") from exc
    return _validate_provider(bound, key, version)


class CapabilityProviderRegistry:
    """Thread-safe registry for explicitly trusted providers and factories."""

    def __init__(self) -> None:
        self._instances: dict[ProviderIdentity, CapabilityProvider] = {}
        self._factories: dict[ProviderIdentity, ProviderFactory] = {}
        self._sealed = False
        self._lock = threading.RLock()

    @property
    def sealed(self) -> bool:
        with self._lock:
            return self._sealed

    def keys(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                sorted(
                    {
                        key
                        for key, _version in (
                            set(self._instances) | set(self._factories)
                        )
                    }
                )
            )

    def identities(self) -> tuple[ProviderIdentity, ...]:
        """Return every exact registered Provider identity."""

        with self._lock:
            return tuple(sorted(set(self._instances) | set(self._factories)))

    def __contains__(self, provider_key: object) -> bool:
        if isinstance(provider_key, tuple) and len(provider_key) == 2:
            try:
                identity = (
                    normalize_provider_key(provider_key[0]),
                    normalize_provider_version(provider_key[1]),
                )
            except CapabilityRegistryError:
                return False
            with self._lock:
                return identity in self._instances or identity in self._factories
        try:
            key = normalize_provider_key(provider_key)
        except CapabilityRegistryError:
            return False
        with self._lock:
            return any(
                identity_key == key
                for identity_key, _version in (
                    set(self._instances) | set(self._factories)
                )
            )

    def _ensure_mutable(self) -> None:
        if self._sealed:
            raise CapabilityRegistryError("provider registry is sealed")

    def _ensure_available(self, identity: ProviderIdentity) -> None:
        if identity in self._instances or identity in self._factories:
            key, version = identity
            raise CapabilityRegistryError(
                f"provider is already registered: {key}@{version}"
            )

    def register_instance(
        self,
        provider: CapabilityProvider,
        *,
        provider_key: str | None = None,
        provider_version: str | None = None,
    ) -> CapabilityProvider:
        """Register one already-created trusted provider."""

        raw_key = provider_key if provider_key is not None else getattr(
            provider, "provider_key", ""
        )
        key = normalize_provider_key(raw_key)
        raw_version = (
            provider_version
            if provider_version is not None
            else getattr(provider, "provider_version", "")
        )
        version = normalize_provider_version(raw_version)
        identity = (key, version)
        validated = _validate_provider(provider, key, version)
        with self._lock:
            self._ensure_mutable()
            self._ensure_available(identity)
            self._instances[identity] = validated
        return validated

    def register_factory(
        self,
        provider_key: str,
        factory: ProviderFactory,
        *,
        provider_version: str | None = None,
    ) -> None:
        """Register a zero-argument factory explicitly supplied by code.

        String import paths are rejected.  The factory is evaluated once, on
        first resolution, and the validated provider is cached as a singleton.
        Its version must be supplied explicitly or declared as immutable
        ``provider_version`` metadata on the factory; the registry never calls
        a factory merely to guess its identity during registration.
        """

        key = normalize_provider_key(provider_key)
        if isinstance(factory, (str, bytes, bytearray)) or not callable(factory):
            raise CapabilityRegistryError(
                "provider factory must be an explicit callable, not an import path"
            )
        raw_version = (
            provider_version
            if provider_version is not None
            else getattr(factory, "provider_version", "")
        )
        if not str(raw_version or "").strip():
            raise CapabilityRegistryError(
                "provider factory version must be declared explicitly"
            )
        version = normalize_provider_version(raw_version)
        identity = (key, version)
        with self._lock:
            self._ensure_mutable()
            self._ensure_available(identity)
            self._factories[identity] = factory

    def resolve(
        self,
        provider_key: str,
        provider_version: str | None = None,
    ) -> CapabilityProvider:
        key = normalize_provider_key(provider_key)
        with self._lock:
            if provider_version is None:
                candidates = sorted(
                    identity
                    for identity in (set(self._instances) | set(self._factories))
                    if identity[0] == key
                )
                if not candidates:
                    raise CapabilityRegistryError(
                        f"provider is not registered: {key}"
                    )
                if len(candidates) != 1:
                    raise CapabilityRegistryError(
                        f"provider version is ambiguous: {key}"
                    )
                identity = candidates[0]
            else:
                identity = (key, normalize_provider_version(provider_version))
            existing = self._instances.get(identity)
            if existing is not None:
                return existing
            factory = self._factories.get(identity)
            if factory is None:
                raise CapabilityRegistryError(
                    f"provider is not registered: {identity[0]}@{identity[1]}"
                )
            try:
                provider = factory()
            except Exception as exc:  # noqa: BLE001 - hide factory internals.
                raise CapabilityRegistryError(
                    f"provider factory failed: {identity[0]}@{identity[1]}"
                ) from exc
            validated = _validate_provider(provider, *identity)
            self._instances[identity] = validated
            # The resolved instance is now authoritative.  Removing the
            # factory also prevents accidental re-instantiation after sealing.
            self._factories.pop(identity, None)
            return validated

    def seal(self) -> None:
        """Prevent all subsequent registrations for process-lifetime trust."""

        with self._lock:
            self._sealed = True


default_provider_registry = CapabilityProviderRegistry()


def register_builtin_providers(
    registry: CapabilityProviderRegistry = default_provider_registry,
) -> CapabilityProviderRegistry:
    """Register fixed platform adapters and installed trusted Provider packages."""

    from ..providers import trusted_capability_providers
    from .builtin_capability_providers import builtin_capability_providers

    for provider in (
        *builtin_capability_providers(),
        *trusted_capability_providers(),
    ):
        identity = (provider.provider_key, provider.provider_version)
        if identity not in registry:
            registry.register_instance(provider)
    return registry


register_builtin_providers()
default_provider_registry.seal()


__all__ = [
    "CapabilityProvider",
    "CapabilityProviderRegistry",
    "CapabilityRegistryError",
    "ProviderRecovery",
    "ProviderFactory",
    "ProviderIdentity",
    "bind_provider",
    "builtin_provider_key",
    "default_provider_registry",
    "derive_provider_execution_key",
    "normalize_provider_key",
    "normalize_provider_version",
    "register_builtin_providers",
]
