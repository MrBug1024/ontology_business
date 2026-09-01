"""Server-authoritative, one-way Agent capability cutover.

Historical ``legacy``/``shadow``/``prefer_capability`` values and their old
shadow checkpoints remain readable audit facts. They are never executed and
no product path can create new shadow evidence. A migration request can only
move an Agent directly to ``capability_only`` after the current capability
definition, validation, release, and runtime axes pass server-side readiness.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    Agent,
    BusinessScenario,
    PlatformMigrationCheckpoint,
    PlatformMigrationRun,
)
from . import agent_readiness_service, permission_service, tenant_service
from .capability_contracts import canonical_hash, canonical_json


MIGRATION_CONTRACT = "agent-capability-migration/v2"
_MODES = ("legacy", "shadow", "prefer_capability", "capability_only")
_TARGET_MODE = "capability_only"
_TARGET_ENVIRONMENT = "dev"
_READINESS_AXES = ("definition", "validation", "release", "runtime")
_PLAN_DIGEST = canonical_hash(
    {
        "contract": MIGRATION_CONTRACT,
        "source_modes": list(_MODES[:-1]),
        "target_mode": _TARGET_MODE,
        "environment": _TARGET_ENVIRONMENT,
        "required_readiness_axes": list(_READINESS_AXES),
    },
    domain="agent-capability-migration-plan-v2",
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
    tenant_id = _tenant(db)
    agent = db.execute(
        select(Agent)
        .where(Agent.id == agent_id, Agent.tenant_id == tenant_id)
        .with_for_update()
    ).scalar_one_or_none()
    if agent is None:
        raise AgentMigrationError(
            "agent_not_found", "Agent does not exist", status_code=404
        )
    if agent.scenario_id:
        scenario = tenant_service.require_scenario(
            db, agent.scenario_id, writable=manage
        )
        permission_service.require_scenario_permission(
            db, scenario, "write" if manage else "read"
        )
        # Serialize the readiness decision with scenario-scoped definition and
        # deployment writers that follow the same ownership lock.
        db.execute(
            select(BusinessScenario.id)
            .where(
                BusinessScenario.id == scenario.id,
                BusinessScenario.tenant_id == tenant_id,
            )
            .with_for_update()
        ).scalar_one()
    return agent


def _new_run(tenant_id: str, name: str) -> PlatformMigrationRun:
    clock = _now()
    return PlatformMigrationRun(
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
            "cutover": {
                "target_mode": _TARGET_MODE,
                "environment": _TARGET_ENVIRONMENT,
                "required_readiness_axes": list(_READINESS_AXES),
            },
        },
        started_at=clock,
        updated_at=clock,
        completed_at=None,
        last_error="",
    )


def _run_query(db: Session, *, lock: bool = False):
    statement = select(PlatformMigrationRun).where(
        PlatformMigrationRun.migration_name == _migration_name(_tenant(db)),
        PlatformMigrationRun.plan_digest == _PLAN_DIGEST,
    )
    return statement.with_for_update() if lock else statement


def _validate_run_tenant(run: PlatformMigrationRun, tenant_id: str) -> None:
    if str((run.manifest or {}).get("tenant_id") or "") != tenant_id:
        raise AgentMigrationError(
            "migration_tenant_mismatch",
            "Agent migration ledger does not belong to this tenant",
            status_code=403,
        )


def _load_or_create_run(db: Session) -> PlatformMigrationRun:
    tenant_id = _tenant(db)
    run = db.execute(_run_query(db, lock=True)).scalar_one_or_none()
    if run is not None:
        _validate_run_tenant(run, tenant_id)
        return run

    candidate = _new_run(tenant_id, _migration_name(tenant_id))
    try:
        # Different Agents in one tenant can race to create the shared ledger.
        # A savepoint contains the unique-plan conflict without aborting the
        # request transaction; the winner is then loaded and validated.
        with db.begin_nested():
            db.add(candidate)
            db.flush()
        return candidate
    except IntegrityError:
        run = db.execute(_run_query(db, lock=True)).scalar_one_or_none()
        if run is None:
            raise AgentMigrationError(
                "migration_ledger_conflict",
                "Agent migration ledger could not be resolved",
            ) from None
        _validate_run_tenant(run, tenant_id)
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


def _tenant_runs(db: Session) -> list[PlatformMigrationRun]:
    tenant_id = _tenant(db)
    runs = list(
        db.scalars(
            select(PlatformMigrationRun)
            .where(
                PlatformMigrationRun.migration_name == _migration_name(tenant_id)
            )
            .order_by(
                PlatformMigrationRun.started_at.desc(), PlatformMigrationRun.id.desc()
            )
        ).all()
    )
    return [
        run
        for run in runs
        if str((run.manifest or {}).get("tenant_id") or "") == tenant_id
    ]


def _historical_shadow_rows(
    db: Session, agent_id: str
) -> list[PlatformMigrationCheckpoint]:
    run_ids = [run.id for run in _tenant_runs(db)]
    if not run_ids:
        return []
    rows = list(
        db.scalars(
            select(PlatformMigrationCheckpoint)
            .where(
                PlatformMigrationCheckpoint.run_id.in_(run_ids),
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
    ][:20]


def _mode_events(db: Session, agent_id: str) -> list[dict[str, Any]]:
    run_ids = [run.id for run in _tenant_runs(db)]
    if not run_ids:
        return []
    rows = list(
        db.scalars(
            select(PlatformMigrationCheckpoint)
            .where(
                PlatformMigrationCheckpoint.run_id.in_(run_ids),
                PlatformMigrationCheckpoint.stage == _MODE_STAGE,
            )
            .order_by(
                PlatformMigrationCheckpoint.completed_at.desc(),
                PlatformMigrationCheckpoint.item_key.desc(),
            )
        ).all()
    )
    return [
        dict(row.payload or {})
        for row in rows
        if str((row.payload or {}).get("agent_id") or "") == agent_id
    ][:100]


def _canonical_document(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(canonical_json(value))


def evaluate_migration_gate(
    db: Session,
    agent_id: str,
    *,
    _agent: Agent | None = None,
    _run: PlatformMigrationRun | None = None,
) -> dict[str, Any]:
    """Evaluate the target capability runtime without constructing legacy state."""

    del _run
    agent = _agent or _require_agent(db, agent_id)
    readiness = _canonical_document(
        agent_readiness_service.compute_agent_readiness(
            db,
            agent,
            environment=_TARGET_ENVIRONMENT,
            runtime_binding_mode=_TARGET_MODE,
        )
    )
    reasons: list[dict[str, Any]] = []
    for axis in _READINESS_AXES:
        axis_document = readiness.get(axis) or {}
        if bool(axis_document.get("ready", False)):
            continue
        missing = axis_document.get("missing") or []
        if missing:
            reasons.extend(
                {"axis": axis, **dict(issue)}
                for issue in missing
                if isinstance(issue, dict)
            )
        else:
            reasons.append(
                {
                    "axis": axis,
                    "code": "readiness_axis_incomplete",
                    "blocking": True,
                }
            )
    fingerprint = canonical_hash(
        {"environment": _TARGET_ENVIRONMENT, "readiness": readiness},
        domain="agent-capability-cutover-readiness-v1",
    )
    return {
        "contract": "agent-capability-cutover-gate/v1",
        "passed": not reasons,
        "target_mode": _TARGET_MODE,
        "environment": _TARGET_ENVIRONMENT,
        "required_axes": list(_READINESS_AXES),
        "readiness": readiness,
        "readiness_fingerprint": fingerprint,
        "reasons": reasons,
    }


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
    runs = _tenant_runs(db)
    return {
        "contract": MIGRATION_CONTRACT,
        "agent_id": agent.id,
        "runtime_binding_mode": str(agent.runtime_binding_mode or "legacy"),
        "gate": gate,
        # Old shadow evidence is retained only as an immutable diagnostic. No
        # route or exported service function can append to this stage.
        "shadow_observations": [
            dict(row.payload or {})
            for row in _historical_shadow_rows(db, agent.id)
        ],
        "shadow_observations_read_only": True,
        "events": _mode_events(db, agent.id),
        "run_id": run.id,
        "ledger_runs": [
            {
                "run_id": candidate.id,
                "contract": str((candidate.manifest or {}).get("contract") or ""),
                "plan_digest": candidate.plan_digest,
            }
            for candidate in runs
        ],
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
        raise AgentMigrationError(
            "migration_reason_required", "A migration reason is required"
        )
    if len(value) > 2_000:
        raise AgentMigrationError(
            "migration_reason_too_long", "Migration reason is too long"
        )
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
    target_mode: Literal["capability_only"] | str,
    reason: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    agent = _require_agent(db, agent_id)
    target = str(target_mode or "").strip()
    if target != _TARGET_MODE:
        raise AgentMigrationError(
            "invalid_target_mode", "Migration target mode must be capability_only"
        )
    current = str(agent.runtime_binding_mode or "legacy").strip()
    if current not in _MODES:
        raise AgentMigrationError(
            "invalid_current_mode", "Agent runtime binding mode is invalid"
        )
    normalized_reason = _normalized_reason(reason)
    item_key, logical_key = _event_key(agent.id, idempotency_key)
    run = _load_or_create_run(db)
    request_hash = canonical_hash(
        {
            "agent_id": agent.id,
            "target_mode": target,
            "reason": normalized_reason,
        },
        domain="agent-mode-request-v2",
    )
    existing = _existing_event(db, run, item_key)
    if existing is not None:
        if str((existing.payload or {}).get("request_hash") or "") != request_hash:
            raise AgentMigrationError(
                "migration_idempotency_conflict",
                "The migration idempotency key was already used for another request",
            )
        return agent_migration_status(db, agent.id, _agent=agent, _run=run)

    gate = evaluate_migration_gate(db, agent.id, _agent=agent, _run=run)
    if current != target and not bool(gate["passed"]):
        raise AgentMigrationError(
            "migration_readiness_failed",
            "Agent capability readiness is incomplete",
        )

    outcome = "no_change" if current == target else "changed"
    if outcome == "changed":
        agent.runtime_binding_mode = target
        db.flush()
    payload = {
        "contract": "agent-capability-cutover-event/v2",
        "agent_id": agent.id,
        "event": "mode_change",
        "from_mode": current,
        "to_mode": target,
        "outcome": outcome,
        "reason": normalized_reason,
        "actor_id": str(db.info.get("user_id") or ""),
        "environment": gate["environment"],
        "readiness": gate["readiness"],
        "readiness_fingerprint": gate["readiness_fingerprint"],
        "request_hash": request_hash,
        "idempotency_key_hash": hashlib.sha256(logical_key.encode()).hexdigest(),
        "gate": gate,
        "recorded_at": _now().isoformat(),
    }
    _checkpoint(db, run, stage=_MODE_STAGE, item_key=item_key, payload=payload)
    run.updated_at = _now()
    db.flush()
    return agent_migration_status(db, agent.id, _agent=agent, _run=run)


__all__ = [
    "AgentMigrationError",
    "MIGRATION_CONTRACT",
    "agent_migration_status",
    "assert_direct_mode_update_allowed",
    "change_agent_mode",
    "evaluate_migration_gate",
]
