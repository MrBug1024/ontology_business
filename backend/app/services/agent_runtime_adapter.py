"""Validation-Agent adapter for the protocol-neutral capability kernel.

Historical Agent mode values remain readable for migration audit, but they are
not executable. This module resolves a public capability catalog, keeps one
immutable ``RuntimeDataContext`` for the turn, and translates generic model
tool calls into ``capability_application_service`` requests.

No provider, connector, data-source, mapping, SQL, or industry-specific branch
belongs here.  Those concerns stay behind the capability application service
and ``CapabilityInvoker``.
"""
from __future__ import annotations

import copy
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Iterator
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    Agent,
    BusinessScenario,
    CapabilityInvocation,
    ConnectorBinding,
    DataSource,
    LLMConfig,
)
from . import (
    agent_capability_service,
    capability_application_service,
    llm_service,
    permission_service,
    runtime_connector_service,
    tenant_service,
)
from .capability_contracts import (
    Actor,
    CapabilityContractError,
    CapabilityRef,
    DataBindingOverride,
    Request,
    RuntimeDataContext,
    canonical_hash,
    canonical_json,
)
from .capability_invoker import CapabilityInvocationError
from .agent_prompt_policy import AUTHORITATIVE_DECISION_PROMPT


_CAPABILITY_CATEGORY_BY_KIND = {
    "function": "functions",
    "action": "actions",
    "rule": "rules",
    "workflow": "workflows",
}
_HISTORICAL_MODES = {
    "legacy",
    "shadow",
    "prefer_capability",
}
_CAPABILITY_TOOL_LIST = "list_available_capabilities"
_CAPABILITY_TOOL_INVOKE = "invoke_capability"


