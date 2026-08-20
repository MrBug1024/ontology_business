"""P1 运营执行底座：持久化工作流任务、事件投递、重试和人工审批。

实现刻意使用数据库轮询而非内存队列：任务、事件和审批在服务重启后仍然可见，
并且后续可将同一张运行表接入独立 worker / Redis，而不改变 API 契约。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import (
    BusinessScenario,
    EventEnvelope,
    OntologyEvent,
    OntologyWorkflow,
    WorkflowApprovalRequest,
    WorkflowRun,
)
from . import permission_service
from .policies import PolicyViolation, validate_action_params


TERMINAL_RUN_STATUSES = {"succeeded", "failed", "timed_out", "rejected", "cancelled"}
DISPATCHABLE_RUN_STATUSES = {"queued", "retry_waiting"}


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
            cfg.get("interval_seconds"), minimum=1, maximum=2_592_000, default=1, label="定时间隔（秒）"
        )
    elif trigger_type == "event" and not (cfg.get("event_id") or cfg.get("event_name")):
        raise PolicyViolation("事件触发工作流必须选择事件")


def _is_active(workflow: OntologyWorkflow) -> bool:
    return bool(workflow.enabled) and (workflow.status or "draft") == "active"


def enqueue_workflow_run(
    db: Session,
    workflow: OntologyWorkflow,
    params: dict[str, Any] | None = None,
    *,
    trigger_source: str = "manual",
    event_envelope_id: str | None = None,
    dedupe_key: str | None = None,
    scheduled_for: datetime | None = None,
    created_by_user_id: str | None = None,
    available_at: datetime | None = None,
) -> tuple[WorkflowRun, bool]:
    """把运行请求持久化到队列。返回 (run, 是否新建)。"""
    if not _is_active(workflow):
        raise PolicyViolation("工作流当前未启用，不能提交任务")
    if trigger_source in {"manual", "retry"}:
        decision = permission_service.check_workflow(db, workflow, "execute")
        if not decision.allowed:
            raise PolicyViolation("没有提交该工作流的权限")
    policy = runtime_policy(workflow.trigger_config or {})
    if dedupe_key:
        existing = db.execute(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id == workflow.id, WorkflowRun.dedupe_key == dedupe_key)
            .order_by(WorkflowRun.created_at.desc())
        ).scalars().first()
        if existing:
            return existing, False

    now = utc_now()
    run = WorkflowRun(
        scenario_id=workflow.scenario_id,
        workflow_id=workflow.id,
        trigger_source=trigger_source,
        event_envelope_id=event_envelope_id,
        dedupe_key=dedupe_key,
        input_params=dict(params or {}),
        status="queued",
        max_attempts=policy["max_attempts"],
        timeout_seconds=policy["timeout_seconds"],
        available_at=available_at or now,
        scheduled_for=scheduled_for,
        created_by_user_id=created_by_user_id,
    )
    db.add(run)
    db.flush()
    return run, True


def publish_event(
    db: Session,
    event: OntologyEvent,
    payload: dict[str, Any] | None = None,
    *,
    source: str = "manual",
    source_run_id: str | None = None,
    dedupe_key: str | None = None,
    created_by_user_id: str | None = None,
) -> tuple[EventEnvelope, list[WorkflowRun]]:
    """持久化一个事件信封，并把匹配的启用工作流原子地投递到队列。"""
    if not event.enabled:
        raise PolicyViolation("事件已停用，不能发布")
    raw_payload = dict(payload or {})
    if event.payload_schema:
        raw_payload = validate_action_params(event.payload_schema, raw_payload)
    if dedupe_key:
        existing = db.execute(
            select(EventEnvelope)
            .where(EventEnvelope.event_id == event.id, EventEnvelope.dedupe_key == dedupe_key)
            .order_by(EventEnvelope.created_at.desc())
        ).scalars().first()
        if existing:
            existing_runs = db.execute(
                select(WorkflowRun).where(WorkflowRun.event_envelope_id == existing.id)
            ).scalars().all()
            return existing, existing_runs

    envelope = EventEnvelope(
        scenario_id=event.scenario_id,
        event_id=event.id,
        name=event.name,
        payload=raw_payload,
        source=source,
        source_run_id=source_run_id,
        dedupe_key=dedupe_key,
    )
    db.add(envelope)
    db.flush()

    subscribers = db.execute(
        select(OntologyWorkflow).where(
            OntologyWorkflow.scenario_id == event.scenario_id,
            OntologyWorkflow.trigger_type == "event",
            OntologyWorkflow.status == "active",
            OntologyWorkflow.enabled == True,  # noqa: E712
        )
    ).scalars().all()
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
        run, created = enqueue_workflow_run(
            db,
            workflow,
            {"event": raw_payload, "event_id": event.id, "event_name": event.name},
            trigger_source="event",
            event_envelope_id=envelope.id,
            dedupe_key=f"event:{envelope.id}",
            created_by_user_id=created_by_user_id,
        )
        if created:
            queued.append(run)
    return envelope, queued


def enqueue_due_schedules(db: Session, *, now: datetime | None = None) -> list[WorkflowRun]:
    """扫描启用的 interval 定时工作流，并将到期运行写入持久化队列。"""
    now = now or utc_now()
    workflows = db.execute(
        select(OntologyWorkflow).where(
            OntologyWorkflow.trigger_type == "scheduled",
            OntologyWorkflow.status == "active",
            OntologyWorkflow.enabled == True,  # noqa: E712
        )
    ).scalars().all()
    queued: list[WorkflowRun] = []
    for workflow in workflows:
        interval_value = (workflow.trigger_config or {}).get("interval_seconds")
        if interval_value in (None, ""):
            # 遗留的无效配置不应被每秒自动投递。
            continue
        try:
            interval = _bounded_int(
                interval_value,
                minimum=1,
                maximum=2_592_000,
                default=1,
                label="定时间隔（秒）",
            )
        except PolicyViolation:
            # 配置在保存时会被拒绝；此分支保护升级前已有的无效遗留数据。
            continue
        previous = db.execute(
            select(WorkflowRun.scheduled_for)
            .where(
                WorkflowRun.workflow_id == workflow.id,
                WorkflowRun.scheduled_for.is_not(None),
            )
            .order_by(WorkflowRun.scheduled_for.desc())
            .limit(1)
        ).scalar_one_or_none()
        previous = _aware(previous)
        if previous and now < previous + timedelta(seconds=interval):
            continue
        scheduled_for = now.replace(microsecond=0)
        run, created = enqueue_workflow_run(
            db,
            workflow,
            (workflow.trigger_config or {}).get("params") or {},
            trigger_source="scheduled",
            scheduled_for=scheduled_for,
            dedupe_key=f"schedule:{scheduled_for.isoformat()}",
        )
        if created:
            queued.append(run)
    return queued


def _retry_or_finish(db: Session, run: WorkflowRun, *, final_status: str, error: str, now: datetime) -> None:
    policy = runtime_policy(run.workflow.trigger_config or {})
    run.error = error
    if run.attempt < run.max_attempts:
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
    node_id = str(waiting_step.get("node") or waiting_step.get("step") or "approval")
    approval = db.execute(
        select(WorkflowApprovalRequest).where(
            WorkflowApprovalRequest.workflow_run_id == run.id,
            WorkflowApprovalRequest.node_id == node_id,
        )
    ).scalars().first()
    if approval:
        return approval
    details = waiting_step.get("result") or {}
    timeout = details.get("timeout_seconds")
    try:
        timeout_seconds = _bounded_int(timeout, default=0, minimum=1, maximum=2_592_000, label="审批超时")
    except PolicyViolation:
        timeout_seconds = 0
    approval = WorkflowApprovalRequest(
        workflow_run_id=run.id,
        scenario_id=run.scenario_id,
        node_id=node_id,
        node_name=str(waiting_step.get("name") or "人工审批"),
        instructions=str(details.get("instructions") or "请核对影响范围后决定是否批准。"),
        expires_at=now + timedelta(seconds=timeout_seconds) if timeout_seconds else None,
    )
    db.add(approval)
    db.flush()
    return approval


def process_available_runs(db: Session, *, now: datetime | None = None, limit: int = 8) -> list[WorkflowRun]:
    """同步处理少量已到期队列项；由 lifespan 中的后台循环调用。"""
    now = now or utc_now()
    run_ids = db.execute(
        select(WorkflowRun.id)
        .where(
            WorkflowRun.status.in_(DISPATCHABLE_RUN_STATUSES),
            WorkflowRun.available_at <= now,
        )
        .order_by(WorkflowRun.available_at.asc(), WorkflowRun.created_at.asc())
        .limit(max(1, min(limit, 32)))
    ).scalars().all()
    processed: list[WorkflowRun] = []
    for run_id in run_ids:
        run = db.get(WorkflowRun, run_id)
        if not run or run.status not in DISPATCHABLE_RUN_STATUSES or _aware(run.available_at) > now:
            continue
        workflow = db.get(OntologyWorkflow, run.workflow_id)
        if not workflow or not _is_active(workflow):
            run.status = "cancelled"
            run.error = "工作流不存在或当前未启用"
            run.completed_at = now
            db.commit()
            processed.append(run)
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
            )
            .values(**claim_values)
            # SQLite 返回的无时区 datetime 不能由 ORM 在内存中与 utc_now() 比较；
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

            scenario = db.get(BusinessScenario, run.scenario_id)
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
                    execution_id=run.id,
                    approved_node_ids=set(run.approved_node_ids or []),
                    attempt=max(1, run.attempt),
                    source_run_id=run.id,
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
            _retry_or_finish(db, run, final_status="timed_out", error="任务执行超过配置的超时限制", now=finished_at)
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
        approval.status = "expired"
        approval.resolved_at = now
        approval.comment = "审批超时"
        run = approval.workflow_run
        if run.status == "awaiting_approval":
            run.status = "rejected"
            run.error = "审批超时"
            run.completed_at = now

    running = db.execute(select(WorkflowRun).where(WorkflowRun.status == "running")).scalars().all()
    for run in running:
        started_at = _aware(run.started_at)
        if started_at and now > started_at + timedelta(seconds=run.timeout_seconds):
            _retry_or_finish(db, run, final_status="timed_out", error="任务执行超时", now=now)
    db.commit()


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
    workflow = run.workflow or db.get(OntologyWorkflow, run.workflow_id)
    if not workflow or not permission_service.check_workflow(db, workflow, "approve").allowed:
        raise PolicyViolation("没有审批该工作流的权限")
    now = now or utc_now()
    approval = db.execute(
        select(WorkflowApprovalRequest)
        .where(WorkflowApprovalRequest.workflow_run_id == run.id, WorkflowApprovalRequest.status == "pending")
        .order_by(WorkflowApprovalRequest.requested_at.asc())
        .limit(1)
    ).scalars().first()
    if not approval or run.status != "awaiting_approval":
        raise PolicyViolation("当前任务没有待处理的审批")
    if approval.expires_at and _aware(approval.expires_at) <= now:
        approval.status = "expired"
        approval.resolved_at = now
        approval.comment = "审批超时"
        run.status = "rejected"
        run.error = "审批超时"
        run.completed_at = now
        db.commit()
        raise PolicyViolation("审批已超时")

    approval.status = "approved" if approved else "rejected"
    approval.resolved_at = now
    approval.resolved_by_user_id = user_id
    approval.comment = comment
    if approved:
        approved_nodes = set(run.approved_node_ids or [])
        approved_nodes.add(approval.node_id)
        run.approved_node_ids = sorted(approved_nodes)
        run.status = "queued"
        run.trigger_source = "approval"
        run.available_at = now
        run.error = ""
    else:
        run.status = "rejected"
        run.error = comment or "审批被驳回"
        run.completed_at = now
    db.commit()
    db.refresh(run)
    return run


def retry_run(db: Session, run: WorkflowRun, *, now: datetime | None = None) -> WorkflowRun:
    workflow = run.workflow or db.get(OntologyWorkflow, run.workflow_id)
    if not workflow or not permission_service.check_workflow(db, workflow, "execute").allowed:
        raise PolicyViolation("没有重试该工作流的权限")
    if run.status not in {"failed", "timed_out", "cancelled"}:
        raise PolicyViolation("只有失败、超时或取消的任务可以重新执行")
    now = now or utc_now()
    run.status = "queued"
    run.trigger_source = "retry"
    run.attempt = 0
    run.available_at = now
    run.next_retry_at = None
    run.completed_at = None
    run.error = ""
    run.result = {}
    # 手动重试是一轮新的业务运行，要求再次经过审批节点。
    run.approved_node_ids = []
    db.commit()
    db.refresh(run)
    return run


def worker_tick(*, limit: int = 8) -> int:
    """供应用生命周期后台协程调用的一次无状态轮询。"""
    from ..database import SessionLocal
    from . import rag_service

    db = SessionLocal()
    try:
        expire_stale_operations(db)
        rag_service.expire_stale_document_index_jobs(db)
        enqueue_due_schedules(db)
        db.commit()
        workflow_count = len(process_available_runs(db, limit=limit))
        document_count = len(rag_service.process_document_index_jobs(db, limit=max(1, limit // 2)))
        return workflow_count + document_count
    finally:
        db.close()
