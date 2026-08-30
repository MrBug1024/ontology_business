"""Server-authoritative shadow gates and reversible Agent mode migration.

Metrics are derived either from persisted Agent runtime snapshots or from an
internal validation executor that submits hashes and counts only.  No protocol
endpoint accepts comparison values.  Mode changes and rollbacks are immutable
ledger checkpoints scoped to one tenant and one Agent.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Agent,
    CapabilityInvocation,
    Conversation,
    LLMConfig,
    Message,
    PlatformMigrationCheckpoint,
    PlatformMigrationRun,
)
from . import (
    agent_readiness_service,
    agent_runtime_adapter,
    capability_application_service,
    permission_service,
    tenant_service,
)
from .capability_contracts import (
    CapabilityContractError,
    CapabilityRef,
    Request,
    canonical_hash,
    canonical_json,
)
from .capability_invoker import (
    CapabilityInvocationError,
    runtime_context_from_invocation_audit,
)


MIGRATION_CONTRACT = "agent-capability-migration/v1"
SHADOW_CONTRACT = "agent-shadow-observation/v1"
MIN_GATE_OBSERVATIONS = 2
MAX_GATE_ROW_DELTA = 0
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MODES = ("legacy", "shadow", "prefer_capability", "capability_only")
_PLAN_DIGEST = canonical_hash(
    {
        "contract": MIGRATION_CONTRACT,
        "modes": _MODES,
        "minimum_observations": MIN_GATE_OBSERVATIONS,
        "maximum_absolute_row_delta": MAX_GATE_ROW_DELTA,
        "required": ["schema_match", "row_match_or_not_applicable", "result_match"],
    },
    domain="platform-migration-plan-v1",
)
_SHADOW_STAGE = "shadow_metric"
_MODE_STAGE = "agent_mode_event"


class AgentMigrationError(RuntimeError):
    """A safe, structured Agent migration failure."""

    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = str(code or "agent_migration_error")
        self.message = str(message or "Agent migration failed")
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ServerShadowProbe:
    """Credential-free evidence produced by a platform-owned shadow executor.

    The executor compares outputs in memory and supplies only canonical hashes
    and aggregate row counts.  The HTTP layer intentionally has no schema for
    this value.
    """

    observation_id: str
    legacy_schema_hash: str
    capability_schema_hash: str
    legacy_result_hash: str
    capability_result_hash: str
    legacy_row_count: int | None
    capability_row_count: int | None
    rows_applicable: bool = True
    capability_complete: bool = True
    fallback_used: bool = False
    source_message_id: str | None = None
    source_legacy_tool_result_id: str | None = None
    capability_invocation_id: str | None = None

    def __post_init__(self) -> None:
        observation_id = str(self.observation_id or "").strip()
        if not observation_id or len(observation_id) > 180:
            raise AgentMigrationError(
                "invalid_shadow_observation", "Shadow observation id is invalid"
            )
        object.__setattr__(self, "observation_id", observation_id)
        for field_name in (
            "legacy_schema_hash",
            "capability_schema_hash",
            "legacy_result_hash",
            "capability_result_hash",
        ):
            value = str(getattr(self, field_name) or "").strip().lower()
            if _SHA256_RE.fullmatch(value) is None:
                raise AgentMigrationError(
                    "invalid_shadow_observation",
                    f"{field_name} must be a canonical SHA-256",
                )
            object.__setattr__(self, field_name, value)
        if self.rows_applicable:
            for field_name in ("legacy_row_count", "capability_row_count"):
                value = getattr(self, field_name)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise AgentMigrationError(
                        "invalid_shadow_observation",
                        "Applicable shadow row counts must be non-negative integers",
                    )
        elif self.legacy_row_count is not None or self.capability_row_count is not None:
            raise AgentMigrationError(
                "invalid_shadow_observation",
                "Non-applicable row metrics must not contain counts",
            )
        source_message_id = str(self.source_message_id or "").strip() or None
        if source_message_id and len(source_message_id) > 32:
            raise AgentMigrationError(
                "invalid_shadow_observation", "Source message id is invalid"
            )
        object.__setattr__(self, "source_message_id", source_message_id)
        source_tool_result_id = (
            str(self.source_legacy_tool_result_id or "").strip() or None
        )
        if source_tool_result_id and len(source_tool_result_id) > 240:
            raise AgentMigrationError(
                "invalid_shadow_observation", "Source tool result id is invalid"
            )
        object.__setattr__(
            self, "source_legacy_tool_result_id", source_tool_result_id
        )
        capability_invocation_id = (
            str(self.capability_invocation_id or "").strip() or None
        )
        if capability_invocation_id and len(capability_invocation_id) > 32:
            raise AgentMigrationError(
                "invalid_shadow_observation", "Capability invocation id is invalid"
            )
        object.__setattr__(
            self, "capability_invocation_id", capability_invocation_id
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tenant(db: Session) -> str:
    return tenant_service.current_tenant_id(db)


def _migration_name(tenant_id: str) -> str:
    value = f"agent-cutover:{tenant_id}"
    if len(value) > 120:
        value = f"agent-cutover:{hashlib.sha256(tenant_id.encode()).hexdigest()}"
    return value


def _require_agent(db: Session, agent_id: str, *, manage: bool = True) -> Agent:
    if manage:
        permission_service.require_tenant_permission(db, "manage")
    agent = db.execute(
        select(Agent)
        .where(Agent.id == agent_id, Agent.tenant_id == _tenant(db))
        .with_for_update()
    ).scalar_one_or_none()
    if agent is None:
        raise AgentMigrationError("agent_not_found", "Agent does not exist", status_code=404)
    if agent.scenario_id:
        scenario = tenant_service.require_scenario(db, agent.scenario_id, writable=manage)
        permission_service.require_scenario_permission(
            db, scenario, "write" if manage else "read"
        )
    return agent


def _load_or_create_run(db: Session) -> PlatformMigrationRun:
    tenant_id = _tenant(db)
    name = _migration_name(tenant_id)
    run = db.execute(
        select(PlatformMigrationRun)
        .where(
            PlatformMigrationRun.migration_name == name,
            PlatformMigrationRun.plan_digest == _PLAN_DIGEST,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if run is not None:
        if str((run.manifest or {}).get("tenant_id") or "") != tenant_id:
            raise AgentMigrationError(
                "migration_tenant_mismatch",
                "Agent migration ledger does not belong to this tenant",
                status_code=403,
            )
        return run
    clock = _now()
    run = PlatformMigrationRun(
        id=uuid4().hex,
        migration_name=name,
        plan_digest=_PLAN_DIGEST,
        source_fingerprint=canonical_hash(
            {"tenant_id": tenant_id}, domain="agent-migration-source-v1"
        ),
        status="running",
        current_phase="verify",
        manifest={
            "contract": MIGRATION_CONTRACT,
            "tenant_id": tenant_id,
            "gate_policy": {
                "minimum_observations": MIN_GATE_OBSERVATIONS,
                "maximum_absolute_row_delta": MAX_GATE_ROW_DELTA,
                "window": "latest_required_count",
            },
        },
        started_at=clock,
        updated_at=clock,
        completed_at=None,
        last_error="",
    )
    db.add(run)
    db.flush()
    return run


def _checkpoint(
    db: Session,
    run: PlatformMigrationRun,
    *,
    stage: str,
    item_key: str,
    payload: dict[str, Any],
) -> tuple[PlatformMigrationCheckpoint, bool]:
    existing = db.get(
        PlatformMigrationCheckpoint,
        {"run_id": run.id, "stage": stage, "item_key": item_key},
    )
    digest = canonical_hash(payload, domain="agent-migration-checkpoint-v1")
    if existing is not None:
        if existing.payload_sha256 != digest:
            raise AgentMigrationError(
                "migration_idempotency_conflict",
                "The migration idempotency key was already used for different content",
            )
        return existing, False
    checkpoint = PlatformMigrationCheckpoint(
        run_id=run.id,
        stage=stage,
        item_key=item_key,
        status="complete",
        payload_sha256=digest,
        row_count=None,
        payload=payload,
        completed_at=_now(),
    )
    db.add(checkpoint)
    db.flush()
    return checkpoint, True


def _require_shadow_source_message(
    db: Session, agent: Agent, message_id: str
) -> Message:
    message = db.execute(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.id == message_id,
            Conversation.agent_id == agent.id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if message is None:
        raise AgentMigrationError(
            "shadow_source_not_found", "Shadow source message does not belong to this Agent"
        )
    runtime = (message.input_snapshot or {}).get("runtime") or {}
    if not isinstance(runtime, dict) or runtime.get("configured_mode") != "shadow":
        raise AgentMigrationError(
            "shadow_source_invalid", "Source message is not a persisted shadow execution"
        )
    return message


def _automatic_observation(message: Message, agent: Agent) -> dict[str, Any] | None:
    snapshot = message.input_snapshot or {}
    runtime = snapshot.get("runtime") or {}
    if not isinstance(runtime, dict) or runtime.get("configured_mode") != "shadow":
        return None
    shadow = runtime.get("shadow") or {}
    comparison = shadow.get("comparison") or {}
    capability = runtime.get("capability_context") or {}
    legacy = runtime.get("legacy_context") or {}
    legacy_definition = str(legacy.get("definition_hash") or "").lower()
    capability_definition = str(capability.get("definition_hash") or "").lower()
    schema_comparable = bool(
        _SHA256_RE.fullmatch(legacy_definition)
        and _SHA256_RE.fullmatch(capability_definition)
    )
    return {
        "contract": SHADOW_CONTRACT,
        "agent_id": agent.id,
        "observation_id": f"message:{message.id}",
        "source": {"kind": "agent_runtime_snapshot", "message_id": message.id},
        "gate_eligible": False,
        "schema": {
            "comparable": schema_comparable,
            "legacy_hash": legacy_definition if schema_comparable else "",
            "capability_hash": capability_definition if schema_comparable else "",
            "equal": bool(
                schema_comparable and legacy_definition == capability_definition
            ),
        },
        # Resource counts are retained as diagnostic facts but are explicitly
        # not mislabeled as business row counts.
        "rows": {
            "comparable": False,
            "reason": "business_row_counts_not_observed",
            "legacy_resource_count": int(comparison.get("legacy_source_count") or 0),
            "capability_resource_count": int(
                comparison.get("capability_data_handle_count") or 0
            ),
        },
        "result": {
            "comparable": False,
            "reason": "shadow_runtime_executes_legacy_only",
        },
        "capability_complete": bool(capability.get("complete", False)),
        "fallback_used": bool((runtime.get("fallback") or {}).get("used", False)),
        "recorded_at": message.created_at.isoformat(),
    }


def refresh_shadow_observations(db: Session, agent_id: str) -> dict[str, Any]:
    """Derive diagnostic observations only from persisted server snapshots."""

    agent = _require_agent(db, agent_id)
    run = _load_or_create_run(db)
    messages = list(
        db.scalars(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.agent_id == agent.id,
                Message.role == "assistant",
            )
            .order_by(Message.created_at, Message.id)
        ).all()
    )
    created = 0
    for message in messages:
        payload = _automatic_observation(message, agent)
        if payload is None:
            continue
        _row, inserted = _checkpoint(
            db,
            run,
            stage=_SHADOW_STAGE,
            item_key=f"{agent.id}:{payload['observation_id']}",
            payload=payload,
        )
        created += int(inserted)
    run.updated_at = _now()
    db.flush()
    status = agent_migration_status(db, agent.id, _agent=agent, _run=run)
    status["refreshed_observations"] = created
    return status


def _shadow_turn_input(
    *,
    source_message_id: str,
    legacy_tool_result_id: str,
    capability_kind: str,
    capability_key: str,
    inputs: Mapping[str, Any],
    managed_inputs: Sequence[Mapping[str, Any]],
) -> agent_runtime_adapter.AgentTurnInput:
    try:
        overrides = tuple(
            capability_application_service.managed_binding_override(dict(item))
            for item in managed_inputs
        )
        managed_override_hash = canonical_hash(
            [
                {
                    "binding_key": item.binding_key,
                    "port_key": item.port_key,
                    "binding_kind": item.binding_kind,
                    "reference_id": item.reference_id,
                    "selector": item.selector,
                    "signature": item.signature,
                    "version_id": item.version_id,
                }
                for item in sorted(
                    overrides,
                    key=lambda candidate: candidate.port_key,
                )
            ],
            domain="capability-managed-override-intent-v1",
        )
        structured_input_hash = canonical_hash(
            dict(inputs),
            domain="capability-structured-input-v1",
        )
        request_identity = canonical_hash(
            {
                "source_message_id": source_message_id,
                "legacy_tool_result_id": legacy_tool_result_id,
                "capability": {
                    "kind": capability_kind,
                    "key": capability_key,
                },
                "structured_input_hash": structured_input_hash,
                "managed_override_hash": managed_override_hash,
            },
            domain="agent-shadow-validation-request-v1",
        )
        return agent_runtime_adapter.AgentTurnInput(
            structured_inputs=dict(inputs),
            binding_overrides=overrides,
            target_kind=capability_kind,
            target_key=capability_key,
            idempotency_key=f"shadow-validation:{request_identity}",
        )
    except capability_application_service.CapabilityApplicationError as exc:
        raise AgentMigrationError(
            exc.code,
            exc.message,
            status_code=exc.status_code,
        ) from None
    except (CapabilityContractError, agent_runtime_adapter.AgentRuntimeAdapterError):
        raise AgentMigrationError(
            "invalid_shadow_inputs",
            "Shadow validation inputs are invalid",
            status_code=422,
        ) from None


def _safe_input_document(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "contract": document.get("contract"),
        "structured_inputs": document.get("structured_inputs"),
        "managed_inputs": document.get("managed_inputs"),
        "target": document.get("target"),
    }


def _require_matching_shadow_input(
    message: Message,
    runtime: agent_runtime_adapter.CapabilityAgentRuntime,
) -> None:
    recorded = message.input_snapshot if isinstance(message.input_snapshot, dict) else {}
    expected = runtime.input_snapshot()
    try:
        matches = canonical_hash(
            _safe_input_document(recorded),
            domain="agent-shadow-safe-input-v1",
        ) == canonical_hash(
            _safe_input_document(expected),
            domain="agent-shadow-safe-input-v1",
        )
    except CapabilityContractError:
        raise AgentMigrationError(
            "shadow_source_invalid",
            "Source message has an invalid input snapshot",
        ) from None
    if recorded.get("contract") != "agent-turn-input/v1" or not matches:
        raise AgentMigrationError(
            "shadow_input_mismatch",
            "Submitted inputs do not match the persisted shadow turn",
            status_code=422,
        )


def _require_matching_shadow_deployment(
    message: Message,
    runtime: agent_runtime_adapter.CapabilityAgentRuntime,
) -> dict[str, Any]:
    snapshot = message.input_snapshot if isinstance(message.input_snapshot, dict) else {}
    runtime_snapshot = snapshot.get("runtime") or {}
    if not isinstance(runtime_snapshot, dict):
        raise AgentMigrationError(
            "shadow_source_invalid", "Source message has an invalid runtime snapshot"
        )
    capability = runtime_snapshot.get("capability_context") or {}
    if not isinstance(capability, dict):
        raise AgentMigrationError(
            "shadow_source_invalid", "Source message has an invalid capability snapshot"
        )
    current = {
        "definition_hash": runtime.deployment.definition_hash,
        "deployment_fingerprint": runtime.deployment.fingerprint,
        "data_context_fingerprint": runtime.runtime_data_context.fingerprint,
    }
    if any(str(capability.get(key) or "") != value for key, value in current.items()):
        raise AgentMigrationError(
            "shadow_deployment_changed",
            "The capability deployment changed after the persisted shadow turn",
        )
    return capability


def _tool_call_id(call: Any) -> str:
    return str(call.get("id") or "").strip() if isinstance(call, Mapping) else ""


def _tool_call_name_arguments(call: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    function = call.get("function") if isinstance(call.get("function"), Mapping) else {}
    name = str(call.get("name") or function.get("name") or "").strip()
    raw_arguments = call.get(
        "args",
        call.get("arguments", function.get("arguments", {})),
    )
    if isinstance(raw_arguments, str):
        try:
            raw_arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            raw_arguments = None
    if not name or not isinstance(raw_arguments, Mapping):
        raise AgentMigrationError(
            "legacy_tool_call_invalid",
            "The persisted legacy tool call is not canonically attributable",
        )
    try:
        arguments = json.loads(canonical_json(dict(raw_arguments)))
    except CapabilityContractError:
        raise AgentMigrationError(
            "legacy_tool_call_invalid",
            "The persisted legacy tool call is not canonically attributable",
        ) from None
    return name, arguments


def _legacy_call_result(
    message: Message,
    tool_result_id: str,
) -> tuple[str, dict[str, Any], Any]:
    calls = [
        item
        for item in (message.tool_calls or [])
        if _tool_call_id(item) == tool_result_id
    ]
    results = [
        item
        for item in (message.tool_results or [])
        if isinstance(item, Mapping)
        and str(item.get("id") or "").strip() == tool_result_id
    ]
    if len(calls) != 1 or len(results) != 1 or "result" not in results[0]:
        raise AgentMigrationError(
            "legacy_tool_result_not_found",
            "The persisted legacy tool result is unavailable or ambiguous",
            status_code=404,
        )
    name, arguments = _tool_call_name_arguments(calls[0])
    result_name = str(results[0].get("name") or "").strip()
    if not result_name or result_name != name:
        raise AgentMigrationError(
            "legacy_tool_identity_mismatch",
            "The persisted legacy tool call and result identities do not match",
        )
    raw = results[0]["result"]
    if isinstance(raw, str):
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            result = raw
    else:
        try:
            result = json.loads(canonical_json(raw))
        except CapabilityContractError:
            raise AgentMigrationError(
                "legacy_tool_result_invalid",
                "The persisted legacy tool result is not comparable",
            ) from None
    try:
        canonical_json(result)
    except CapabilityContractError:
        raise AgentMigrationError(
            "legacy_tool_result_invalid",
            "The persisted legacy tool result is not comparable",
        ) from None
    return name, arguments, result


def _schema_outline(value: Any, *, depth: int = 0) -> dict[str, Any]:
    """Describe JSON structure without retaining scalar values or row counts."""

    if depth >= 12:
        return {"type": "truncated"}
    if isinstance(value, Mapping):
        return {
            "type": "object",
            "fields": {
                str(key): _schema_outline(item, depth=depth + 1)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            },
        }
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        unique: dict[str, dict[str, Any]] = {}
        for item in value:
            outline = _schema_outline(item, depth=depth + 1)
            unique[canonical_json(outline)] = outline
        return {
            "type": "array",
            "items": [unique[key] for key in sorted(unique)],
        }
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


def _require_finalized_legacy_shadow(message: Message) -> dict[str, Any]:
    if message.role != "assistant" or not bool(message.stream_finalized):
        raise AgentMigrationError(
            "shadow_source_not_finalized",
            "Source message must be a finalized assistant response",
        )
    runtime_snapshot = (message.input_snapshot or {}).get("runtime") or {}
    shadow_snapshot = (
        runtime_snapshot.get("shadow")
        if isinstance(runtime_snapshot, dict)
        and isinstance(runtime_snapshot.get("shadow"), dict)
        else {}
    )
    fallback_snapshot = (
        runtime_snapshot.get("fallback")
        if isinstance(runtime_snapshot, dict)
        and isinstance(runtime_snapshot.get("fallback"), dict)
        else {}
    )
    if (
        not isinstance(runtime_snapshot, dict)
        or runtime_snapshot.get("selected_path") != "legacy"
        or shadow_snapshot.get("executed_path") != "legacy"
        or bool(fallback_snapshot.get("used", False))
    ):
        raise AgentMigrationError(
            "shadow_source_invalid",
            "Source message is not a finalized legacy-path shadow execution",
        )
    return runtime_snapshot


def _legacy_capability_match(
    db: Session,
    agent: Agent,
    llm: LLMConfig,
    message: Message,
    legacy_tool_result_id: str,
) -> tuple[Any, Any, str]:
    from . import agent_engine  # local import avoids an engine/migration cycle

    name, arguments, legacy_result = _legacy_call_result(
        message,
        legacy_tool_result_id,
    )
    try:
        legacy_context = agent_engine.AgentContext(db, agent, llm)
    except Exception:
        raise AgentMigrationError(
            "shadow_legacy_context_unavailable",
            "The legacy runtime context cannot be re-authorized",
        ) from None
    runtime_snapshot = _require_finalized_legacy_shadow(message)
    recorded_legacy = runtime_snapshot.get("legacy_context") or {}
    current_legacy = agent_runtime_adapter.legacy_runtime_fact(legacy_context)
    if not isinstance(recorded_legacy, Mapping) or any(
        str(recorded_legacy.get(key) or "") != str(current_legacy.get(key) or "")
        for key in ("definition_hash", "semantic_contract_fingerprint")
    ):
        raise AgentMigrationError(
            "shadow_legacy_context_changed",
            "The legacy runtime context changed after the persisted shadow turn",
        )
    match = legacy_context.match_historic_capability_result(
        name,
        arguments,
        legacy_result,
    )
    if match is None:
        raise AgentMigrationError(
            "shadow_legacy_target_unproven",
            "The legacy tool result cannot be attributed to a capability target and typed inputs",
        )
    safe_snapshot = message.input_snapshot if isinstance(message.input_snapshot, dict) else {}
    target = safe_snapshot.get("target") or {}
    structured = safe_snapshot.get("structured_inputs") or {}
    if (
        not isinstance(target, Mapping)
        or (str(target.get("kind") or ""), str(target.get("key") or ""))
        != (match.capability_kind, match.capability_key)
        or not isinstance(structured, Mapping)
        or str(structured.get("hash") or "")
        != canonical_hash(match.inputs, domain="agent-structured-input-v1")
        or str(structured.get("invocation_hash") or "")
        != canonical_hash(match.inputs, domain="capability-structured-input-v1")
    ):
        raise AgentMigrationError(
            "shadow_legacy_input_mismatch",
            "The legacy tool call does not match the persisted capability target and inputs",
        )
    linkage_hash = canonical_hash(
        {
            "owner_key": match.owner_key,
            "owner_version": match.owner_version,
            "legacy_tool_name": name,
            "capability": {
                "kind": match.capability_kind,
                "key": match.capability_key,
            },
            "structured_input_hash": structured["invocation_hash"],
        },
        domain="agent-shadow-legacy-capability-link-v1",
    )
    return legacy_context, match, linkage_hash


def _require_attributable_invocation(
    agent: Agent,
    invocation: CapabilityInvocation | None,
    *,
    capability_kind: str,
    capability_key: str,
) -> CapabilityInvocation:
    if (
        invocation is None
        or invocation.tenant_id != agent.tenant_id
        or invocation.scenario_id != agent.scenario_id
        or invocation.agent_id != agent.id
        or invocation.principal_type != "agent"
        or invocation.principal_id != agent.id
        or invocation.invocation_source != "agent"
        or invocation.capability_kind != capability_kind
        or invocation.capability_key != capability_key
        or invocation.status != "succeeded"
    ):
        raise AgentMigrationError(
            "shadow_capability_failed",
            "Capability shadow execution did not produce an attributable successful invocation",
        )
    return invocation


def _derive_authoritative_shadow_facts(
    db: Session,
    agent: Agent,
    *,
    source_message_id: str,
    legacy_tool_result_id: str,
    capability_invocation_id: str,
) -> dict[str, Any]:
    message = _require_shadow_source_message(db, agent, source_message_id)
    runtime_snapshot = _require_finalized_legacy_shadow(message)
    llm = (
        tenant_service.get_visible(db, LLMConfig, agent.llm_config_id)
        if agent.llm_config_id
        else LLMConfig(name="Server shadow validation")
    )
    if llm is None:
        raise AgentMigrationError(
            "agent_runtime_not_ready", "Agent validation model is unavailable"
        )
    legacy_context, match, linkage_hash = _legacy_capability_match(
        db,
        agent,
        llm,
        message,
        legacy_tool_result_id,
    )
    invocation = _require_attributable_invocation(
        agent,
        db.get(CapabilityInvocation, capability_invocation_id),
        capability_kind=match.capability_kind,
        capability_key=match.capability_key,
    )
    capability_snapshot = runtime_snapshot.get("capability_context") or {}
    if (
        not isinstance(capability_snapshot, Mapping)
        or not bool(capability_snapshot.get("complete", False))
        or str(capability_snapshot.get("definition_hash") or "")
        != invocation.definition_hash
        or str(capability_snapshot.get("deployment_fingerprint") or "")
        != invocation.deployment_fingerprint
    ):
        raise AgentMigrationError(
            "shadow_invocation_context_mismatch",
            "Capability invocation does not match the persisted shadow deployment",
        )
    safe_snapshot = message.input_snapshot if isinstance(message.input_snapshot, dict) else {}
    structured = safe_snapshot.get("structured_inputs") or {}
    request_document = (
        invocation.request_document
        if isinstance(invocation.request_document, dict)
        else {}
    )
    request_structured = request_document.get("structured_inputs") or {}
    if (
        not isinstance(structured, Mapping)
        or not isinstance(request_structured, Mapping)
        or str(structured.get("invocation_hash") or "")
        != str(request_structured.get("hash") or "")
        or str(safe_snapshot.get("managed_override_hash") or "")
        != str(request_document.get("managed_override_hash") or "")
    ):
        raise AgentMigrationError(
            "shadow_invocation_input_mismatch",
            "Capability invocation does not match the persisted shadow input",
        )
    expected_request_digest = canonical_hash(
        {
            "source_message_id": message.id,
            "legacy_tool_result_id": legacy_tool_result_id,
            "capability": {
                "kind": match.capability_kind,
                "key": match.capability_key,
            },
            "structured_input_hash": structured["invocation_hash"],
            "managed_override_hash": safe_snapshot["managed_override_hash"],
        },
        domain="agent-shadow-validation-request-v1",
    )
    if (
        invocation.request_id != expected_request_digest
        or invocation.idempotency_key
        != f"shadow-validation:{expected_request_digest}"
        or invocation.correlation_id != f"agent-shadow:{expected_request_digest}"
    ):
        raise AgentMigrationError(
            "shadow_invocation_lineage_mismatch",
            "Capability invocation was not issued for the persisted shadow validation request",
        )
    try:
        invocation_data_context = runtime_context_from_invocation_audit(
            db,
            invocation,
        )
    except CapabilityContractError:
        raise AgentMigrationError(
            "shadow_data_context_unproven",
            "Capability input audit cannot reproduce its runtime data context",
        ) from None
    if invocation_data_context.fingerprint != invocation.data_context_fingerprint:
        raise AgentMigrationError(
            "shadow_data_context_unproven",
            "Capability input audit does not match the recorded runtime data context",
        )
    data_equivalence_hash = legacy_context.verify_shadow_data_context(
        match,
        invocation_data_context,
    )
    if _SHA256_RE.fullmatch(str(data_equivalence_hash or "")) is None:
        raise AgentMigrationError(
            "shadow_data_context_mismatch",
            "Legacy and capability execution are not proven to use the same data context",
        )
    result_document = (
        invocation.result_document
        if isinstance(invocation.result_document, dict)
        else {}
    )
    if "output" not in result_document:
        raise AgentMigrationError(
            "shadow_capability_failed",
            "Capability shadow execution did not produce a comparable result",
        )
    capability_output = result_document["output"]
    if str(result_document.get("output_hash") or "") != canonical_hash(
        capability_output,
        domain="capability-output-v1",
    ):
        raise AgentMigrationError(
            "shadow_capability_result_invalid",
            "Capability invocation result integrity verification failed",
        )
    capability_result = legacy_context.normalize_capability_shadow_result(
        match,
        capability_output,
    )
    if capability_result is None:
        raise AgentMigrationError(
            "shadow_capability_result_unproven",
            "Capability output is not covered by the legacy compatibility contract",
        )
    legacy_result = match.comparison_result
    try:
        legacy_schema_hash = canonical_hash(
            _schema_outline(legacy_result), domain="shadow-result-schema-v1"
        )
        capability_schema_hash = canonical_hash(
            _schema_outline(capability_result), domain="shadow-result-schema-v1"
        )
        legacy_result_hash = canonical_hash(
            legacy_result, domain="shadow-result-value-v1"
        )
        capability_result_hash = canonical_hash(
            capability_result, domain="shadow-result-value-v1"
        )
    except CapabilityContractError:
        raise AgentMigrationError(
            "shadow_result_invalid", "Shadow results are not canonically comparable"
        ) from None
    rows_applicable = isinstance(legacy_result, list) and isinstance(
        capability_result, list
    )
    legacy_row_count = len(legacy_result) if rows_applicable else None
    capability_row_count = len(capability_result) if rows_applicable else None
    observation_id = "validated:" + canonical_hash(
        {
            "source_message_id": message.id,
            "legacy_tool_result_id": legacy_tool_result_id,
            "capability_invocation_id": invocation.id,
            "input_hash": invocation.input_hash,
        },
        domain="agent-shadow-observation-id-v1",
    )
    return {
        "agent_id": agent.id,
        "observation_id": observation_id,
        "legacy_schema_hash": legacy_schema_hash,
        "capability_schema_hash": capability_schema_hash,
        "legacy_result_hash": legacy_result_hash,
        "capability_result_hash": capability_result_hash,
        "legacy_row_count": legacy_row_count,
        "capability_row_count": capability_row_count,
        "rows_applicable": rows_applicable,
        "capability_complete": True,
        "fallback_used": False,
        "source_message_id": message.id,
        "source_legacy_tool_result_id": legacy_tool_result_id,
        "capability_invocation_id": invocation.id,
        "linkage_hash": linkage_hash,
        "invocation_input_hash": invocation.input_hash,
        "data_equivalence_hash": data_equivalence_hash,
    }


def _shadow_evidence_document(facts: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: facts.get(key)
        for key in (
            "agent_id",
            "observation_id",
            "legacy_schema_hash",
            "capability_schema_hash",
            "legacy_result_hash",
            "capability_result_hash",
            "legacy_row_count",
            "capability_row_count",
            "rows_applicable",
            "capability_complete",
            "fallback_used",
            "source_message_id",
            "source_legacy_tool_result_id",
            "capability_invocation_id",
            "linkage_hash",
            "invocation_input_hash",
            "data_equivalence_hash",
        )
    }


def execute_server_shadow_validation(
    db: Session,
    agent_id: str,
    *,
    source_message_id: str,
    legacy_tool_result_id: str,
    capability_kind: Literal["function", "action", "workflow"],
    capability_key: str,
    inputs: Mapping[str, Any],
    managed_inputs: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Re-execute one persisted shadow input and derive comparison facts.

    The caller supplies only server resource identifiers and original runtime
    inputs. Raw legacy and capability results exist only in this stack frame;
    the migration ledger receives hashes, generic top-level row counts and
    governed references.
    """

    agent = _require_agent(db, agent_id)
    if str(agent.runtime_binding_mode or "legacy") != "shadow":
        raise AgentMigrationError(
            "shadow_mode_required",
            "Server shadow validation requires the Agent to be in shadow mode",
        )
    message = _require_shadow_source_message(db, agent, source_message_id)
    if message.role != "assistant" or not bool(message.stream_finalized):
        raise AgentMigrationError(
            "shadow_source_not_finalized",
            "Source message must be a finalized assistant response",
        )
    runtime_snapshot = (message.input_snapshot or {}).get("runtime") or {}
    shadow_snapshot = (
        runtime_snapshot.get("shadow")
        if isinstance(runtime_snapshot, dict)
        and isinstance(runtime_snapshot.get("shadow"), dict)
        else {}
    )
    fallback_snapshot = (
        runtime_snapshot.get("fallback")
        if isinstance(runtime_snapshot, dict)
        and isinstance(runtime_snapshot.get("fallback"), dict)
        else {}
    )
    if (
        not isinstance(runtime_snapshot, dict)
        or runtime_snapshot.get("selected_path") != "legacy"
        or shadow_snapshot.get("executed_path") != "legacy"
        or bool(fallback_snapshot.get("used", False))
    ):
        raise AgentMigrationError(
            "shadow_source_invalid",
            "Source message is not a finalized legacy-path shadow execution",
        )
    llm = (
        tenant_service.get_visible(db, LLMConfig, agent.llm_config_id)
        if agent.llm_config_id
        else LLMConfig(name="Server shadow validation")
    )
    if llm is None:
        raise AgentMigrationError(
            "agent_runtime_not_ready", "Agent validation model is unavailable"
        )
    turn_input = _shadow_turn_input(
        source_message_id=source_message_id,
        legacy_tool_result_id=legacy_tool_result_id,
        capability_kind=capability_kind,
        capability_key=capability_key,
        inputs=inputs,
        managed_inputs=managed_inputs,
    )
    try:
        runtime = agent_runtime_adapter.CapabilityAgentRuntime(
            db,
            agent,
            llm,
            turn_input=turn_input,
        )
    except agent_runtime_adapter.AgentRuntimeAdapterError as exc:
        raise AgentMigrationError(exc.code, exc.message) from None
    _require_matching_shadow_input(message, runtime)
    recorded_capability = _require_matching_shadow_deployment(message, runtime)
    catalog = [
        item
        for item in runtime.public_catalog()
        if (str(item.get("kind") or ""), str(item.get("key") or ""))
        == (capability_kind, capability_key)
    ]
    if len(catalog) != 1:
        raise AgentMigrationError(
            "shadow_capability_unavailable",
            "The requested capability is outside this Agent's governed scope",
            status_code=404,
        )
    capability = catalog[0]
    if bool(capability.get("side_effect")) or bool(
        capability.get("requires_confirmation")
    ):
        raise AgentMigrationError(
            "shadow_side_effect_forbidden",
            "Side-effecting or confirmation-gated capabilities cannot produce shadow gate evidence",
        )
    if (
        not bool((capability.get("readiness") or {}).get("ready", False))
        or not runtime.complete
        or not bool(recorded_capability.get("complete", False))
    ):
        raise AgentMigrationError(
            "shadow_capability_not_ready",
            "The requested capability is not ready for authoritative shadow validation",
        )

    # Fail before invoking the capability unless a trusted, provider-neutral
    # compatibility contract proves the selected legacy result's target and
    # typed inputs.
    _legacy_capability_match(
        db,
        agent,
        llm,
        message,
        legacy_tool_result_id,
    )
    request_digest = str(turn_input.idempotency_key or "").split(":", 1)[-1]
    request = Request(
        capability=CapabilityRef(kind=capability_kind, resource_id=capability_key),
        inputs=turn_input.structured_inputs,
        binding_overrides=turn_input.binding_overrides,
        mode="execute",
        idempotency_key=turn_input.idempotency_key,
        correlation_id=f"agent-shadow:{request_digest}",
        expected_definition_hash=runtime.deployment.definition_hash,
        expected_deployment_fingerprint=runtime.deployment.fingerprint,
        request_id=request_digest,
    )
    previous_audit = db.info.get("action_audit_context")
    db.info["action_audit_context"] = {"agent_id": agent.id}
    try:
        receipt = capability_application_service.invoke(
            db,
            runtime.scenario,
            runtime._actor(),
            request,
            environment=runtime.environment,
            invocation_source="agent",
        )
    except capability_application_service.CapabilityApplicationError as exc:
        raise AgentMigrationError(
            exc.code, exc.message, status_code=exc.status_code
        ) from None
    except (CapabilityInvocationError, CapabilityContractError) as exc:
        raise AgentMigrationError(
            getattr(exc, "code", "shadow_capability_failed"),
            "Capability shadow execution failed",
        ) from None
    finally:
        if previous_audit is None:
            db.info.pop("action_audit_context", None)
        else:
            db.info["action_audit_context"] = previous_audit
    invocation = _require_attributable_invocation(
        agent,
        db.get(CapabilityInvocation, receipt.invocation_id),
        capability_kind=capability_kind,
        capability_key=capability_key,
    )
    request_document = (
        invocation.request_document
        if isinstance(invocation.request_document, dict)
        else {}
    )
    expected_invocation_input_hash = canonical_hash(
        turn_input.structured_inputs,
        domain="capability-structured-input-v1",
    )
    expected_managed_override_hash = canonical_hash(
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
    if (
        str((request_document.get("structured_inputs") or {}).get("hash") or "")
        != expected_invocation_input_hash
        or str(request_document.get("managed_override_hash") or "")
        != expected_managed_override_hash
    ):
        raise AgentMigrationError(
            "shadow_invocation_input_mismatch",
            "Capability invocation does not match the persisted shadow input",
        )
    facts = _derive_authoritative_shadow_facts(
        db,
        agent,
        source_message_id=message.id,
        legacy_tool_result_id=legacy_tool_result_id,
        capability_invocation_id=invocation.id,
    )
    replayed = bool(receipt.audit_ref.get("replayed", False))
    status = record_server_shadow_probe(
        db,
        agent.id,
        ServerShadowProbe(
            observation_id=str(facts["observation_id"]),
            legacy_schema_hash=str(facts["legacy_schema_hash"]),
            capability_schema_hash=str(facts["capability_schema_hash"]),
            legacy_result_hash=str(facts["legacy_result_hash"]),
            capability_result_hash=str(facts["capability_result_hash"]),
            legacy_row_count=facts["legacy_row_count"],
            capability_row_count=facts["capability_row_count"],
            rows_applicable=bool(facts["rows_applicable"]),
            capability_complete=True,
            fallback_used=False,
            source_message_id=message.id,
            source_legacy_tool_result_id=legacy_tool_result_id,
            capability_invocation_id=invocation.id,
        ),
    )
    return {
        "contract": "agent-shadow-validation-receipt/v1",
        "agent_id": agent.id,
        "source_message_id": message.id,
        "observation_id": facts["observation_id"],
        "capability_invocation_id": invocation.id,
        "replayed": replayed,
        "runtime_binding_mode": status["runtime_binding_mode"],
        "gate": status["gate"],
        "run_id": status["run_id"],
    }


def record_server_shadow_probe(
    db: Session,
    agent_id: str,
    probe: ServerShadowProbe,
) -> dict[str, Any]:
    """Record diagnostic facts, promoting only independently verified lineage.

    Callers cannot make a probe gate-eligible by supplying hashes. Eligibility
    requires source Message and succeeded Invocation identifiers whose raw
    server records reproduce every submitted summary.
    """

    agent = _require_agent(db, agent_id)
    if str(agent.runtime_binding_mode or "legacy") != "shadow":
        raise AgentMigrationError(
            "shadow_mode_required", "Server shadow probes require the Agent to be in shadow mode"
        )
    if probe.source_message_id:
        _require_shadow_source_message(db, agent, probe.source_message_id)
    run = _load_or_create_run(db)
    submitted_facts: dict[str, Any] = {
        "agent_id": agent.id,
        "observation_id": probe.observation_id,
        "legacy_schema_hash": probe.legacy_schema_hash,
        "capability_schema_hash": probe.capability_schema_hash,
        "legacy_result_hash": probe.legacy_result_hash,
        "capability_result_hash": probe.capability_result_hash,
        "legacy_row_count": probe.legacy_row_count,
        "capability_row_count": probe.capability_row_count,
        "rows_applicable": probe.rows_applicable,
        "capability_complete": probe.capability_complete,
        "fallback_used": probe.fallback_used,
        "source_message_id": probe.source_message_id,
        "source_legacy_tool_result_id": probe.source_legacy_tool_result_id,
        "capability_invocation_id": probe.capability_invocation_id,
        "linkage_hash": None,
        "invocation_input_hash": None,
        "data_equivalence_hash": None,
    }
    authoritative_facts: dict[str, Any] | None = None
    if (
        probe.source_message_id
        and probe.source_legacy_tool_result_id
        and probe.capability_invocation_id
    ):
        try:
            candidate = _derive_authoritative_shadow_facts(
                db,
                agent,
                source_message_id=probe.source_message_id,
                legacy_tool_result_id=probe.source_legacy_tool_result_id,
                capability_invocation_id=probe.capability_invocation_id,
            )
        except (AgentMigrationError, CapabilityContractError):
            candidate = None
        if candidate is not None and all(
            candidate.get(key) == submitted_facts.get(key)
            for key in (
                "agent_id",
                "observation_id",
                "legacy_schema_hash",
                "capability_schema_hash",
                "legacy_result_hash",
                "capability_result_hash",
                "legacy_row_count",
                "capability_row_count",
                "rows_applicable",
                "capability_complete",
                "fallback_used",
                "source_message_id",
                "source_legacy_tool_result_id",
                "capability_invocation_id",
            )
        ):
            authoritative_facts = candidate
    facts = authoritative_facts or submitted_facts
    gate_eligible = authoritative_facts is not None
    row_delta = None
    if bool(facts["rows_applicable"]):
        row_delta = int(facts["capability_row_count"] or 0) - int(
            facts["legacy_row_count"] or 0
        )
    evidence = _shadow_evidence_document(facts)
    evidence_hash = canonical_hash(evidence, domain="server-shadow-probe-v1")
    item_key = f"{agent.id}:{probe.observation_id}"
    existing = db.get(
        PlatformMigrationCheckpoint,
        {"run_id": run.id, "stage": _SHADOW_STAGE, "item_key": item_key},
    )
    if existing is not None:
        if str((existing.payload or {}).get("evidence_hash") or "") != evidence_hash:
            raise AgentMigrationError(
                "migration_idempotency_conflict",
                "The shadow observation id was already used for different evidence",
            )
        return agent_migration_status(db, agent.id, _agent=agent, _run=run)
    payload = {
        "contract": SHADOW_CONTRACT,
        "agent_id": agent.id,
        "observation_id": facts["observation_id"],
        "source": {
            "kind": (
                "server_shadow_validation"
                if gate_eligible
                else "server_shadow_diagnostic"
            ),
            "message_id": facts["source_message_id"],
            "legacy_tool_result_id": facts["source_legacy_tool_result_id"],
            "capability_invocation_id": facts["capability_invocation_id"],
            "linkage_hash": facts["linkage_hash"],
            "invocation_input_hash": facts["invocation_input_hash"],
            "data_equivalence_hash": facts["data_equivalence_hash"],
        },
        "gate_eligible": gate_eligible,
        "evidence_hash": evidence_hash,
        "schema": {
            "comparable": True,
            "legacy_hash": facts["legacy_schema_hash"],
            "capability_hash": facts["capability_schema_hash"],
            "equal": facts["legacy_schema_hash"] == facts["capability_schema_hash"],
        },
        "rows": {
            "comparable": True,
            "applicable": facts["rows_applicable"],
            "legacy_count": facts["legacy_row_count"],
            "capability_count": facts["capability_row_count"],
            "delta": row_delta,
            "within_tolerance": (
                True
                if not facts["rows_applicable"]
                else abs(int(row_delta or 0)) <= MAX_GATE_ROW_DELTA
            ),
        },
        "result": {
            "comparable": True,
            "legacy_hash": facts["legacy_result_hash"],
            "capability_hash": facts["capability_result_hash"],
            "equal": facts["legacy_result_hash"] == facts["capability_result_hash"],
        },
        "capability_complete": bool(facts["capability_complete"]),
        "fallback_used": bool(facts["fallback_used"]),
        "recorded_at": _now().isoformat(),
    }
    _checkpoint(
        db,
        run,
        stage=_SHADOW_STAGE,
        item_key=item_key,
        payload=payload,
    )
    run.updated_at = _now()
    db.flush()
    return agent_migration_status(db, agent.id, _agent=agent, _run=run)


def _shadow_rows(
    db: Session, run: PlatformMigrationRun, agent_id: str
) -> list[PlatformMigrationCheckpoint]:
    rows = list(
        db.scalars(
            select(PlatformMigrationCheckpoint)
            .where(
                PlatformMigrationCheckpoint.run_id == run.id,
                PlatformMigrationCheckpoint.stage == _SHADOW_STAGE,
            )
            .order_by(
                PlatformMigrationCheckpoint.completed_at.desc(),
                PlatformMigrationCheckpoint.item_key.desc(),
            )
        ).all()
    )
    return [
        row
        for row in rows
        if str((row.payload or {}).get("agent_id") or "") == agent_id
    ]


def _verified_gate_checkpoint(
    db: Session,
    agent: Agent,
    checkpoint: PlatformMigrationCheckpoint,
) -> bool:
    payload = checkpoint.payload or {}
    if not bool(payload.get("gate_eligible")):
        return False
    source = payload.get("source") or {}
    if not isinstance(source, Mapping) or source.get("kind") != "server_shadow_validation":
        return False
    try:
        facts = _derive_authoritative_shadow_facts(
            db,
            agent,
            source_message_id=str(source.get("message_id") or ""),
            legacy_tool_result_id=str(source.get("legacy_tool_result_id") or ""),
            capability_invocation_id=str(source.get("capability_invocation_id") or ""),
        )
    except (AgentMigrationError, CapabilityContractError):
        return False
    schema = payload.get("schema") or {}
    rows = payload.get("rows") or {}
    result = payload.get("result") or {}
    if not all(isinstance(item, Mapping) for item in (schema, rows, result)):
        return False
    row_delta = (
        int(facts["capability_row_count"] or 0)
        - int(facts["legacy_row_count"] or 0)
        if facts["rows_applicable"]
        else None
    )
    expected_evidence_hash = canonical_hash(
        _shadow_evidence_document(facts),
        domain="server-shadow-probe-v1",
    )
    expected = {
        "agent_id": agent.id,
        "observation_id": facts["observation_id"],
        "evidence_hash": expected_evidence_hash,
        "schema": {
            "comparable": True,
            "legacy_hash": facts["legacy_schema_hash"],
            "capability_hash": facts["capability_schema_hash"],
            "equal": facts["legacy_schema_hash"] == facts["capability_schema_hash"],
        },
        "rows": {
            "comparable": True,
            "applicable": facts["rows_applicable"],
            "legacy_count": facts["legacy_row_count"],
            "capability_count": facts["capability_row_count"],
            "delta": row_delta,
            "within_tolerance": (
                True
                if not facts["rows_applicable"]
                else abs(int(row_delta or 0)) <= MAX_GATE_ROW_DELTA
            ),
        },
        "result": {
            "comparable": True,
            "legacy_hash": facts["legacy_result_hash"],
            "capability_hash": facts["capability_result_hash"],
            "equal": facts["legacy_result_hash"] == facts["capability_result_hash"],
        },
        "capability_complete": True,
        "fallback_used": False,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        return False
    return (
        str(source.get("linkage_hash") or "") == facts["linkage_hash"]
        and str(source.get("invocation_input_hash") or "")
        == facts["invocation_input_hash"]
        and str(source.get("data_equivalence_hash") or "")
        == facts["data_equivalence_hash"]
    )


def evaluate_migration_gate(
    db: Session,
    agent_id: str,
    *,
    _agent: Agent | None = None,
    _run: PlatformMigrationRun | None = None,
) -> dict[str, Any]:
    agent = _agent or _require_agent(db, agent_id)
    run = _run or _load_or_create_run(db)
    rows = _shadow_rows(db, run, agent.id)
    claimed = [row for row in rows if bool((row.payload or {}).get("gate_eligible"))]
    eligible = [row for row in claimed if _verified_gate_checkpoint(db, agent, row)]
    window = eligible[:MIN_GATE_OBSERVATIONS]
    reasons: list[dict[str, Any]] = []
    for checkpoint in claimed:
        if checkpoint not in eligible:
            reasons.append(
                {
                    "code": "shadow_evidence_unverified",
                    "observation_id": str(
                        (checkpoint.payload or {}).get("observation_id")
                        or checkpoint.item_key
                    ),
                }
            )
    if len(window) < MIN_GATE_OBSERVATIONS:
        reasons.append(
            {
                "code": "insufficient_shadow_observations",
                "required": MIN_GATE_OBSERVATIONS,
                "observed": len(window),
            }
        )
    for checkpoint in window:
        payload = checkpoint.payload or {}
        observation_id = str(payload.get("observation_id") or checkpoint.item_key)
        schema = payload.get("schema") or {}
        row_metric = payload.get("rows") or {}
        result = payload.get("result") or {}
        if not bool(schema.get("comparable")) or not bool(schema.get("equal")):
            reasons.append({"code": "shadow_schema_mismatch", "observation_id": observation_id})
        if not bool(row_metric.get("comparable")) or not bool(
            row_metric.get("within_tolerance")
        ):
            reasons.append({"code": "shadow_row_mismatch", "observation_id": observation_id})
        if not bool(result.get("comparable")) or not bool(result.get("equal")):
            reasons.append({"code": "shadow_result_mismatch", "observation_id": observation_id})
        if not bool(payload.get("capability_complete")):
            reasons.append({"code": "capability_context_incomplete", "observation_id": observation_id})
        if bool(payload.get("fallback_used")):
            reasons.append({"code": "shadow_fallback_observed", "observation_id": observation_id})
    readiness = agent_readiness_service.compute_agent_readiness(db, agent)
    if not bool(readiness.get("runtime_ready", False)):
        reasons.append({"code": "agent_runtime_not_ready"})
    unique_reasons: list[dict[str, Any]] = []
    seen: set[str] = set()
    for reason in reasons:
        key = canonical_hash(reason, domain="agent-migration-gate-reason-v1")
        if key not in seen:
            unique_reasons.append(reason)
            seen.add(key)
    return {
        "contract": "agent-migration-gate/v1",
        "passed": not unique_reasons,
        "policy": {
            "minimum_observations": MIN_GATE_OBSERVATIONS,
            "maximum_absolute_row_delta": MAX_GATE_ROW_DELTA,
            "window": "latest_required_count",
        },
        "metrics": {
            "total_observations": len(rows),
            "eligible_observations": len(eligible),
            "evaluated_observations": len(window),
            "schema_matches": sum(
                bool((row.payload or {}).get("schema", {}).get("equal")) for row in window
            ),
            "row_matches": sum(
                bool((row.payload or {}).get("rows", {}).get("within_tolerance"))
                for row in window
            ),
            "result_matches": sum(
                bool((row.payload or {}).get("result", {}).get("equal")) for row in window
            ),
        },
        "reasons": unique_reasons,
    }


def _mode_events(
    db: Session, run: PlatformMigrationRun, agent_id: str
) -> list[dict[str, Any]]:
    rows = list(db.scalars(
        select(PlatformMigrationCheckpoint)
        .where(
            PlatformMigrationCheckpoint.run_id == run.id,
            PlatformMigrationCheckpoint.stage == _MODE_STAGE,
        )
        .order_by(PlatformMigrationCheckpoint.completed_at.desc())
    ).all())
    return [
        dict(row.payload or {})
        for row in rows
        if str((row.payload or {}).get("agent_id") or "") == agent_id
    ][:100]


def agent_migration_status(
    db: Session,
    agent_id: str,
    *,
    _agent: Agent | None = None,
    _run: PlatformMigrationRun | None = None,
) -> dict[str, Any]:
    agent = _agent or _require_agent(db, agent_id)
    run = _run or _load_or_create_run(db)
    gate = evaluate_migration_gate(db, agent.id, _agent=agent, _run=run)
    observations = _shadow_rows(db, run, agent.id)
    diagnostic = [
        dict(row.payload or {})
        for row in observations[:20]
    ]
    return {
        "contract": MIGRATION_CONTRACT,
        "agent_id": agent.id,
        "runtime_binding_mode": str(agent.runtime_binding_mode or "legacy"),
        "gate": gate,
        "shadow_observations": diagnostic,
        "events": _mode_events(db, run, agent.id),
        "run_id": run.id,
    }


def assert_direct_mode_update_allowed(agent: Agent, requested_mode: str | None) -> None:
    """Prevent the generic Agent update endpoint from bypassing migration gates."""

    if requested_mode is None:
        return
    requested = str(requested_mode or "").strip()
    current = str(agent.runtime_binding_mode or "legacy")
    if requested != current:
        raise AgentMigrationError(
            "agent_mode_migration_required",
            "Runtime binding mode must be changed through the migration endpoint",
        )


def _normalized_reason(reason: str) -> str:
    value = str(reason or "").strip()
    if not value:
        raise AgentMigrationError("migration_reason_required", "A migration reason is required")
    if len(value) > 2_000:
        raise AgentMigrationError("migration_reason_too_long", "Migration reason is too long")
    return value


def _event_key(agent_id: str, idempotency_key: str | None) -> tuple[str, str]:
    key = str(idempotency_key or "").strip()
    if key and len(key) > 180:
        raise AgentMigrationError(
            "invalid_idempotency_key", "Migration idempotency key is too long"
        )
    logical_key = key or uuid4().hex
    digest = hashlib.sha256(logical_key.encode()).hexdigest()
    return f"{agent_id}:{digest}", logical_key


def _existing_event(
    db: Session, run: PlatformMigrationRun, item_key: str
) -> PlatformMigrationCheckpoint | None:
    return db.get(
        PlatformMigrationCheckpoint,
        {"run_id": run.id, "stage": _MODE_STAGE, "item_key": item_key},
    )


def change_agent_mode(
    db: Session,
    agent_id: str,
    *,
    target_mode: Literal["shadow", "prefer_capability", "capability_only"],
    reason: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    agent = _require_agent(db, agent_id)
    run = _load_or_create_run(db)
    target = str(target_mode or "").strip()
    if target not in _MODES or target == "legacy":
        raise AgentMigrationError("invalid_target_mode", "Migration target mode is invalid")
    normalized_reason = _normalized_reason(reason)
    item_key, logical_key = _event_key(agent.id, idempotency_key)
    request_hash = canonical_hash(
        {"agent_id": agent.id, "target_mode": target, "reason": normalized_reason},
        domain="agent-mode-request-v1",
    )
    existing = _existing_event(db, run, item_key)
    if existing is not None:
        if str((existing.payload or {}).get("request_hash") or "") != request_hash:
            raise AgentMigrationError(
                "migration_idempotency_conflict",
                "The migration idempotency key was already used for another request",
            )
        return agent_migration_status(db, agent.id, _agent=agent, _run=run)
    current = str(agent.runtime_binding_mode or "legacy")
    allowed_next = {
        "legacy": "shadow",
        "shadow": "prefer_capability",
        "prefer_capability": "capability_only",
    }
    if current == target:
        outcome = "no_change"
        gate = evaluate_migration_gate(db, agent.id, _agent=agent, _run=run)
    else:
        if allowed_next.get(current) != target:
            raise AgentMigrationError(
                "invalid_mode_transition",
                f"Agent mode must follow {' -> '.join(_MODES)}",
            )
        gate = evaluate_migration_gate(db, agent.id, _agent=agent, _run=run)
        if target in {"prefer_capability", "capability_only"} and not gate["passed"]:
            raise AgentMigrationError(
                "migration_gate_failed",
                "Agent has not passed the server-authoritative shadow migration gate",
            )
        agent.runtime_binding_mode = target
        outcome = "changed"
        db.flush()
    payload = {
        "contract": "agent-mode-event/v1",
        "agent_id": agent.id,
        "event": "mode_change",
        "from_mode": current,
        "to_mode": target,
        "outcome": outcome,
        "reason": normalized_reason,
        "actor_id": str(db.info.get("user_id") or ""),
        "request_hash": request_hash,
        "idempotency_key_hash": hashlib.sha256(logical_key.encode()).hexdigest(),
        "gate": gate,
        "recorded_at": _now().isoformat(),
    }
    _checkpoint(db, run, stage=_MODE_STAGE, item_key=item_key, payload=payload)
    run.updated_at = _now()
    db.flush()
    return agent_migration_status(db, agent.id, _agent=agent, _run=run)


def rollback_agent_to_legacy(
    db: Session,
    agent_id: str,
    *,
    reason: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    agent = _require_agent(db, agent_id)
    run = _load_or_create_run(db)
    normalized_reason = _normalized_reason(reason)
    item_key, logical_key = _event_key(agent.id, idempotency_key)
    request_hash = canonical_hash(
        {"agent_id": agent.id, "target_mode": "legacy", "reason": normalized_reason},
        domain="agent-mode-request-v1",
    )
    existing = _existing_event(db, run, item_key)
    if existing is not None:
        if str((existing.payload or {}).get("request_hash") or "") != request_hash:
            raise AgentMigrationError(
                "migration_idempotency_conflict",
                "The migration idempotency key was already used for another request",
            )
        return agent_migration_status(db, agent.id, _agent=agent, _run=run)
    current = str(agent.runtime_binding_mode or "legacy")
    agent.runtime_binding_mode = "legacy"
    db.flush()
    payload = {
        "contract": "agent-mode-event/v1",
        "agent_id": agent.id,
        "event": "rollback",
        "from_mode": current,
        "to_mode": "legacy",
        "outcome": "no_change" if current == "legacy" else "changed",
        "reason": normalized_reason,
        "actor_id": str(db.info.get("user_id") or ""),
        "request_hash": request_hash,
        "idempotency_key_hash": hashlib.sha256(logical_key.encode()).hexdigest(),
        "recorded_at": _now().isoformat(),
    }
    _checkpoint(db, run, stage=_MODE_STAGE, item_key=item_key, payload=payload)
    run.updated_at = _now()
    db.flush()
    return agent_migration_status(db, agent.id, _agent=agent, _run=run)


__all__ = [
    "AgentMigrationError",
    "MIGRATION_CONTRACT",
    "ServerShadowProbe",
    "agent_migration_status",
    "assert_direct_mode_update_allowed",
    "change_agent_mode",
    "execute_server_shadow_validation",
    "evaluate_migration_gate",
    "record_server_shadow_probe",
    "refresh_shadow_observations",
    "rollback_agent_to_legacy",
]