class AgentRuntimeAdapterError(RuntimeError):
    """Safe failure while choosing or using one Agent runtime path."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code or "agent_runtime_adapter_error").strip().lower()
        self.message = str(message or "Agent capability runtime is unavailable").strip()


@dataclass(frozen=True, slots=True)
class AgentAttachmentInput:
    """An immutable user upload; the model maps it to a capability data port."""

    asset_version_id: str | None = None
    dataset_version_id: str | None = None
    filename: str = ""
    expected_signature: str | None = None

    def __post_init__(self) -> None:
        asset_version_id = str(self.asset_version_id or "").strip() or None
        dataset_version_id = str(self.dataset_version_id or "").strip() or None
        filename = str(self.filename or "").strip()[:500]
        signature = str(self.expected_signature or "").strip().lower() or None
        if (asset_version_id is None) == (dataset_version_id is None) or any(
            len(value) > 32 for value in (asset_version_id, dataset_version_id) if value
        ):
            raise AgentRuntimeAdapterError(
                "invalid_attachment_reference",
                "Agent attachment version reference is invalid",
            )
        if signature is not None and (
            len(signature) != 64 or any(char not in "0123456789abcdef" for char in signature)
        ):
            raise AgentRuntimeAdapterError(
                "invalid_attachment_signature",
                "Agent attachment signature is invalid",
            )
        object.__setattr__(self, "asset_version_id", asset_version_id)
        object.__setattr__(self, "dataset_version_id", dataset_version_id)
        object.__setattr__(self, "filename", filename)
        object.__setattr__(self, "expected_signature", signature)

    @property
    def binding_kind(self) -> str:
        return "dataset_version" if self.dataset_version_id else "asset_version"

    @property
    def reference_id(self) -> str:
        return str(self.dataset_version_id or self.asset_version_id or "")


@dataclass(frozen=True, slots=True)
class AgentTurnInput:
    """Immutable per-turn inputs supplied by the validation client."""

    structured_inputs: Mapping[str, Any] = field(default_factory=dict)
    binding_overrides: tuple[DataBindingOverride, ...] = ()
    target_kind: str | None = None
    target_key: str | None = None
    idempotency_key: str | None = None
    attachments: tuple[AgentAttachmentInput, ...] = ()

    def __post_init__(self) -> None:
        try:
            plain_inputs = json.loads(canonical_json(self.structured_inputs))
        except CapabilityContractError as exc:
            raise AgentRuntimeAdapterError(
                "invalid_structured_inputs",
                "Agent structured inputs must be a JSON-compatible object",
            ) from exc
        if not isinstance(plain_inputs, dict):
            raise AgentRuntimeAdapterError(
                "invalid_structured_inputs",
                "Agent structured inputs must be an object",
            )
        overrides = tuple(self.binding_overrides)
        if any(not isinstance(item, DataBindingOverride) for item in overrides):
            raise AgentRuntimeAdapterError(
                "invalid_managed_inputs",
                "Agent managed inputs must use governed binding overrides",
            )
        keys = [item.port_key for item in overrides]
        if len(keys) != len(set(keys)):
            raise AgentRuntimeAdapterError(
                "duplicate_managed_input",
                "A managed input port can be supplied only once per turn",
            )
        kind = str(self.target_kind or "").strip().lower() or None
        key = str(self.target_key or "").strip() or None
        if (kind is None) != (key is None):
            raise AgentRuntimeAdapterError(
                "invalid_capability_target",
                "Capability kind and key must be supplied together",
            )
        if kind is not None and kind not in _CAPABILITY_CATEGORY_BY_KIND:
            raise AgentRuntimeAdapterError(
                "unsupported_capability_target",
                "The requested capability kind is not available to validation Agents",
            )
        idempotency_key = str(self.idempotency_key or "").strip() or None
        if idempotency_key is not None and len(idempotency_key) > 180:
            raise AgentRuntimeAdapterError(
                "invalid_idempotency_key",
                "Agent idempotency key is too long",
            )
        attachments = tuple(self.attachments)
        if any(not isinstance(item, AgentAttachmentInput) for item in attachments):
            raise AgentRuntimeAdapterError(
                "invalid_attachments",
                "Agent attachments must use immutable managed-upload references",
            )
        if len(attachments) > 20:
            raise AgentRuntimeAdapterError(
                "too_many_attachments",
                "An Agent turn may contain at most 20 attachments",
            )
        version_ids = [(item.binding_kind, item.reference_id) for item in attachments]
        if len(version_ids) != len(set(version_ids)):
            raise AgentRuntimeAdapterError(
                "duplicate_attachment",
                "The same attachment cannot be supplied twice in one turn",
            )
        object.__setattr__(self, "structured_inputs", plain_inputs)
        object.__setattr__(
            self,
            "binding_overrides",
            tuple(sorted(overrides, key=lambda item: item.port_key)),
        )
        object.__setattr__(self, "target_kind", kind)
        object.__setattr__(self, "target_key", key)
        object.__setattr__(self, "idempotency_key", idempotency_key)
        object.__setattr__(self, "attachments", attachments)


def _outline(value: Any, *, depth: int = 0) -> dict[str, Any]:
    """Return structure and scalar types without retaining caller values."""

    if depth >= 8:
        return {"type": "truncated"}
    if isinstance(value, Mapping):
        return {
            "type": "object",
            "fields": {
                str(key)[:160]: _outline(item, depth=depth + 1)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            },
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        samples = [_outline(item, depth=depth + 1) for item in list(value)[:10]]
        return {"type": "array", "length": len(value), "samples": samples}
    if value is None:
        kind = "null"
    elif isinstance(value, bool):
        kind = "boolean"
    elif isinstance(value, int):
        kind = "integer"
    elif isinstance(value, float):
        kind = "number"
    else:
        kind = "string"
    return {"type": kind}


def _safe_turn_input(turn_input: AgentTurnInput) -> dict[str, Any]:
    managed: list[dict[str, Any]] = []
    for item in turn_input.binding_overrides:
        selector_hash = canonical_hash(
            {
                "binding_kind": item.binding_kind,
                "selector": item.selector,
                "selector_value": item.selector_value,
            },
            domain="agent-managed-selector-v1",
        )
        managed.append(
            {
                "port_key": item.port_key,
                "binding_kind": item.binding_kind,
                "selector": item.selector,
                "selector_hash": selector_hash,
                "expected_signature": item.signature,
            }
        )
    managed_override_hash = canonical_hash(
        [
            {
                "binding_kind": item.binding_kind,
                "binding_key": item.binding_key,
                "port_key": item.port_key,
                "reference_id": item.reference_id,
                "signature": item.signature,
                "selector": item.selector,
                "version_id": item.version_id,
            }
            for item in turn_input.binding_overrides
        ],
        domain="capability-managed-override-intent-v1",
    )
    return {
        "contract": "agent-turn-input/v1",
        "structured_inputs": {
            "hash": canonical_hash(
                turn_input.structured_inputs,
                domain="agent-structured-input-v1",
            ),
            "invocation_hash": canonical_hash(
                turn_input.structured_inputs,
                domain="capability-structured-input-v1",
            ),
            "outline": _outline(turn_input.structured_inputs),
        },
        "managed_inputs": managed,
        "managed_override_hash": managed_override_hash,
        "target": (
            {"kind": turn_input.target_kind, "key": turn_input.target_key}
            if turn_input.target_kind and turn_input.target_key
            else None
        ),
        "attachments": [
            {
                "asset_version_id": item.asset_version_id,
                "dataset_version_id": item.dataset_version_id,
                "filename": item.filename,
                "expected_signature": item.expected_signature,
            }
            for item in turn_input.attachments
        ],
    }


def _tool(name: str, description: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": json.loads(canonical_json(parameters)),
        },
    }


def _parse_result(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _safe_error(code: str, message: str, *, retryable: bool = False) -> str:
    return json.dumps(
        {
            "ok": False,
            "error": {
                "code": str(code or "CAPABILITY_INVOCATION_FAILED")[:80],
                "message": str(message or "Capability invocation failed")[:500],
                "retryable": bool(retryable),
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _public_capability(document: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only the application service's machine-readable public contract."""

    public_ports: list[dict[str, Any]] = []
    for raw_port in document.get("data_ports") or []:
        if not isinstance(raw_port, Mapping):
            continue
        public_ports.append(
            {
                "port_key": str(
                    raw_port.get("port_key") or raw_port.get("key") or ""
                ),
                "name": str(raw_port.get("name") or ""),
                "description": str(raw_port.get("description") or ""),
                "direction": str(raw_port.get("direction") or "input"),
                "role": str(raw_port.get("role") or "invocation_input"),
                "media_kind": str(raw_port.get("media_kind") or "structured"),
                "schema_document": copy.deepcopy(
                    raw_port.get("schema_document") or {}
                ),
                "schema_signature": str(
                    raw_port.get("schema_signature")
                    or raw_port.get("schema_hash")
                    or ""
                ),
                "required": bool(raw_port.get("required", True)),
                "cardinality": str(raw_port.get("cardinality") or "one"),
                "binding_policy": str(
                    raw_port.get("binding_policy") or "per_invocation"
                ),
                "binding_kinds": [
                    str(value)
                    for value in (raw_port.get("binding_kinds") or [])
                    if str(value).strip()
                ],
                "allow_override": bool(raw_port.get("allow_override", False)),
            }
        )
    raw_readiness = document.get("readiness") or {}
    public_issues: list[dict[str, Any]] = []
    if isinstance(raw_readiness, Mapping):
        for raw_issue in raw_readiness.get("issues") or []:
            if not isinstance(raw_issue, Mapping):
                continue
            public_issues.append(
                {
                    key: copy.deepcopy(raw_issue[key])
                    for key in (
                        "axis",
                        "blocking",
                        "code",
                        "port_key",
                        "kind",
                        "count",
                    )
                    if key in raw_issue
                }
            )
    return {
        "kind": str(document.get("kind") or ""),
        "key": str(document.get("key") or ""),
        "name": str(document.get("name") or ""),
        "description": str(document.get("description") or ""),
        "input_schema": copy.deepcopy(document.get("input_schema") or {}),
        "output_schema": copy.deepcopy(document.get("output_schema") or {}),
        "side_effect": bool(document.get("side_effect", False)),
        "requires_confirmation": bool(document.get("requires_confirmation", False)),
        "idempotency_required": bool(document.get("idempotency_required", False)),
        "data_ports": public_ports,
        "readiness": {
            "ready": bool(
                raw_readiness.get("ready", False)
                if isinstance(raw_readiness, Mapping)
                else False
            ),
            "issues": public_issues,
        },
        "definition_hash": str(document.get("definition_hash") or ""),
        "deployment_fingerprint": str(document.get("deployment_fingerprint") or ""),
    }


