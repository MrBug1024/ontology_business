"""Server-owned bindings for built-in ontology capability providers."""
from __future__ import annotations

from types import MappingProxyType

from .capability_contracts import (
    Actor,
    Request,
    ResolvedDeployment,
    canonical_hash,
)


BUILTIN_PROVIDER_KEYS = MappingProxyType(
    {
        "action": "builtin.ontology-action",
        "function": "builtin.ontology-function",
        "rule": "builtin.ontology-rule",
        "workflow": "builtin.ontology-workflow",
    }
)


def builtin_provider_key(capability_kind: str) -> str | None:
    """Return the fixed provider binding for a built-in capability kind."""

    return BUILTIN_PROVIDER_KEYS.get(str(capability_kind or "").strip().lower())


def derive_provider_execution_key(
    request: Request,
    actor: Actor,
    deployment: ResolvedDeployment,
) -> str:
    """Scope a caller idempotency key to one deployment and principal."""

    if not isinstance(request, Request):
        raise ValueError("provider execution key requires an immutable request")
    if not isinstance(actor, Actor):
        raise ValueError("provider execution key requires an authenticated actor")
    if not isinstance(deployment, ResolvedDeployment):
        raise ValueError("provider execution key requires a resolved deployment")
    caller_key = str(request.idempotency_key or "").strip()
    if not caller_key:
        raise ValueError("side-effecting capability requires an idempotency key")
    scope = canonical_hash(
        {
            "capability": {
                "kind": request.capability.kind,
                "resource_id": request.capability.resource_id,
            },
            "definition_hash": deployment.definition_hash,
            "deployment_fingerprint": deployment.fingerprint,
            "principal": {
                "id": actor.principal_id,
                "type": actor.actor_type,
            },
            "scenario_id": deployment.scenario_id,
            "tenant_id": deployment.tenant_id,
        },
        domain="capability-provider-execution-scope-v1",
    )
    return f"cap:{scope[:24]}:{caller_key}"


__all__ = [
    "BUILTIN_PROVIDER_KEYS",
    "builtin_provider_key",
    "derive_provider_execution_key",
]
