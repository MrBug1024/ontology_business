"""P1 运营执行底座：持久化工作流任务、事件投递、重试和人工审批。

实现刻意使用数据库轮询而非内存队列：任务、事件和审批在服务重启后仍然可见，
并且后续可将同一张运行表接入独立 worker / Redis，而不改变 API 契约。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import (
    AssistantAttachment,
    BusinessScenario,
    EventEnvelope,
    OntologyEvent,
    OntologyWorkflow,
    WorkflowApprovalRequest,
    WorkflowRun,
)
from . import (
    capability_readiness_service,
    datasource_service,
    object_deletion_service,
    permission_service,
    runtime_connector_service,
    runtime_definition_service,
)
from .policies import PolicyViolation, validate_action_params


TERMINAL_RUN_STATUSES = {"succeeded", "failed", "timed_out", "rejected", "cancelled"}
DISPATCHABLE_RUN_STATUSES = {"queued", "retry_waiting"}
ACTIVE_RUN_STATUSES = DISPATCHABLE_RUN_STATUSES | {"running", "awaiting_approval"}

# Keep service-side validation aligned with the workflow editor.  The lower bound
# prevents a one-second scheduler loop from turning a bad configuration into an
# unbounded queue, while the upper bound still permits annual housekeeping jobs.
SCHEDULE_INTERVAL_MIN_SECONDS = 5
SCHEDULE_INTERVAL_MAX_SECONDS = 31_536_000
APPROVAL_TIMEOUT_DEFAULT_SECONDS = 86_400
APPROVAL_TIMEOUT_MIN_SECONDS = 60
APPROVAL_TIMEOUT_MAX_SECONDS = 604_800
VALID_APPROVAL_TIMEOUT_POLICIES = {"reject", "timeout"}
AUTOMATION_PRINCIPAL_BLOCK_MARKER = "自动执行主体不可用"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int, label: str) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PolicyViolation(f"{label} 必须是整数") from exc
    if not minimum <= parsed <= maximum:
        raise PolicyViolation(f"{label} 必须在 {minimum} 到 {maximum} 之间")
    return parsed


def runtime_policy(trigger_config: dict[str, Any] | None) -> dict[str, int]:
    """从工作流配置提取经过边界校验的运行策略。"""
    cfg = trigger_config or {}
    return {
        "max_attempts": _bounded_int(
            cfg.get("max_attempts"), default=3, minimum=1, maximum=10, label="最大尝试次数"
        ),
        "timeout_seconds": _bounded_int(
            cfg.get("timeout_seconds"), default=300, minimum=5, maximum=86_400, label="超时秒数"
        ),
        "retry_backoff_seconds": _bounded_int(
            cfg.get("retry_backoff_seconds"), default=5, minimum=1, maximum=3_600, label="重试间隔"
        ),
    }


def validate_trigger_config(trigger_type: str, trigger_config: dict[str, Any] | None) -> None:
    """在工作流保存时阻止不可调度的定时/事件触发配置。"""
    cfg = trigger_config or {}
    runtime_policy(cfg)
    if trigger_type == "scheduled":
        if cfg.get("interval_seconds") in (None, ""):
            raise PolicyViolation("定时工作流必须填写定时间隔（秒）")
        _bounded_int(
            cfg.get("interval_seconds"),
            minimum=SCHEDULE_INTERVAL_MIN_SECONDS,
            maximum=SCHEDULE_INTERVAL_MAX_SECONDS,
            default=SCHEDULE_INTERVAL_MIN_SECONDS,
            label="定时间隔（秒）",
        )
    elif trigger_type == "event" and not (cfg.get("event_id") or cfg.get("event_name")):
        raise PolicyViolation("事件触发工作流必须选择事件")


def validate_approval_nodes(
    nodes: list[dict[str, Any]] | None,
    steps: list[dict[str, Any]] | None,
) -> None:
    """Reject approval definitions that could silently wait forever.

    Legacy definitions that omit these fields receive the explicit safe defaults
    used by the runtime, but malformed values are never coerced into an unlimited
    approval window.
    """
    definitions: list[dict[str, Any]] = []
    for node in nodes or []:
        if node.get("type") == "approval":
            definitions.append(node.get("data") or {})
    for step in steps or []:
        if step.get("type") == "approval":
            definitions.append(step)
    for definition in definitions:
        _bounded_int(
            definition.get("timeout_seconds"),
            default=APPROVAL_TIMEOUT_DEFAULT_SECONDS,
            minimum=APPROVAL_TIMEOUT_MIN_SECONDS,
            maximum=APPROVAL_TIMEOUT_MAX_SECONDS,
            label="审批超时（秒）",
        )
        on_timeout = str(definition.get("on_timeout") or "reject")
        if on_timeout not in VALID_APPROVAL_TIMEOUT_POLICIES:
            raise PolicyViolation("审批超时策略只能是 reject 或 timeout")


def _trigger_event_ids(
    db: Session,
    scenario_id: str,
    trigger_config: dict[str, Any] | None,
) -> set[str]:
    """Resolve an event trigger to canonical event IDs for cycle validation."""
    cfg = trigger_config or {}
    result: set[str] = set()
    event_id = str(cfg.get("event_id") or "")
    if event_id:
        event = db.get(OntologyEvent, event_id)
        if not event or event.scenario_id != scenario_id:
            raise PolicyViolation("事件触发工作流引用的事件不存在或不属于当前场景")
        result.add(event.id)
    event_name = str(cfg.get("event_name") or "")
    if event_name:
        matches = db.execute(
            select(OntologyEvent.id).where(
                OntologyEvent.scenario_id == scenario_id,
                OntologyEvent.name == event_name,
            )
        ).scalars().all()
        if not matches:
            raise PolicyViolation("事件触发工作流引用的事件不存在或不属于当前场景")
        result.update(matches)
    return result


def _emitted_event_ids(
    db: Session,
    scenario_id: str,
    nodes: list[dict[str, Any]] | None,
    steps: list[dict[str, Any]] | None,
) -> set[str]:
    result: set[str] = set()
    definitions: list[dict[str, Any]] = []
    for node in nodes or []:
        if node.get("type") == "event":
            definitions.append(node.get("data") or {})
    for step in steps or []:
        if step.get("type") == "event":
            definitions.append(step)
    for definition in definitions:
        event_id = str(definition.get("event_id") or "")
        if not event_id:
            continue
        event = db.get(OntologyEvent, event_id)
        if not event or event.scenario_id != scenario_id:
            raise PolicyViolation("工作流发布的事件不存在或不属于当前场景")
        result.add(event.id)
    return result


def validate_event_feedback_loops(
    db: Session,
    scenario_id: str,
    *,
    trigger_type: str,
    trigger_config: dict[str, Any] | None,
    nodes: list[dict[str, Any]] | None,
    steps: list[dict[str, Any]] | None,
    workflow_id: str | None = None,
) -> None:
    """Prevent an event-triggered workflow graph from introducing a cycle.

    Each event workflow contributes ``trigger event -> emitted event`` edges.
    A cycle would create an autonomous queue even if every individual workflow is
    a valid DAG, so it must be rejected while saving the definition.
    """
    if trigger_type != "event":
        return
    candidate_inputs = _trigger_event_ids(db, scenario_id, trigger_config)
    candidate_outputs = _emitted_event_ids(db, scenario_id, nodes, steps)
    if not candidate_outputs:
        return

    graph: dict[str, set[str]] = {}
    existing = db.execute(
        select(OntologyWorkflow).where(
            OntologyWorkflow.scenario_id == scenario_id,
            OntologyWorkflow.trigger_type == "event",
        )
    ).scalars().all()
    for workflow in existing:
        if workflow.id == workflow_id:
            continue
        inputs = _trigger_event_ids(db, scenario_id, workflow.trigger_config or {})
        outputs = _emitted_event_ids(db, scenario_id, workflow.nodes or [], workflow.steps or [])
        for event_in in inputs:
            graph.setdefault(event_in, set()).update(outputs)
    for event_in in candidate_inputs:
        graph.setdefault(event_in, set()).update(candidate_outputs)

    def reaches(start: str, target: str) -> bool:
        pending = [start]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(graph.get(current, set()) - visited)
        return False

    for event_in in candidate_inputs:
        for event_out in candidate_outputs:
            if reaches(event_out, event_in):
                raise PolicyViolation("事件触发链形成循环，可能导致无限任务入队")


def _is_active(workflow: OntologyWorkflow) -> bool:
    return bool(workflow.enabled) and (workflow.status or "draft") == "active"


def _scoped_dedupe_key(key: str | None, environment: str) -> str | None:
    """Keep durable event/workflow dedupe isolated per deployment environment."""
    if not key:
        return None
    scoped = f"{environment}:{key}"
    if len(scoped) <= 180:
        return scoped
    # Input schemas cap external keys at 180; this fallback preserves a stable
    # scope for internal/legacy callers without relying on a lossy truncation.
    import hashlib

    return f"{environment}:sha256:{hashlib.sha256(key.encode('utf-8')).hexdigest()}"


def _definition_for_workflow(
    db: Session,
    workflow: Any,
    *,
    definition: runtime_definition_service.RuntimeDefinition | None = None,
) -> tuple[runtime_definition_service.RuntimeDefinition, Any]:
    """Resolve a workflow from the deployment definition that owns it."""
    scenario = getattr(workflow, "scenario", None) or db.get(
        BusinessScenario, workflow.scenario_id
    )
    if not scenario:
        raise PolicyViolation("工作流所属场景不存在")
    resolved = definition or runtime_definition_service.resolve_active(
        db,
        scenario,
        environment=runtime_connector_service.runtime_environment(),
    )
    if resolved.scenario.id != scenario.id:
        raise PolicyViolation("运行定义不属于工作流业务场景")
    try:
        frozen_workflow = runtime_definition_service.resolve_resource(
            resolved, "workflow", workflow.id
        )
    except runtime_definition_service.RuntimeDefinitionError as exc:
        raise PolicyViolation("工作流不存在于当前运行定义") from exc
    return resolved, frozen_workflow


def _definition_for_run(
    db: Session,
    run: WorkflowRun,
) -> tuple[runtime_definition_service.RuntimeDefinition, Any]:
    """Resolve the definition fixed at enqueue time for a durable run."""
    try:
        definition = runtime_definition_service.resolve_for_run(db, run)
        workflow = runtime_definition_service.resolve_resource(
            definition, "workflow", run.workflow_id
        )
    except runtime_definition_service.RuntimeDefinitionError as exc:
        raise PolicyViolation(str(exc)) from exc
    return definition, workflow


def _current_principal_user_id(db: Session) -> str | None:
    """Return the authenticated caller when this is an HTTP/request session.

    Worker sessions deliberately have no ``db.info`` identity.  Treat a partial
    or invalid identity as an error instead of silently borrowing another
    member; only a genuinely context-free worker may continue without a
    current caller.
    """
    tenant_id = str(db.info.get("tenant_id") or "").strip()
    user_id = str(db.info.get("user_id") or "").strip()
    if not tenant_id and not user_id:
        return None
    return permission_service.require_principal(db).user_id


def _normalize_creator_id(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _creator_for_enqueue(
    db: Session,
    requested_user_id: str | None,
) -> str | None:
    """Bind a new run to its real caller whenever one exists.

    Public routes already pass the current user explicitly.  Keeping this
    defensive check in the service prevents a request-context caller from
    forging a different ``created_by_user_id`` through a future route or an
    in-process caller.  Background callers can still pass a creator derived
    from durable provenance (for example an event source or prior manual run).
    """
    requested = _normalize_creator_id(requested_user_id)
    current = _current_principal_user_id(db)
    if current:
        if requested and requested != current:
            raise PolicyViolation("任务创建者必须与当前认证主体一致")
        return current
    return requested


def _eligible_workflow_creator(
    db: Session,
    workflow: OntologyWorkflow,
    candidate_user_id: str | None,
) -> str | None:
    """Return a still-authorized persisted actor for an automatic run.

    This intentionally does *not* choose an organization owner.  A scheduled
    or source-less event run may only reuse an actor who previously initiated
    this workflow and who can still execute it under the current ACL.  The
    execution context manager verifies active tenant membership and restores
    ``db.info`` afterwards.
    """
    candidate = _normalize_creator_id(candidate_user_id)
    if not candidate:
        return None
    scenario = workflow.scenario or db.get(BusinessScenario, workflow.scenario_id)
    if not scenario:
        return None
    try:
        with permission_service.execution_principal(
            db,
            scenario,
            requested_user_id=candidate,
        ) as resolved_user_id:
            if permission_service.check_workflow(db, workflow, "execute").allowed:
                return resolved_user_id
    except HTTPException:
        # A removed/disabled member, changed tenant membership, or revoked
        # workflow ACL is not an invitation to upgrade the run to owner.
        return None
    return None


def _automation_creator_for_workflow(
    db: Session,
    workflow: Any,
    *,
    environment: str | None = None,
) -> str | None:
    """Find an explicitly attributable actor for schedule/event automation.

    A live authenticated caller is the strongest provenance.  Worker ticks do
    not have one, so they may reuse only a creator stored on an earlier run of
    the same workflow.  Earlier versions left automatic runs unowned and the
    permission layer then fell back to tenant owner; those legacy rows have no
    creator and therefore cannot bootstrap new automatic work.
    """
    current = _current_principal_user_id(db)
    if current:
        eligible = _eligible_workflow_creator(db, workflow, current)
        if eligible:
            return eligible

    candidates = db.execute(
        select(WorkflowRun.created_by_user_id)
        .where(
            WorkflowRun.workflow_id == workflow.id,
            WorkflowRun.created_by_user_id.is_not(None),
            *(
                [WorkflowRun.environment == environment]
                if environment
                else []
            ),
        )
        .order_by(WorkflowRun.created_at.desc())
    ).scalars().all()
    seen: set[str] = set()
    for candidate in candidates:
        normalized = _normalize_creator_id(candidate)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        eligible = _eligible_workflow_creator(db, workflow, normalized)
        if eligible:
            return eligible
    return None


def _event_creator(
    db: Session,
    event: OntologyEvent,
    *,
    source_run_id: str | None,
    requested_user_id: str | None,
) -> str | None:
    """Resolve event provenance without accepting a cross-scenario spoof."""
    requested = _creator_for_enqueue(db, requested_user_id)
    if requested:
        return requested
    if not source_run_id:
        return None
    source_run = db.get(WorkflowRun, source_run_id)
    if not source_run or source_run.scenario_id != event.scenario_id:
        raise PolicyViolation("事件来源任务不存在或不属于当前场景")
    return _normalize_creator_id(source_run.created_by_user_id)


def _run_principal_error(
    db: Session,
    workflow: Any,
    run: WorkflowRun,
) -> str | None:
    """Validate a queued run before dispatching it under a worker session."""
    creator = _normalize_creator_id(run.created_by_user_id)
    if not creator:
        return (
            f"{AUTOMATION_PRINCIPAL_BLOCK_MARKER}：任务缺少可审计执行主体；"
            "自动任务不会回退为组织所有者。"
            "请由仍具执行权限的成员手动运行一次后再启用自动触发。"
        )
    scenario = workflow.scenario or db.get(BusinessScenario, run.scenario_id)
    if not scenario:
        return "工作流所属场景不存在"
    try:
        with permission_service.execution_principal(
            db,
            scenario,
            requested_user_id=creator,
        ):
            if not permission_service.check_workflow(db, workflow, "execute").allowed:
                return (
                    f"{AUTOMATION_PRINCIPAL_BLOCK_MARKER}："
                    "任务发起人已不再拥有工作流执行权限；自动任务不会提升为组织所有者"
                )
    except HTTPException as exc:
        return (
            f"{AUTOMATION_PRINCIPAL_BLOCK_MARKER}："
            f"任务发起人不可用；自动任务不会提升为组织所有者：{exc.detail}"
        )
    return None


def _has_unresolved_automation_principal_block(
    db: Session,
    workflow: Any,
    *,
    environment: str | None = None,
) -> bool:
    """Avoid creating one failed audit row per scheduling interval.

    A later manual run can establish a valid creator and immediately unblocks
    future automatic runs because callers only use this guard when no creator
    was resolved.
    """
    return bool(
        db.execute(
            select(WorkflowRun.id)
            .where(
                WorkflowRun.workflow_id == workflow.id,
                WorkflowRun.status == "failed",
                WorkflowRun.error.like(f"{AUTOMATION_PRINCIPAL_BLOCK_MARKER}%"),
                *(
                    [WorkflowRun.environment == environment]
                    if environment
                    else []
                ),
            )
            .limit(1)
        ).scalar_one_or_none()
    )


def enqueue_workflow_run(
    db: Session,
    workflow: Any,
    params: dict[str, Any] | None = None,
    *,
    trigger_source: str = "manual",
    event_envelope_id: str | None = None,
    dedupe_key: str | None = None,
    scheduled_for: datetime | None = None,
    created_by_user_id: str | None = None,
    available_at: datetime | None = None,
    runtime_definition: runtime_definition_service.RuntimeDefinition | None = None,
) -> tuple[WorkflowRun, bool]:
    """把运行请求持久化到队列。返回 (run, 是否新建)。"""
    definition, workflow = _definition_for_workflow(
        db, workflow, definition=runtime_definition
    )
    capability_readiness_service.require_executable(
        "workflow", workflow, definition=definition, db=db
    )
    if trigger_source in {"manual", "retry"}:
        decision = permission_service.check_workflow(db, workflow, "execute")
        if not decision.allowed:
            raise PolicyViolation("没有提交该工作流的权限")
    policy = runtime_policy(workflow.trigger_config or {})
    scoped_dedupe_key = _scoped_dedupe_key(dedupe_key, definition.environment)
    if scoped_dedupe_key:
        existing = db.execute(
            select(WorkflowRun)
            .where(
                WorkflowRun.workflow_id == workflow.id,
                WorkflowRun.dedupe_key == scoped_dedupe_key,
            )
            .order_by(WorkflowRun.created_at.desc())
        ).scalars().first()
        if existing:
            return existing, False

    creator_id = _creator_for_enqueue(db, created_by_user_id)
    now = utc_now()
    run = WorkflowRun(
        scenario_id=workflow.scenario_id,
        workflow_id=workflow.id,
        trigger_source=trigger_source,
        event_envelope_id=event_envelope_id,
        dedupe_key=scoped_dedupe_key,
        input_params=dict(params or {}),
        environment=definition.environment,
        definition_snapshot_id=definition.snapshot_id,
        release_id=definition.release_id,
        definition_hash=definition.definition_hash,
        definition_source=definition.source,
        status="queued",
        max_attempts=policy["max_attempts"],
        timeout_seconds=policy["timeout_seconds"],
        available_at=available_at or now,
        scheduled_for=scheduled_for,
        created_by_user_id=creator_id,
    )
    db.add(run)
    db.flush()
    # The primary key is now available.  Retain it for every automatic retry;
    # explicit user retries mint a new key in ``retry_run``.
    run.execution_key = run.id
    db.flush()
    return run, True


def publish_event(
    db: Session,
    event: Any,
    payload: dict[str, Any] | None = None,
    *,
    source: str = "manual",
    source_run_id: str | None = None,
    dedupe_key: str | None = None,
    created_by_user_id: str | None = None,
    runtime_definition: runtime_definition_service.RuntimeDefinition | None = None,
) -> tuple[EventEnvelope, list[WorkflowRun]]:
    """持久化一个事件信封，并把匹配的启用工作流原子地投递到队列。"""
    scenario = getattr(event, "scenario", None) or db.get(BusinessScenario, event.scenario_id)
    if not scenario:
        raise PolicyViolation("事件所属业务场景不存在")
    definition = runtime_definition or runtime_definition_service.resolve_active(
        db,
        scenario,
        environment=runtime_connector_service.runtime_environment(),
    )
    if definition.scenario.id != scenario.id:
        raise PolicyViolation("运行定义不属于事件业务场景")
    try:
        event = runtime_definition_service.resolve_resource(definition, "event", event.id)
    except runtime_definition_service.RuntimeDefinitionError as exc:
        raise PolicyViolation("事件不存在于当前运行定义") from exc
    capability_readiness_service.require_executable(
        "event", event, definition=definition, db=db
    )
    creator_id = _event_creator(
        db,
        event,
        source_run_id=source_run_id,
        requested_user_id=created_by_user_id,
    )
    raw_payload = dict(payload or {})
    if event.payload_schema:
        raw_payload = validate_action_params(event.payload_schema, raw_payload)
    scoped_dedupe_key = _scoped_dedupe_key(dedupe_key, definition.environment)
    if scoped_dedupe_key:
        existing = db.execute(
            select(EventEnvelope)
            .where(
                EventEnvelope.event_id == event.id,
                EventEnvelope.dedupe_key == scoped_dedupe_key,
            )
            .order_by(EventEnvelope.created_at.desc())
        ).scalars().first()
        if existing:
            existing_runs = db.execute(
                select(WorkflowRun).where(WorkflowRun.event_envelope_id == existing.id)
            ).scalars().all()
            return existing, existing_runs

    # Definition-time validation rejects newly created loops.  This runtime
    # guard also protects legacy/imported definitions and cross-workflow cycles
    # that predate that validation: an event may not reappear in its own causal
    # chain.  We retain a suppressed envelope as audit evidence but deliberately
    # do not create any child runs.
    if source == "workflow" and source_run_id and _event_in_causation_chain(db, source_run_id, event.id):
        envelope = EventEnvelope(
            scenario_id=event.scenario_id,
            event_id=event.id,
            name=event.name,
            payload=raw_payload,
            source="workflow_cycle_suppressed",
            source_run_id=source_run_id,
            dedupe_key=scoped_dedupe_key,
            environment=definition.environment,
            definition_snapshot_id=definition.snapshot_id,
            release_id=definition.release_id,
            definition_hash=definition.definition_hash,
            definition_source=definition.source,
        )
        db.add(envelope)
        db.flush()
        return envelope, []

    envelope = EventEnvelope(
        scenario_id=event.scenario_id,
        event_id=event.id,
        name=event.name,
        payload=raw_payload,
        source=source,
        source_run_id=source_run_id,
        dedupe_key=scoped_dedupe_key,
        environment=definition.environment,
        definition_snapshot_id=definition.snapshot_id,
        release_id=definition.release_id,
        definition_hash=definition.definition_hash,
        definition_source=definition.source,
    )
    db.add(envelope)
    db.flush()

    subscribers = [
        workflow
        for workflow in definition.workflows.values()
        if workflow.scenario_id == event.scenario_id
        and workflow.trigger_type == "event"
        and _is_active(workflow)
    ]
    queued: list[WorkflowRun] = []
    for workflow in subscribers:
        cfg = workflow.trigger_config or {}
        event_id = cfg.get("event_id")
        event_name = cfg.get("event_name")
        if not event_id and not event_name:
            continue
        if event_id and event_id != event.id:
            continue
        if event_name and event_name != event.name:
            continue
        # A direct user event carries its caller.  For source-less service
        # events, reuse a prior, still-authorized run creator for this exact
        # workflow if there is one.  ``None`` is retained as an auditable
        # blocked run and will fail closed before dispatch instead of invoking
        # permission_service's owner fallback.
        workflow_creator = creator_id or _automation_creator_for_workflow(
            db, workflow, environment=definition.environment
        )
        if not workflow_creator and _has_unresolved_automation_principal_block(
            db, workflow, environment=definition.environment
        ):
            continue
        run, created = enqueue_workflow_run(
            db,
            workflow,
            {"event": raw_payload, "event_id": event.id, "event_name": event.name},
            trigger_source="event",
            event_envelope_id=envelope.id,
            dedupe_key=f"event:{envelope.id}",
            created_by_user_id=workflow_creator,
            runtime_definition=definition,
        )
        if created:
            queued.append(run)
    return envelope, queued


def _event_in_causation_chain(db: Session, source_run_id: str, event_id: str) -> bool:
    """Whether ``event_id`` already caused the workflow run publishing it."""
    run_id: str | None = source_run_id
    visited_runs: set[str] = set()
    while run_id and run_id not in visited_runs:
        visited_runs.add(run_id)
        run = db.get(WorkflowRun, run_id)
        if not run or not run.event_envelope_id:
            return False
        envelope = db.get(EventEnvelope, run.event_envelope_id)
        if not envelope:
            return False
        if envelope.event_id == event_id:
            return True
        run_id = envelope.source_run_id
    return False


def enqueue_due_schedules(db: Session, *, now: datetime | None = None) -> list[WorkflowRun]:
    """扫描启用的 interval 定时工作流，并将到期运行写入持久化队列。"""
    now = now or utc_now()
    deployment_environment = runtime_connector_service.runtime_environment()
    definitions = runtime_definition_service.active_definitions(
        db, environment=deployment_environment
    )
    queued: list[WorkflowRun] = []
    for definition in definitions:
        for workflow in definition.workflows.values():
            if workflow.trigger_type != "scheduled" or not _is_active(workflow):
                continue
            interval_value = (workflow.trigger_config or {}).get("interval_seconds")
            if interval_value in (None, ""):
                # 遗留的无效配置不应被每秒自动投递。
                continue
            try:
                interval = _bounded_int(
                    interval_value,
                    minimum=SCHEDULE_INTERVAL_MIN_SECONDS,
                    maximum=SCHEDULE_INTERVAL_MAX_SECONDS,
                    default=SCHEDULE_INTERVAL_MIN_SECONDS,
                    label="定时间隔（秒）",
                )
            except PolicyViolation:
                # 配置在保存时会被拒绝；此分支保护升级前已有的无效遗留数据。
                continue
            previous = db.execute(
                select(WorkflowRun.scheduled_for)
                .where(
                    WorkflowRun.workflow_id == workflow.id,
                    WorkflowRun.environment == definition.environment,
                    WorkflowRun.scheduled_for.is_not(None),
                )
                .order_by(WorkflowRun.scheduled_for.desc())
                .limit(1)
            ).scalar_one_or_none()
            previous = _aware(previous)
            if previous and now < previous + timedelta(seconds=interval):
                continue
            # Never stack schedule runs while an earlier run is queued, retrying,
            # running or waiting for approval.  This avoids concurrent duplicate
            # side effects when a workflow takes longer than its interval.
            overlapping = db.execute(
                select(WorkflowRun.id)
                .where(
                    WorkflowRun.workflow_id == workflow.id,
                    WorkflowRun.environment == definition.environment,
                    WorkflowRun.status.in_(ACTIVE_RUN_STATUSES),
                )
                .limit(1)
            ).scalar_one_or_none()
            if overlapping:
                continue
            scheduled_for = now.replace(microsecond=0)
            # A background worker has no HTTP identity.  It must use a concrete,
            # previously attributable executor rather than letting the execution
            # permission helper choose the tenant owner implicitly.
            creator_id = _automation_creator_for_workflow(
                db, workflow, environment=definition.environment
            )
            if not creator_id and _has_unresolved_automation_principal_block(
                db, workflow, environment=definition.environment
            ):
                continue
            run, created = enqueue_workflow_run(
                db,
                workflow,
                (workflow.trigger_config or {}).get("params") or {},
                trigger_source="scheduled",
                scheduled_for=scheduled_for,
                dedupe_key=f"schedule:{scheduled_for.isoformat()}",
                created_by_user_id=creator_id,
                runtime_definition=definition,
            )
            if created:
                queued.append(run)
    return queued


def _retry_or_finish(
    db: Session,
    run: WorkflowRun,
    *,
    final_status: str,
    error: str,
    now: datetime,
    retryable: bool = True,
) -> None:
    try:
        _, workflow = _definition_for_run(db, run)
    except PolicyViolation:
        workflow = None
    policy = runtime_policy(workflow.trigger_config or {}) if workflow else {
        "retry_backoff_seconds": 5,
    }
    run.error = error
    if retryable and run.attempt < run.max_attempts:
        delay = min(policy["retry_backoff_seconds"] * (2 ** max(0, run.attempt - 1)), 3_600)
        run.status = "retry_waiting"
        run.trigger_source = "retry"
        run.available_at = now + timedelta(seconds=delay)
        run.next_retry_at = run.available_at
        run.completed_at = None
    else:
        run.status = final_status
        run.completed_at = now
        run.next_retry_at = None
    db.commit()


def _ensure_approval_request(db: Session, run: WorkflowRun, waiting_step: dict[str, Any], now: datetime) -> WorkflowApprovalRequest:
    details = waiting_step.get("result") or {}
    # Legacy linear steps use ``step`` for display but persist a distinct
    # ``node_id`` (for example ``step-1``) in the execution result.  Approval
    # resume must use that canonical ID or it will pause forever on the same
    # legacy step after a decision.
    node_id = str(
        details.get("node_id")
        or waiting_step.get("node")
        or waiting_step.get("step")
        or "approval"
    )
    approval = db.execute(
        select(WorkflowApprovalRequest).where(
            WorkflowApprovalRequest.workflow_run_id == run.id,
            WorkflowApprovalRequest.node_id == node_id,
        )
    ).scalars().first()
    if approval:
        return approval
    timeout = details.get("timeout_seconds")
    try:
        timeout_seconds = _bounded_int(
            timeout,
            default=APPROVAL_TIMEOUT_DEFAULT_SECONDS,
            minimum=APPROVAL_TIMEOUT_MIN_SECONDS,
            maximum=APPROVAL_TIMEOUT_MAX_SECONDS,
            label="审批超时（秒）",
        )
    except PolicyViolation:
        # Existing imported data may bypass save-time validation.  Never turn a
        # malformed timeout into an unlimited approval; fall back to one day.
        timeout_seconds = APPROVAL_TIMEOUT_DEFAULT_SECONDS
    approval = WorkflowApprovalRequest(
        workflow_run_id=run.id,
        scenario_id=run.scenario_id,
        node_id=node_id,
        node_name=str(waiting_step.get("name") or "人工审批"),
        instructions=str(details.get("instructions") or "请核对影响范围后决定是否批准。"),
        expires_at=now + timedelta(seconds=timeout_seconds),
    )
    db.add(approval)
    db.flush()
    return approval


def process_available_runs(db: Session, *, now: datetime | None = None, limit: int = 8) -> list[WorkflowRun]:
    """同步处理少量已到期队列项；由 lifespan 中的后台循环调用。"""
    now = now or utc_now()
    deployment_environment = runtime_connector_service.runtime_environment()
    run_ids = db.execute(
        select(WorkflowRun.id)
        .where(
            WorkflowRun.status.in_(DISPATCHABLE_RUN_STATUSES),
            WorkflowRun.available_at <= now,
            # A shared queue may be served by multiple deployment environments.
            # Never claim another environment's work; its matching worker must
            # process it, and legacy runs without an environment are quarantined
            # by the database migration instead of falling back to dev.
            WorkflowRun.environment == deployment_environment,
        )
        .order_by(WorkflowRun.available_at.asc(), WorkflowRun.created_at.asc())
        .limit(max(1, min(limit, 32)))
    ).scalars().all()
    processed: list[WorkflowRun] = []
    for run_id in run_ids:
        run = db.get(WorkflowRun, run_id)
        if (
            not run
            or run.status not in DISPATCHABLE_RUN_STATUSES
            or _aware(run.available_at) > now
            or run.environment != deployment_environment
        ):
            continue
        try:
            definition, workflow = _definition_for_run(db, run)
        except PolicyViolation as exc:
            run.status = "cancelled"
            run.error = str(exc)
            run.completed_at = now
            run.next_retry_at = None
            db.commit()
            processed.append(run)
            continue
        if not _is_active(workflow):
            run.status = "cancelled"
            run.error = "工作流不存在于固定运行定义或当前未启用"
            run.completed_at = now
            db.commit()
            processed.append(run)
            continue

        # ``execution_principal`` historically selected the tenant owner when
        # ``requested_user_id`` was empty.  A worker must never turn a legacy,
        # source-less schedule/event row into an owner-authorized execution.
        # Validate before claiming the run so this is a terminal, auditable
        # governance failure rather than a retryable runtime error.
        principal_error = _run_principal_error(db, workflow, run)
        if principal_error:
            failed = db.execute(
                update(WorkflowRun)
                .where(
                    WorkflowRun.id == run_id,
                    WorkflowRun.status.in_(DISPATCHABLE_RUN_STATUSES),
                )
                .values(
                    status="failed",
                    error=principal_error,
                    completed_at=now,
                    next_retry_at=None,
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            if failed == 1:
                db.commit()
                db.expire_all()
                failed_run = db.get(WorkflowRun, run_id)
                if failed_run:
                    processed.append(failed_run)
            else:
                db.rollback()
            continue

        # 通过条件 UPDATE 原子领取任务，避免多 worker / 多进程看到同一条 queued
        # 记录后重复执行。批准后的恢复属于同一次尝试，前置 Action 使用相同幂等键回放。
        claim_values: dict[str, Any] = {
            "status": "running",
            "started_at": now,
            "next_retry_at": None,
        }
        if run.trigger_source != "approval":
            claim_values["attempt"] = WorkflowRun.attempt + 1
        claimed = db.execute(
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run_id,
                WorkflowRun.status.in_(DISPATCHABLE_RUN_STATUSES),
                WorkflowRun.available_at <= now,
                WorkflowRun.environment == deployment_environment,
            )
            .values(**claim_values)
            # 数据库返回的时间值统一由 ORM 在内存中与 utc_now() 比较；
            # 随后的 expire_all()/get() 会从数据库重新读取已领取的记录。
            .execution_options(synchronize_session=False)
        ).rowcount
        if claimed != 1:
            db.rollback()
            continue
        db.commit()
        db.expire_all()
        run = db.get(WorkflowRun, run_id)
        if not run:
            continue

        try:
            from . import workflow_service

            scenario = definition.scenario
            if not scenario:
                raise PolicyViolation("工作流所属场景不存在")
            with permission_service.execution_principal(
                db,
                scenario,
                requested_user_id=run.created_by_user_id,
            ):
                result = workflow_service.execute_workflow(
                    db,
                    workflow,
                    run.input_params or {},
                    execution_id=run.execution_key or run.id,
                    approved_node_ids=set(run.approved_node_ids or []),
                    attempt=max(1, run.attempt),
                    source_run_id=run.id,
                    deadline_at=(_aware(run.started_at) or now)
                    + timedelta(seconds=run.timeout_seconds),
                    # Assert again inside the execution path.  The resolver
                    # rejects any mismatch instead of treating the persisted
                    # value as a request-controlled environment selector.
                    runtime_environment=runtime_connector_service.runtime_environment(run.environment),
                    runtime_definition=definition,
                )
        except Exception as exc:  # noqa: BLE001
            result = {"status": "failed", "steps": [], "error": str(exc), "duration_ms": 0}

        db.refresh(run)
        # 超时巡检可能已把长时间运行转入重试/终态，不覆盖它的决策。
        if run.status != "running":
            processed.append(run)
            continue
        finished_at = utc_now()
        run.result = result
        elapsed = (finished_at - (_aware(run.started_at) or finished_at)).total_seconds()
        if elapsed > run.timeout_seconds:
            # A synchronous external call may already be in flight when the
            # watchdog fires.  Retrying it automatically can duplicate a side
            # effect, so timeouts are terminal and require explicit operator
            # retry after the external outcome is reconciled.
            _retry_or_finish(
                db,
                run,
                final_status="timed_out",
                error="任务执行超过配置的超时限制",
                now=finished_at,
                retryable=False,
            )
        elif result.get("status") == "awaiting_approval":
            waiting_step = next(
                (step for step in result.get("steps", []) if step.get("status") == "awaiting_approval"),
                {},
            )
            _ensure_approval_request(db, run, waiting_step, finished_at)
            run.status = "awaiting_approval"
            run.error = ""
            run.completed_at = None
            db.commit()
        elif result.get("status") == "success":
            run.status = "succeeded"
            run.error = ""
            run.completed_at = finished_at
            db.commit()
        else:
            _retry_or_finish(
                db,
                run,
                final_status="failed",
                error=str(result.get("error") or "工作流执行失败"),
                now=finished_at,
            )
        processed.append(run)
    return processed


def expire_stale_operations(db: Session, *, now: datetime | None = None) -> None:
    """处理审批过期，以及异常退出后长期占用 running 的任务。"""
    now = now or utc_now()
    approvals = db.execute(
        select(WorkflowApprovalRequest).where(
            WorkflowApprovalRequest.status == "pending",
            WorkflowApprovalRequest.expires_at.is_not(None),
            WorkflowApprovalRequest.expires_at <= now,
        )
    ).scalars().all()
    for approval in approvals:
        run = approval.workflow_run
        _expire_approval(db, approval, run, now)

    running = db.execute(select(WorkflowRun).where(WorkflowRun.status == "running")).scalars().all()
    for run in running:
        started_at = _aware(run.started_at)
        if started_at and now > started_at + timedelta(seconds=run.timeout_seconds):
            _retry_or_finish(
                db,
                run,
                final_status="timed_out",
                error="任务执行超时",
                now=now,
                retryable=False,
            )
    db.commit()


def _approval_timeout_outcome(run: WorkflowRun, node_id: str) -> str:
    """Read the persisted approval node policy from the immutable active run."""
    for step in (run.result or {}).get("steps", []):
        current = str(step.get("node") or step.get("step") or "")
        if current != node_id:
            continue
        on_timeout = str((step.get("result") or {}).get("on_timeout") or "reject")
        return on_timeout if on_timeout in VALID_APPROVAL_TIMEOUT_POLICIES else "reject"
    return "reject"


def _expire_approval(
    db: Session,
    approval: WorkflowApprovalRequest,
    run: WorkflowRun,
    now: datetime,
) -> None:
    """Resolve an approval expiry according to its configured safe policy."""
    outcome = _approval_timeout_outcome(run, approval.node_id)
    claimed = db.execute(
        update(WorkflowApprovalRequest)
        .where(
            WorkflowApprovalRequest.id == approval.id,
            WorkflowApprovalRequest.status == "pending",
        )
        .values(status="expired", resolved_at=now, comment="审批超时")
    ).rowcount
    if claimed != 1:
        return
    db.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run.id, WorkflowRun.status == "awaiting_approval")
        .values(
            status="timed_out" if outcome == "timeout" else "rejected",
            error="审批超时" if outcome == "reject" else "审批超时，任务已标记超时",
            completed_at=now,
            next_retry_at=None,
        )
    )


def decide_approval(
    db: Session,
    run: WorkflowRun,
    *,
    approved: bool,
    comment: str = "",
    user_id: str | None = None,
    now: datetime | None = None,
) -> WorkflowRun:
    principal = permission_service.require_principal(db)
    if user_id and user_id != principal.user_id:
        raise PolicyViolation("审批用户与当前登录主体不一致")
    try:
        _, workflow = _definition_for_run(db, run)
    except PolicyViolation as exc:
        raise PolicyViolation("固定运行定义不可用，不能处理审批") from exc
    if not permission_service.check_workflow(db, workflow, "approve").allowed:
        raise PolicyViolation("没有审批该工作流的权限")
    now = now or utc_now()
    db.refresh(run)
    approval = db.execute(
        select(WorkflowApprovalRequest)
        .where(WorkflowApprovalRequest.workflow_run_id == run.id, WorkflowApprovalRequest.status == "pending")
        .order_by(WorkflowApprovalRequest.requested_at.asc())
        .limit(1)
    ).scalars().first()
    if not approval or run.status != "awaiting_approval":
        raise PolicyViolation("当前任务没有待处理的审批")
    if approval.expires_at and _aware(approval.expires_at) <= now:
        # Conditional claims make an approval decision race safe: a concurrent
        # approver or expiry worker cannot both resolve the same request.
        claimed = db.execute(
            update(WorkflowApprovalRequest)
            .where(
                WorkflowApprovalRequest.id == approval.id,
                WorkflowApprovalRequest.status == "pending",
            )
            .values(status="expired", resolved_at=now, comment="审批超时")
        ).rowcount
        if claimed != 1:
            db.rollback()
            raise PolicyViolation("审批已被其他操作处理")
        outcome = _approval_timeout_outcome(run, approval.node_id)
        run_status = "timed_out" if outcome == "timeout" else "rejected"
        updated = db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run.id, WorkflowRun.status == "awaiting_approval")
            .values(
                status=run_status,
                error="审批超时" if outcome == "reject" else "审批超时，任务已标记超时",
                completed_at=now,
                next_retry_at=None,
            )
        ).rowcount
        if updated != 1:
            db.rollback()
            raise PolicyViolation("审批已被其他操作处理")
        db.commit()
        raise PolicyViolation("审批已超时")

    resolved_status = "approved" if approved else "rejected"
    approver_user_id = user_id or principal.user_id
    claimed = db.execute(
        update(WorkflowApprovalRequest)
        .where(
            WorkflowApprovalRequest.id == approval.id,
            WorkflowApprovalRequest.status == "pending",
        )
        .values(
            status=resolved_status,
            resolved_at=now,
            resolved_by_user_id=approver_user_id,
            comment=comment,
        )
    ).rowcount
    if claimed != 1:
        db.rollback()
        raise PolicyViolation("审批已被其他操作处理")
    if approved:
        approved_nodes = set(run.approved_node_ids or [])
        approved_nodes.add(approval.node_id)
        values = {
            "approved_node_ids": sorted(approved_nodes),
            "status": "queued",
            "trigger_source": "approval",
            "available_at": now,
            "next_retry_at": None,
            "completed_at": None,
            "error": "",
        }
    else:
        values = {
            "status": "rejected",
            "error": comment or "审批被驳回",
            "completed_at": now,
            "next_retry_at": None,
        }
    updated = db.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run.id, WorkflowRun.status == "awaiting_approval")
        .values(**values)
    ).rowcount
    if updated != 1:
        db.rollback()
        raise PolicyViolation("审批已被其他操作处理")
    db.commit()
    db.refresh(run)
    return run


def retry_run(db: Session, run: WorkflowRun, *, now: datetime | None = None) -> WorkflowRun:
    principal = permission_service.require_principal(db)
    try:
        _, workflow = _definition_for_run(db, run)
    except PolicyViolation as exc:
        raise PolicyViolation("固定运行定义不可用，不能重试") from exc
    if not permission_service.check_workflow(db, workflow, "execute").allowed:
        raise PolicyViolation("没有重试该工作流的权限")
    if run.status not in {"failed", "timed_out", "cancelled"}:
        raise PolicyViolation("只有失败、超时或取消的任务可以重新执行")
    now = now or utc_now()
    run.status = "queued"
    run.trigger_source = "retry"
    # An operator retry is a new, explicitly requested business execution.  It
    # must not inherit automatic-retry idempotency records from the old lineage.
    run.execution_key = uuid4().hex
    run.attempt = 0
    run.available_at = now
    run.next_retry_at = None
    run.completed_at = None
    run.error = ""
    run.result = {}
    # A human retry is a fresh business invocation.  Preserve the actual
    # retrier rather than running it later as the (possibly departed) original
    # requester.
    run.created_by_user_id = principal.user_id
    # 手动重试是一轮新的业务运行，要求再次经过审批节点。
    run.approved_node_ids = []
    db.commit()
    db.refresh(run)
    return run


def cancel_run(
    db: Session,
    run: WorkflowRun,
    *,
    comment: str = "",
    now: datetime | None = None,
) -> WorkflowRun:
    """Safely cancel a task before it invokes, or resumes, external work.

    A synchronous ``running`` invocation cannot be honestly cancelled after it
    has crossed an external system boundary, so callers receive a conflict rather
    than a misleading success response.  Queued/retry/approval states are fully
    cancellable and preserve their audit record.
    """
    try:
        _, workflow = _definition_for_run(db, run)
    except PolicyViolation as exc:
        raise PolicyViolation("固定运行定义不可用，不能取消") from exc
    if not permission_service.check_workflow(db, workflow, "execute").allowed:
        raise PolicyViolation("没有取消该工作流任务的权限")
    now = now or utc_now()
    db.refresh(run)
    if run.status in TERMINAL_RUN_STATUSES:
        raise PolicyViolation("终态任务不能取消")
    if run.status == "running":
        raise PolicyViolation("任务正在执行，无法安全取消外部调用；请等待完成或超时")
    if run.status not in (DISPATCHABLE_RUN_STATUSES | {"awaiting_approval"}):
        raise PolicyViolation("当前任务状态不能取消")
    updated = db.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run.id, WorkflowRun.status == run.status)
        .values(
            status="cancelled",
            error=comment or "任务已取消",
            completed_at=now,
            next_retry_at=None,
        )
    ).rowcount
    if updated != 1:
        db.rollback()
        raise PolicyViolation("任务状态已变化，请刷新后重试")
    if run.status == "awaiting_approval":
        db.execute(
            update(WorkflowApprovalRequest)
            .where(
                WorkflowApprovalRequest.workflow_run_id == run.id,
                WorkflowApprovalRequest.status == "pending",
            )
            .values(status="cancelled", resolved_at=now, comment=comment or "任务已取消")
        )
    db.commit()
    db.refresh(run)
    return run


def assert_workflow_mutable(db: Session, workflow_id: str) -> None:
    """Protect submitted queue evidence from definition edits or cascading delete."""
    active = db.execute(
        select(WorkflowRun.id)
        .where(
            WorkflowRun.workflow_id == workflow_id,
            WorkflowRun.status.in_(ACTIVE_RUN_STATUSES),
        )
        .limit(1)
    ).scalar_one_or_none()
    if active:
        raise PolicyViolation("工作流存在进行中的任务，不能编辑或删除；请先取消或等待完成")


def purge_expired_assistant_attachments(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = 200,
) -> int:
    """Globally purge a bounded batch of temporary assistant content.

    This worker path intentionally has no tenant/user filter: expired rows from
    deleted users and fail-closed legacy rows with no owner must not retain
    parsed business text forever.
    """
    cutoff = now or utc_now()
    bounded_limit = max(1, min(int(limit), 1000))
    expired = list(
        db.execute(
            select(AssistantAttachment)
            .where(
                (AssistantAttachment.expires_at.is_(None))
                | (AssistantAttachment.expires_at <= cutoff)
            )
            .order_by(AssistantAttachment.expires_at, AssistantAttachment.created_at)
            .limit(bounded_limit)
        ).scalars().all()
    )
    for attachment in expired:
        object_deletion_service.enqueue_assistant_attachment_deletion(
            db, attachment
        )
        db.delete(attachment)
    if expired:
        db.flush()
    return len(expired)


def worker_tick(*, limit: int = 8) -> int:
    """供应用生命周期后台协程调用的一次无状态轮询。"""
    from ..database import SessionLocal
    from . import (
        assistant_compilation_job_service,
        mapping_refresh_service,
        rag_service,
    )

    db = SessionLocal()
    try:
        object_deletion_service.process_object_deletion_jobs(
            db, limit=max(50, limit * 25)
        )
        expire_stale_operations(db)
        purge_expired_assistant_attachments(db, limit=max(50, limit * 25))
        assistant_compilation_job_service.purge_expired_completed_execution_inputs(
            db,
            limit=max(50, limit * 25),
        )
        rag_service.expire_stale_document_index_jobs(db)
        mapping_refresh_service.expire_stale_mapping_refresh_jobs(db)
        enqueue_due_schedules(db)
        db.commit()
        workflow_count = len(process_available_runs(db, limit=limit))
        document_count = len(rag_service.process_document_index_jobs(db, limit=max(1, limit // 2)))
        mapping_count = len(mapping_refresh_service.process_mapping_refresh_jobs(db, limit=max(1, limit // 2)))
        return workflow_count + document_count + mapping_count
    finally:
        db.close()