class CapabilityAgentRuntime:
    """One capability-only validation turn over a frozen application context."""

    runtime_path = "capability"

    def __init__(
        self,
        db: Session,
        agent: Agent,
        llm: LLMConfig,
        *,
        turn_input: AgentTurnInput | None = None,
        environment: str | None = None,
    ) -> None:
        self.db = db
        self.agent = agent
        self.llm = llm
        self.turn_input = turn_input or AgentTurnInput()
        self.tenant_id = tenant_service.current_tenant_id(db)
        self.scenario = (
            tenant_service.get_visible(db, BusinessScenario, agent.scenario_id)
            if agent.scenario_id
            else None
        )
        if self.scenario is None:
            raise AgentRuntimeAdapterError(
                "scenario_unavailable",
                "Capability Agent requires a visible business scenario",
            )
        permission_service.require_scenario_permission(
            db,
            self.scenario,
            "read",
            message="没有使用该 Agent 业务场景的权限",
        )
        self.environment = runtime_connector_service.runtime_environment(environment)
        if self.environment not in {"dev", "staging", "prod"}:
            raise AgentRuntimeAdapterError(
                "invalid_runtime_environment",
                "Agent runtime environment must be dev, staging, or prod",
            )
        try:
            raw_catalog = capability_application_service.list_capabilities(
                db,
                self.scenario,
                environment=self.environment,
            )
            deployment, deployment_inputs = capability_application_service.resolve_deployment(
                db,
                self.scenario,
                environment=self.environment,
            )
        except capability_application_service.CapabilityApplicationError as exc:
            raise AgentRuntimeAdapterError(exc.code, exc.message) from exc
        if any(
            str(item.get("definition_hash") or "") != deployment.definition_hash
            for item in raw_catalog
        ):
            raise AgentRuntimeAdapterError(
                "capability_context_changed",
                "The active capability deployment changed while the Agent turn was resolving",
            )
        self.deployment = deployment
        self.deployment_inputs = deployment_inputs
        self.runtime_definition = deployment.definition
        self.runtime_data_context: RuntimeDataContext = deployment.data_context
        runtime_source_ids = [
            str(item) for item in (agent.runtime_data_source_ids or []) if str(item)
        ]
        self.runtime_connections = list(
            db.scalars(
                select(DataSource).where(
                    DataSource.id.in_(runtime_source_ids),
                    DataSource.tenant_id == self.tenant_id,
                    DataSource.resource_scope == "agent_runtime",
                    DataSource.owner_agent_id == agent.id,
                )
            ).all()
        ) if runtime_source_ids else []
        binding_rows = list(
            db.scalars(
                select(ConnectorBinding).where(
                    ConnectorBinding.tenant_id == self.tenant_id,
                    ConnectorBinding.scenario_id == self.scenario.id,
                    ConnectorBinding.environment == self.environment,
                    ConnectorBinding.connector_kind == "data_source",
                    ConnectorBinding.connector_id.in_([item.id for item in self.runtime_connections]),
                )
            ).all()
        ) if self.runtime_connections else []
        bindings_by_source = {item.connector_id: item for item in binding_rows}
        self.runtime_connection_options = [
            {
                "binding_key": bindings_by_source[source.id].binding_key,
                "name": source.name,
                "adapter": source.type,
                "ready": bindings_by_source[source.id].health_status == "healthy",
            }
            for source in self.runtime_connections
            if source.id in bindings_by_source
        ]
        self.capability_scope = agent_capability_service.normalize_scope(
            (
                agent_capability_service.legacy_all_scope()
                if agent.capability_scope is None
                else agent.capability_scope
            ),
            legacy_default=False,
            allow_all=True,
        )
        self.capabilities = self._scoped_capabilities(raw_catalog)
        self._capability_by_ref = {
            (str(item["kind"]), str(item["key"])): item for item in self.capabilities
        }
        self.context_issues = self._context_issues(raw_catalog)
        self.complete = not self.context_issues
        self.citations: list[dict[str, Any]] = []
        self._evidence_refs: list[dict[str, Any]] = []
        self._invocations: list[dict[str, Any]] = []
        self.runtime_decision: dict[str, Any] = {}

    def _scoped_capabilities(
        self,
        raw_catalog: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for raw in raw_catalog:
            kind = str(raw.get("kind") or "")
            category = _CAPABILITY_CATEGORY_BY_KIND.get(kind)
            if category is None:
                continue
            entry = self.capability_scope[category]
            key = str(raw.get("key") or "")
            if entry["mode"] != "all" and key not in set(entry["selected_ids"]):
                continue
            if (
                self.turn_input.target_kind
                and (kind, key)
                != (self.turn_input.target_kind, self.turn_input.target_key)
            ):
                continue
            result.append(_public_capability(raw))
        return sorted(result, key=lambda item: (item["kind"], item["name"], item["key"]))

    def _context_issues(
        self,
        raw_catalog: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        available = {
            str(item.get("kind") or ""): {
                str(candidate.get("key") or "")
                for candidate in raw_catalog
                if str(candidate.get("kind") or "") == str(item.get("kind") or "")
            }
            for item in raw_catalog
        }
        issues: list[dict[str, Any]] = []
        for kind, category in _CAPABILITY_CATEGORY_BY_KIND.items():
            entry = self.capability_scope[category]
            if entry["mode"] == "all":
                continue
            missing = sorted(set(entry["selected_ids"]) - available.get(kind, set()))
            if missing:
                issues.append(
                    {
                        "code": "selected_capability_unavailable",
                        "kind": kind,
                        "count": len(missing),
                    }
                )
        for item in self.capabilities:
            readiness = item.get("readiness") or {}
            if not bool(readiness.get("ready", False)):
                issues.append(
                    {
                        "code": "selected_capability_not_ready",
                        "kind": item["kind"],
                        "key": item["key"],
                        "blocking_codes": sorted(
                            {
                                str(issue.get("code") or "capability_not_ready")
                                for issue in readiness.get("issues") or []
                                if bool(issue.get("blocking", True))
                            }
                        ),
                    }
                )
        if self.turn_input.target_kind and (
            self.turn_input.target_kind,
            self.turn_input.target_key,
        ) not in self._capability_by_ref:
            issues.append(
                {
                    "code": "requested_capability_unavailable",
                    "kind": self.turn_input.target_kind,
                }
            )
        if self.turn_input.attachments and not any(
            self._capability_accepts_attachments(item)
            for item in self.capabilities
            if bool((item.get("readiness") or {}).get("ready", False))
        ):
            issues.append(
                {
                    "code": "attachments_not_supported",
                    "binding_kinds": sorted(
                        {item.binding_kind for item in self.turn_input.attachments}
                    ),
                    "count": len(self.turn_input.attachments),
                }
            )
        return issues

    def _capability_accepts_attachments(
        self,
        capability: Mapping[str, Any],
    ) -> bool:
        eligible = [
            item
            for item in (capability.get("data_ports") or [])
            if isinstance(item, Mapping)
            and str(item.get("direction") or "input").strip().lower() != "output"
            and bool(item.get("allow_override", False))
            and str(item.get("key") or item.get("port_key") or "").strip()
        ]
        attachments = self.turn_input.attachments
        if len(eligible) < len(attachments):
            return False

        def assign(index: int, used: frozenset[str]) -> bool:
            if index >= len(attachments):
                return True
            attachment = attachments[index]
            for port in eligible:
                key = str(port.get("key") or port.get("port_key") or "").strip()
                if key in used:
                    continue
                kinds = {
                    str(kind).strip().lower()
                    for kind in (port.get("binding_kinds") or [])
                }
                if attachment.binding_kind in kinds and assign(index + 1, used | {key}):
                    return True
            return False

        return assign(0, frozenset())

    def set_runtime_decision(self, document: Mapping[str, Any]) -> None:
        self.runtime_decision = copy.deepcopy(dict(document))

    def input_snapshot(self) -> dict[str, Any]:
        snapshot = _safe_turn_input(self.turn_input)
        snapshot["runtime"] = copy.deepcopy(self.runtime_decision)
        snapshot["invocations"] = copy.deepcopy(self._invocations)
        return snapshot

    def evidence_snapshot(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._evidence_refs)

    def citation_snapshot(self) -> list[dict[str, Any]]:
        return []

    def _actor(self) -> Actor:
        principal = permission_service.require_principal(self.db)
        return Actor(
            actor_type="agent",
            # Built-in providers bind the principal to the configured Agent;
            # the authenticated human remains a separate audit fact.
            principal_id=self.agent.id,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            agent_id=self.agent.id,
            roles=principal.role_keys,
            scopes=("capability:read", "capability:invoke"),
        )

    def public_catalog(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.capabilities)

    def build_tools(self) -> list[dict[str, Any]]:
        if not self.capabilities:
            return []
        kinds = sorted({str(item["kind"]) for item in self.capabilities})
        keys = sorted({str(item["key"]) for item in self.capabilities})
        invoke_properties: dict[str, Any] = {
            "kind": {"type": "string", "enum": kinds},
            "key": {"type": "string", "enum": keys},
            "inputs": {"type": "object"},
        }
        if self.turn_input.attachments:
            invoke_properties["attachment_bindings"] = {
                "type": "array",
                "description": (
                    "Map each user attachment index to one compatible managed-data "
                    "input port from the selected capability. Omit only when the order "
                    "of attachments and eligible ports is unambiguous."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "attachment_index": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": len(self.turn_input.attachments) - 1,
                        },
                        "port_key": {"type": "string"},
                    },
                    "required": ["attachment_index", "port_key"],
                    "additionalProperties": False,
                },
            }
        if self.runtime_connection_options:
            invoke_properties["connection_bindings"] = {
                "type": "array",
                "description": (
                    "Map the Agent's configured database index to a compatible "
                    "connector input port. Omit when there is exactly one clear mapping."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "connection_index": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": len(self.runtime_connection_options) - 1,
                        },
                        "port_key": {"type": "string"},
                    },
                    "required": ["connection_index", "port_key"],
                    "additionalProperties": False,
                },
            }
        return [
            _tool(
                _CAPABILITY_TOOL_LIST,
                "List the current Agent's governed capability contracts and readiness.",
                {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            ),
            _tool(
                _CAPABILITY_TOOL_INVOKE,
                "Invoke one listed capability through the governed capability kernel. "
                "Side-effecting capabilities are previewed and require a separate user confirmation.",
                {
                    "type": "object",
                    "properties": invoke_properties,
                    "required": ["kind", "key"],
                    "additionalProperties": False,
                },
            ),
        ]

    def _attachment_overrides(
        self,
        capability: Mapping[str, Any],
        raw_bindings: Any,
    ) -> tuple[DataBindingOverride, ...]:
        """Bind chat uploads to the selected capability without a user-facing form."""

        attachments = self.turn_input.attachments
        if not attachments:
            return ()
        occupied = {item.port_key for item in self.turn_input.binding_overrides}
        eligible = [
            item
            for item in (capability.get("data_ports") or [])
            if isinstance(item, Mapping)
            and str(item.get("direction") or "input").strip().lower() != "output"
            and bool(item.get("allow_override", False))
            and str(item.get("key") or item.get("port_key") or "").strip()
            not in occupied
        ]
        eligible_by_key = {
            str(item.get("key") or item.get("port_key") or "").strip(): item
            for item in eligible
        }
        def compatible(index: int, key: str) -> bool:
            port = eligible_by_key.get(key)
            if port is None or index < 0 or index >= len(attachments):
                return False
            return attachments[index].binding_kind in {
                str(kind).strip().lower()
                for kind in (port.get("binding_kinds") or [])
            }

        if not eligible_by_key or any(
            not any(
                attachment.binding_kind in {
                    str(kind).strip().lower()
                    for kind in (port.get("binding_kinds") or [])
                }
                for port in eligible
            )
            for attachment in attachments
        ):
            raise AgentRuntimeAdapterError(
                "capability_does_not_accept_attachments",
                "The selected capability has no compatible dataset or file input port for the user's attachments",
            )

        pairs: list[tuple[int, str]] = []
        if raw_bindings is None:
            used: set[str] = set()
            for index, _attachment in enumerate(attachments):
                key = next(
                    (
                        candidate
                        for candidate in eligible_by_key
                        if candidate not in used and compatible(index, candidate)
                    ),
                    "",
                )
                if not key:
                    raise AgentRuntimeAdapterError(
                        "attachment_port_mapping_required",
                        "There are more attachments than compatible input ports",
                    )
                used.add(key)
                pairs.append((index, key))
        else:
            if not isinstance(raw_bindings, Sequence) or isinstance(
                raw_bindings, (str, bytes, bytearray)
            ):
                raise AgentRuntimeAdapterError(
                    "invalid_attachment_bindings",
                    "Attachment bindings must be an array",
                )
            for item in raw_bindings:
                if not isinstance(item, Mapping):
                    raise AgentRuntimeAdapterError(
                        "invalid_attachment_bindings",
                        "Each attachment binding must be an object",
                    )
                try:
                    index = int(item.get("attachment_index"))
                except (TypeError, ValueError) as exc:
                    raise AgentRuntimeAdapterError(
                        "invalid_attachment_index",
                        "Attachment index is invalid",
                    ) from exc
                key = str(item.get("port_key") or "").strip()
                pairs.append((index, key))

        indices = [index for index, _key in pairs]
        port_keys = [key for _index, key in pairs]
        if sorted(indices) != list(range(len(attachments))):
            raise AgentRuntimeAdapterError(
                "incomplete_attachment_bindings",
                "Every uploaded attachment must be mapped exactly once",
            )
        if len(port_keys) != len(set(port_keys)) or any(
            not compatible(index, key) for index, key in pairs
        ):
            raise AgentRuntimeAdapterError(
                "invalid_attachment_port",
                "Attachment mapping contains a duplicate or incompatible input port",
            )
        return tuple(
            DataBindingOverride(
                port_key=port_key,
                binding_kind=attachments[index].binding_kind,
                reference_id=attachments[index].reference_id,
                signature=attachments[index].expected_signature,
            )
            for index, port_key in pairs
        )

    def _connection_overrides(
        self,
        capability: Mapping[str, Any],
        raw_bindings: Any,
        occupied: set[str],
    ) -> tuple[DataBindingOverride, ...]:
        options = self.runtime_connection_options
        if not options:
            return ()
        eligible = [
            item
            for item in (capability.get("data_ports") or [])
            if isinstance(item, Mapping)
            and str(item.get("direction") or "input").strip().lower() != "output"
            and bool(item.get("allow_override", False))
            and "connector_binding" in {
                str(kind).strip().lower()
                for kind in (item.get("binding_kinds") or [])
            }
            and str(item.get("key") or item.get("port_key") or "").strip()
            not in occupied
        ]
        eligible_by_key = {
            str(item.get("key") or item.get("port_key") or "").strip(): item
            for item in eligible
        }
        if not eligible_by_key:
            return ()

        pairs: list[tuple[int, str]] = []
        if raw_bindings is None:
            if len(options) == 1 and len(eligible) == 1:
                pairs = [(0, next(iter(eligible_by_key)))]
            elif len(options) <= len(eligible):
                pairs = [
                    (index, str(port.get("key") or port.get("port_key") or "").strip())
                    for index, port in enumerate(eligible[: len(options)])
                ]
            else:
                raise AgentRuntimeAdapterError(
                    "database_port_mapping_required",
                    "The Agent has multiple business databases; select which one serves each connector input port",
                )
        else:
            if not isinstance(raw_bindings, Sequence) or isinstance(
                raw_bindings, (str, bytes, bytearray)
            ):
                raise AgentRuntimeAdapterError(
                    "invalid_database_bindings",
                    "Database bindings must be an array",
                )
            for item in raw_bindings:
                if not isinstance(item, Mapping):
                    raise AgentRuntimeAdapterError(
                        "invalid_database_bindings",
                        "Each database binding must be an object",
                    )
                try:
                    index = int(item.get("connection_index"))
                except (TypeError, ValueError) as exc:
                    raise AgentRuntimeAdapterError(
                        "invalid_database_index",
                        "Database index is invalid",
                    ) from exc
                pairs.append((index, str(item.get("port_key") or "").strip()))

        indices = [index for index, _key in pairs]
        keys = [key for _index, key in pairs]
        if (
            not pairs
            or any(index < 0 or index >= len(options) for index in indices)
            or len(indices) != len(set(indices))
            or len(keys) != len(set(keys))
            or any(key not in eligible_by_key for key in keys)
        ):
            raise AgentRuntimeAdapterError(
                "invalid_database_port",
                "Database mapping contains a duplicate or incompatible input port",
            )
        not_ready = [options[index]["name"] for index in indices if not options[index]["ready"]]
        if not_ready:
            raise AgentRuntimeAdapterError(
                "database_connection_not_ready",
                "Agent business database connection is not ready: " + ", ".join(not_ready),
            )
        return tuple(
            DataBindingOverride(
                port_key=port_key,
                binding_kind="connector_binding",
                binding_key=str(options[index]["binding_key"]),
            )
            for index, port_key in pairs
        )

    def _request_inputs(self, tool_inputs: Any) -> dict[str, Any]:
        if tool_inputs is None:
            tool_inputs = {}
        if not isinstance(tool_inputs, Mapping):
            raise AgentRuntimeAdapterError(
                "invalid_capability_inputs",
                "Capability inputs must be an object",
            )
        # The client-supplied structured document is authoritative.  The model
        # may fill omitted fields but cannot silently replace explicit values.
        return {
            **json.loads(canonical_json(tool_inputs)),
            **json.loads(canonical_json(self.turn_input.structured_inputs)),
        }

    def _tool_request_identity(
        self,
        *,
        kind: str,
        key: str,
        inputs: Mapping[str, Any],
        binding_overrides: Sequence[DataBindingOverride],
        mode: str,
        capability: Mapping[str, Any],
    ) -> tuple[str | None, str]:
        parent_key = self.turn_input.idempotency_key
        if parent_key is None:
            return None, uuid4().hex
        digest = canonical_hash(
            {
                "parent_idempotency_key": parent_key,
                "tenant_id": self.tenant_id,
                "scenario_id": self.scenario.id,
                "agent_id": self.agent.id,
                "kind": kind,
                "key": key,
                "inputs": inputs,
                "binding_overrides": tuple(binding_overrides),
                "mode": mode,
                "definition_hash": str(capability.get("definition_hash") or ""),
                "deployment_fingerprint": str(
                    capability.get("deployment_fingerprint") or ""
                ),
            },
            domain="agent-tool-request-v1",
        )
        return f"agent-tool:{digest}", digest

    def _record_receipt(self, receipt_document: Mapping[str, Any]) -> None:
        invocation_id = str(receipt_document.get("invocation_id") or "")
        if not invocation_id:
            return
        invocation = self.db.get(CapabilityInvocation, invocation_id)
        request_document = (
            invocation.request_document
            if invocation is not None and isinstance(invocation.request_document, dict)
            else {}
        )
        invocation_fact = {
            "invocation_id": invocation_id,
            "status": str(receipt_document.get("status") or ""),
            "capability": copy.deepcopy(receipt_document.get("capability") or {}),
            "definition_hash": str(receipt_document.get("definition_hash") or ""),
            "deployment_fingerprint": str(
                receipt_document.get("deployment_fingerprint") or ""
            ),
            "data_context_fingerprint": str(
                receipt_document.get("data_context_fingerprint") or ""
            ),
            "structured_input_hash": str(
                (request_document.get("structured_inputs") or {}).get("hash") or ""
            ),
        }
        if invocation_fact not in self._invocations:
            self._invocations.append(invocation_fact)
        invocation_ref = {
            "kind": "capability_invocation",
            "reference": invocation_id,
            "definition_hash": invocation_fact["definition_hash"],
            "data_context_fingerprint": invocation_fact["data_context_fingerprint"],
        }
        if invocation_ref not in self._evidence_refs:
            self._evidence_refs.append(invocation_ref)
        for item in request_document.get("managed_inputs") or []:
            if not isinstance(item, Mapping):
                continue
            evidence = {
                "kind": str(item.get("resolved_kind") or "managed_input"),
                "port_key": str(item.get("port_key") or ""),
                "signature": str(item.get("signature") or ""),
                "resolved_version_id": str(item.get("resolved_version_id") or "") or None,
                "head_frozen_at_invocation": bool(
                    item.get("head_frozen_at_invocation", False)
                ),
            }
            if evidence not in self._evidence_refs:
                self._evidence_refs.append(evidence)

    def execute_tool(self, name: str, args: Mapping[str, Any]) -> str:
        if name == _CAPABILITY_TOOL_LIST:
            return json.dumps(self.public_catalog(), ensure_ascii=False, sort_keys=True)
        if name != _CAPABILITY_TOOL_INVOKE:
            return _safe_error(
                "UNKNOWN_TOOL",
                "The current capability runtime does not provide this tool",
            )
        kind = str(args.get("kind") or "").strip().lower()
        key = str(args.get("key") or "").strip()
        capability = self._capability_by_ref.get((kind, key))
        if capability is None:
            return _safe_error(
                "CAPABILITY_NOT_AVAILABLE",
                "The requested capability is outside this Agent's governed scope",
                retryable=True,
            )
        if not bool((capability.get("readiness") or {}).get("ready", False)):
            return _safe_error(
                "CAPABILITY_NOT_READY",
                "The requested capability is not ready in this deployment",
            )
        mode = (
            "preview"
            if bool(capability.get("requires_confirmation"))
            or bool(capability.get("side_effect"))
            else "execute"
        )
        trace = self.db.info.get("llm_trace_context")
        correlation_seed = (
            str(trace.get("correlation_id") or "")
            if isinstance(trace, Mapping)
            else ""
        )
        correlation_id = correlation_seed or f"agent:{uuid4().hex}"
        try:
            attachment_overrides = self._attachment_overrides(
                capability,
                args.get("attachment_bindings"),
            )
            occupied_ports = {
                item.port_key
                for item in (*self.turn_input.binding_overrides, *attachment_overrides)
            }
            connection_overrides = self._connection_overrides(
                capability,
                args.get("connection_bindings"),
                occupied_ports,
            )
            request_inputs = self._request_inputs(args.get("inputs"))
            binding_overrides = (
                *self.turn_input.binding_overrides,
                *attachment_overrides,
                *connection_overrides,
            )
            idempotency_key, request_id = self._tool_request_identity(
                kind=kind,
                key=key,
                inputs=request_inputs,
                binding_overrides=binding_overrides,
                mode=mode,
                capability=capability,
            )
            request = Request(
                capability=CapabilityRef(kind=kind, resource_id=key),
                inputs=request_inputs,
                binding_overrides=binding_overrides,
                mode=mode,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                expected_definition_hash=str(capability.get("definition_hash") or ""),
                expected_deployment_fingerprint=str(
                    capability.get("deployment_fingerprint") or ""
                ),
                request_id=request_id,
            )
            receipt = capability_application_service.invoke(
                self.db,
                self.scenario,
                self._actor(),
                request,
                environment=self.environment,
                invocation_source="agent",
            )
            document = capability_application_service.receipt_document(receipt)
            self._record_receipt(document)
            return json.dumps(document, ensure_ascii=False, sort_keys=True)
        except capability_application_service.CapabilityApplicationError as exc:
            self.db.rollback()
            return _safe_error(exc.code.upper(), exc.message)
        except CapabilityInvocationError as exc:
            self.db.rollback()
            return _safe_error(exc.code.upper(), exc.message, retryable=True)
        except (CapabilityContractError, AgentRuntimeAdapterError) as exc:
            self.db.rollback()
            return _safe_error(
                getattr(exc, "code", "INVALID_CAPABILITY_REQUEST").upper(),
                str(exc),
                retryable=True,
            )

    def authorize_historic_tool_result(
        self,
        name: str,
        _args: Mapping[str, Any],
        raw_result: Any,
    ) -> bool:
        parsed = _parse_result(raw_result)
        if name == _CAPABILITY_TOOL_LIST:
            return parsed == self.public_catalog()
        if name != _CAPABILITY_TOOL_INVOKE or not isinstance(parsed, Mapping):
            return False
        invocation_id = str(parsed.get("invocation_id") or "")
        if not invocation_id:
            return False
        try:
            current = capability_application_service.get_receipt(
                self.db,
                self._actor(),
                invocation_id,
            )
        except capability_application_service.CapabilityApplicationError:
            return False
        return json.loads(canonical_json(parsed)) == json.loads(canonical_json(current))

    def _system_prompt(self) -> str:
        base = self.agent.system_prompt or "你是一名专业的业务智能助手。"
        catalog = [
            {
                "kind": item["kind"],
                "key": item["key"],
                "name": item["name"],
                "description": item["description"],
                "input_schema": item["input_schema"],
                "output_schema": item["output_schema"],
                "data_ports": item["data_ports"],
                "readiness": item["readiness"],
                "requires_confirmation": item["requires_confirmation"],
            }
            for item in self.capabilities
        ]
        return "\n".join(
            [
                base,
                f"【验证 Agent 职责】{self.agent.description}" if self.agent.description else "",
                f"【当前业务场景】{self.scenario.name}",
                "【能力运行约束】只使用本轮提供的通用能力工具。先核对机器可读契约，再按 kind/key 调用；"
                "不得猜测数据源、表、列、Provider 或物理连接信息。调用结果中的定义哈希、数据上下文指纹"
                "和证据引用是本次回答的依据。需要确认或存在副作用的能力只能生成预演，不能替用户确认。",
                AUTHORITATIVE_DECISION_PROMPT,
                "【当前能力目录】\n"
                + json.dumps(catalog, ensure_ascii=False, sort_keys=True),
                (
                    "【本轮用户附件】模型必须根据用户需求和能力数据端口自主选择能力，"
                    "并在 invoke_capability 时完成附件到端口的映射；不得要求用户填写端口或 JSON。\n"
                    + json.dumps(
                        [
                            {"attachment_index": index, "filename": item.filename}
                            for index, item in enumerate(self.turn_input.attachments)
                        ],
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    if self.turn_input.attachments
                    else ""
                ),
                (
                    "【Agent 业务数据库】这些连接是在 Agent 创建/编辑时配置的正式运行数据源。"
                    "模型应根据能力的 connector 数据端口自主选择，不得使用建模资料页的数据库连接。\n"
                    + json.dumps(
                        [
                            {
                                "connection_index": index,
                                "name": item["name"],
                                "adapter": item["adapter"],
                                "ready": item["ready"],
                            }
                            for index, item in enumerate(self.runtime_connection_options)
                        ],
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    if self.runtime_connection_options
                    else ""
                ),
            ]
        )

    def _model_user_message(self, message: str) -> str:
        parts = [str(message or "")]
        if self.turn_input.structured_inputs:
            parts.append(
                "【客户端提供的结构化输入（其显式字段优先于模型补全）】\n"
                + json.dumps(
                    self.turn_input.structured_inputs,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        if self.turn_input.binding_overrides:
            parts.append(
                "【本次受管输入端口】"
                + "、".join(item.port_key for item in self.turn_input.binding_overrides)
                + "。引用由服务端解析，禁止猜测或输出物理连接信息。"
            )
        if self.turn_input.attachments:
            parts.append(
                "【本次对话上传文件】"
                + "、".join(
                    f"附件{index}：{item.filename or '未命名文件'}"
                    for index, item in enumerate(self.turn_input.attachments)
                )
                + "。这些文件是本轮正式业务数据，不是建模资料；请自行选择合适的业务能力处理。"
            )
        return "\n\n".join(part for part in parts if part)

    def run_agent(
        self,
        history: list[dict[str, Any]],
        user_message: str,
    ) -> Iterator[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt()},
            *history,
            {"role": "user", "content": self._model_user_message(user_message)},
        ]
        tools = self.build_tools()
        max_rounds = max(1, min(get_settings().max_tool_rounds, 24))
        tool_call_counts: defaultdict[str, int] = defaultdict(int)
        for _round in range(max_rounds):
            content_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for event in llm_service.chat_stream(
                self.llm,
                messages,
                tools=tools or None,
                temperature=self.agent.temperature,
                max_tokens=self.agent.max_tokens,
                db=self.db,
            ):
                if event["type"] == "token":
                    content_parts.append(event["content"])
                elif event["type"] == "tool_calls":
                    tool_calls = event["tool_calls"]
            content = "".join(content_parts)
            if not tool_calls:
                for part in content_parts:
                    yield {"type": "token", "data": part}
                if self._evidence_refs:
                    yield {"type": "evidence_refs", "data": self.evidence_snapshot()}
                yield {"type": "done", "data": content}
                return
            messages.append(
                {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["function"]["name"],
                                "arguments": json.dumps(
                                    call["function"].get("arguments") or {},
                                    ensure_ascii=False,
                                ),
                            },
                        }
                        for call in tool_calls
                    ],
                }
            )
            for call in tool_calls:
                name = str(call["function"]["name"])
                arguments = call["function"].get("arguments") or {}
                yield {
                    "type": "tool_call",
                    "data": {"id": call["id"], "name": name, "arguments": arguments},
                }
                signature = json.dumps(
                    {"name": name, "arguments": arguments},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if tool_call_counts[signature]:
                    result = _safe_error(
                        "DUPLICATE_TOOL_CALL",
                        "The same capability call already completed in this turn",
                    )
                else:
                    result = self.execute_tool(name, arguments)
                tool_call_counts[signature] += 1
                yield {
                    "type": "tool_result",
                    "data": {"id": call["id"], "name": name, "result": result},
                }
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": name,
                        "content": result,
                    }
                )
        fallback = "能力调用轮次已达到上限；请基于已返回的回执说明结论和仍缺少的信息。"
        yield {"type": "token", "data": fallback}
        if self._evidence_refs:
            yield {"type": "evidence_refs", "data": self.evidence_snapshot()}
        yield {"type": "done", "data": fallback}


def _capability_runtime_fact(
    runtime: CapabilityAgentRuntime | None,
    error: AgentRuntimeAdapterError | None,
) -> dict[str, Any]:
    if runtime is None:
        return {
            "resolved": False,
            "complete": False,
            "error": {
                "code": error.code if error else "capability_context_unavailable",
                "message": error.message if error else "Capability context is unavailable",
            },
        }
    return {
        "resolved": True,
        "complete": runtime.complete,
        "environment": runtime.environment,
        "definition_hash": runtime.deployment.definition_hash,
        "deployment_fingerprint": runtime.deployment.fingerprint,
        "data_context_fingerprint": runtime.runtime_data_context.fingerprint,
        "data_handle_count": len(runtime.runtime_data_context.handles),
        "selected_capability_count": len(runtime.capabilities),
        "issues": copy.deepcopy(runtime.context_issues),
    }


def build_runtime_context(
    db: Session,
    agent: Agent,
    llm: LLMConfig,
    *,
    turn_input: AgentTurnInput | None = None,
    environment: str | None = None,
) -> Any:
    """Build the only executable Agent runtime and reject historical modes."""

    mode = str(getattr(agent, "runtime_binding_mode", "legacy") or "legacy").strip()
    if mode in _HISTORICAL_MODES:
        raise AgentRuntimeAdapterError(
            "historical_runtime_disabled",
            "Historical Agent runtime modes are disabled; migrate the Agent to "
            "capability_only",
        )
    if mode != "capability_only":
        raise AgentRuntimeAdapterError(
            "invalid_runtime_binding_mode",
            "Agent runtime binding mode is invalid",
        )

    normalized_input = turn_input or AgentTurnInput()
    capability_runtime: CapabilityAgentRuntime | None = None
    capability_error: AgentRuntimeAdapterError | None = None
    try:
        capability_runtime = CapabilityAgentRuntime(
            db,
            agent,
            llm,
            turn_input=normalized_input,
            environment=environment,
        )
    except AgentRuntimeAdapterError as exc:
        capability_error = exc

    capability_fact = _capability_runtime_fact(capability_runtime, capability_error)
    if capability_runtime is None:
        raise capability_error or AgentRuntimeAdapterError(
            "capability_context_unavailable",
            "Capability-only Agent cannot resolve its runtime context",
        )
    capability_runtime.set_runtime_decision(
        {
            "contract": "agent-runtime-decision/v1",
            "configured_mode": mode,
            "selected_path": "capability",
            "fallback": {"used": False},
            "capability_context": capability_fact,
        }
    )
    return capability_runtime


def input_snapshot(context: Any) -> dict[str, Any]:
    method = getattr(context, "input_snapshot", None)
    return method() if callable(method) else {}


def evidence_snapshot(context: Any) -> list[dict[str, Any]]:
    method = getattr(context, "evidence_snapshot", None)
    return method() if callable(method) else []


__all__ = [
    "AgentRuntimeAdapterError",
    "AgentTurnInput",
    "CapabilityAgentRuntime",
    "build_runtime_context",
    "evidence_snapshot",
    "input_snapshot",
]
