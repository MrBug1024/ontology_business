"""Lease-fenced state machine and worker loop for durable Agent turns."""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import threading
from typing import Any
import uuid

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ..catalog_schemas import ValidationDatasetBuildIn
from ..models import AgentTurnEvent, AgentTurnRun, Message
from . import (
    agent_runtime_adapter,
    agent_turn_input_service,
    agent_turn_payload_service,
    agent_turn_progress_service,
    permission_service,
    validation_dataset_service,
)


TURN_LEASE_SECONDS = 90
TURN_RECHECK_SECONDS = 2
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "indeterminate"})
RUNNABLE_STATUSES = frozenset(
    {
        "accepted",
        "preparing_inputs",
        "validating_contracts",
        "planning",
        "invoking_tools",
        "responding",
        "cancel_requested",
    }
)
CANCEL_RECONCILIATION_STATUSES = frozenset({"invoking_tools", "responding"})


@dataclass(frozen=True)
class TurnLease:
    token: str
    generation: int
    expires_at: datetime


TurnExecutor = Callable[..., dict[str, Any]]
SessionFactory = Callable[[], Session]
FinalizeTurn = Callable[..., bool]
ClaimTurn = Callable[..., TurnLease | None]
ProcessClaimedTurn = Callable[[str, TurnLease, TurnExecutor], bool]
ProcessTurn = Callable[[str, TurnExecutor], bool]


STATUS_LABELS = {
    "accepted": "需求已接收",
    "preparing_inputs": "正在准备输入资料",
    "validating_contracts": "正在校验输入契约",
    "planning": "正在规划处理步骤",
    "invoking_tools": "正在调用受治理能力",
    "responding": "正在整理结果",
    "succeeded": "对话已完成",
    "failed": "处理失败",
    "cancel_requested": "正在取消",
    "cancelled": "已取消",
    "indeterminate": "执行结果待核对",
}


def _append_event(
    db: Session,
    run: AgentTurnRun,
    event_type: str,
    data: Mapping[str, Any] | None = None,
) -> None:
    db.add(
        AgentTurnEvent(
            tenant_id=run.tenant_id,
            run_id=run.id,
            revision=run.revision,
            event_type=event_type,
            data=dict(data or {}),
        )
    )


def _transition(
    db: Session,
    run: AgentTurnRun,
    status: str,
    *,
    data: Mapping[str, Any] | None = None,
    as_of: datetime | None = None,
) -> None:
    now = as_of or agent_turn_input_service._now()
    run.status = status
    run.revision += 1
    run.updated_at = now
    event_data = {"status": status, "label": STATUS_LABELS[status]}
    event_data.update(dict(data or {}))
    _append_event(db, run, status, event_data)


def _finish_expired_cancellation(
    db: Session,
    run: AgentTurnRun,
    *,
    as_of: datetime,
) -> None:
    requires_reconciliation = bool(
        (run.result_document or {}).get("cancellation_requires_reconciliation")
    )
    run.lease_token = ""
    run.lease_expires_at = None
    run.finished_at = as_of
    if requires_reconciliation:
        run.error_code = "agent_turn_cancellation_indeterminate"
        run.error_message = "取消时外部执行可能已经开始，请先核对执行记录"
        _transition(
            db,
            run,
            "indeterminate",
            data={"error": {"code": run.error_code, "message": run.error_message}},
            as_of=as_of,
        )
    else:
        run.error_code = ""
        run.error_message = ""
        _transition(db, run, "cancelled", as_of=as_of)
    assistant = db.get(Message, run.assistant_message_id) if run.assistant_message_id else None
    if assistant is not None:
        assistant.content = (
            "取消时外部执行可能已经开始，请先核对执行记录。"
            if requires_reconciliation
            else "该任务已取消。"
        )
        assistant.stream_finalized = True


def claim_turn(
    db: Session,
    run_id: str,
    *,
    as_of: datetime | None = None,
) -> TurnLease | None:
    now = as_of or agent_turn_input_service._now()
    run = db.scalar(
        select(AgentTurnRun)
        .where(AgentTurnRun.id == run_id)
        .with_for_update()
        # A row lock does not refresh SQLAlchemy's identity map. A worker may
        # have observed this run before another process acquired its lease.
        .execution_options(populate_existing=True)
    )
    if run is None or run.status in TERMINAL_STATUSES:
        return None
    lease_expiry = agent_turn_input_service._as_utc(run.lease_expires_at)
    if run.status == "cancel_requested" and (
        not run.lease_token or lease_expiry is None or lease_expiry <= now
    ):
        _finish_expired_cancellation(db, run, as_of=now)
        db.commit()
        return None
    if run.status not in RUNNABLE_STATUSES:
        return None
    if run.lease_token and lease_expiry is not None and lease_expiry > now:
        return None
    token = uuid.uuid4().hex
    run.lease_generation += 1
    run.lease_token = token
    run.lease_expires_at = now + timedelta(seconds=TURN_LEASE_SECONDS)
    run.updated_at = now
    db.commit()
    return TurnLease(
        token=token,
        generation=run.lease_generation,
        expires_at=run.lease_expires_at,
    )


def _lease_matches(run: AgentTurnRun, lease: TurnLease, *, as_of: datetime) -> bool:
    expiry = agent_turn_input_service._as_utc(run.lease_expires_at)
    return bool(
        run.lease_token == lease.token
        and run.lease_generation == lease.generation
        and expiry is not None
        and expiry > as_of
    )


def transition_claimed_turn(
    db: Session,
    run_id: str,
    *,
    lease: TurnLease,
    status: str,
) -> bool:
    """Advance a worker-owned turn without overwriting cancellation or a newer lease."""
    if status not in RUNNABLE_STATUSES - {"accepted", "cancel_requested"}:
        raise ValueError("Agent Turn running status is invalid")
    now = agent_turn_input_service._now()
    run = db.scalar(
        select(AgentTurnRun)
        .where(AgentTurnRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        run is None
        or run.status == "cancel_requested"
        or not _lease_matches(run, lease, as_of=now)
    ):
        db.rollback()
        return False
    _transition(db, run, status, as_of=now)
    db.commit()
    return True


def _bounded_result(result: Mapping[str, Any]) -> dict[str, Any]:
    document = {
        key: result[key]
        for key in (
            "answer",
            "conversation_id",
            "assistant_message_id",
            "trace_id",
            "citations",
            "input_snapshot",
            "evidence_refs",
            "runtime",
        )
        if key in result
    }
    encoded = agent_turn_input_service._canonical_bytes(document)
    if len(encoded) <= 1_048_576:
        return document
    return {
        "answer": str(document.get("answer") or "")[:200_000],
        "conversation_id": str(document.get("conversation_id") or ""),
        "assistant_message_id": str(document.get("assistant_message_id") or ""),
        "trace_id": str(document.get("trace_id") or ""),
        "truncated": True,
    }


def finalize_turn(
    db: Session,
    run_id: str,
    *,
    lease: TurnLease,
    status: str,
    result: Mapping[str, Any] | None = None,
    error_code: str = "",
    error_message: str = "",
) -> bool:
    if status not in TERMINAL_STATUSES:
        raise ValueError("Agent Turn final status is invalid")
    run = db.scalar(
        select(AgentTurnRun)
        .where(AgentTurnRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = agent_turn_input_service._now()
    if run is None or not _lease_matches(run, lease, as_of=now):
        db.rollback()
        return False
    if run.status == "cancel_requested":
        requires_reconciliation = bool(
            (run.result_document or {}).get("cancellation_requires_reconciliation")
        )
        status = "indeterminate" if requires_reconciliation else "cancelled"
        result = {}
        if requires_reconciliation:
            error_code = "agent_turn_cancellation_indeterminate"
            error_message = "取消时外部执行可能已经开始，请先核对执行记录"
        else:
            error_code = ""
            error_message = ""
    public_result = _bounded_result(result or {})
    run.result_document = public_result
    run.error_code = str(error_code or "")[:80]
    run.error_message = str(error_message or "")[:1000]
    run.lease_token = ""
    run.lease_expires_at = None
    run.finished_at = now
    runtime = public_result.get("runtime")
    if isinstance(runtime, Mapping):
        run.definition_hash = str(runtime.get("definition_hash") or "")[:64]
        run.deployment_fingerprint = str(runtime.get("deployment_fingerprint") or "")[:64]
    snapshot = public_result.get("input_snapshot")
    if isinstance(snapshot, Mapping):
        runtime_snapshot = snapshot.get("runtime")
        if isinstance(runtime_snapshot, Mapping):
            run.data_context_fingerprint = str(
                runtime_snapshot.get("data_context_fingerprint") or ""
            )[:64]
    event_data: dict[str, Any] = {"result": public_result}
    if run.error_code:
        event_data["error"] = {"code": run.error_code, "message": run.error_message}
    _transition(db, run, status, data=event_data, as_of=now)
    assistant = db.get(Message, run.assistant_message_id) if run.assistant_message_id else None
    if assistant is not None:
        if status == "succeeded":
            assistant.content = str(public_result.get("answer") or assistant.content)
        elif status == "cancelled":
            assistant.content = "该任务已取消。"
        elif error_message:
            assistant.content = f"处理失败：{run.error_message}"
        assistant.stream_finalized = True
    db.commit()
    return True


def _release_lease(
    db: Session,
    run: AgentTurnRun,
    lease: TurnLease,
    *,
    delay_seconds: int = TURN_RECHECK_SECONDS,
) -> bool:
    now = agent_turn_input_service._now()
    # Session factories disable autoflush. Flush the atomic preparation event
    # before populate_existing can restore the prior revision.
    db.flush()
    current = db.scalar(
        select(AgentTurnRun)
        .where(AgentTurnRun.id == run.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if current is None or not _lease_matches(current, lease, as_of=now):
        db.rollback()
        return False
    if current.status == "cancel_requested":
        _finish_expired_cancellation(db, current, as_of=now)
        db.commit()
        return True
    current.lease_token = ""
    current.lease_expires_at = None
    current.available_at = now + timedelta(seconds=delay_seconds)
    current.updated_at = now
    db.commit()
    return True


def _renew_turn_lease(
    run_id: str,
    lease: TurnLease,
    *,
    session_factory: SessionFactory,
) -> bool:
    db = session_factory()
    try:
        now = agent_turn_input_service._now()
        result = db.execute(
            update(AgentTurnRun)
            .where(
                AgentTurnRun.id == run_id,
                AgentTurnRun.lease_token == lease.token,
                AgentTurnRun.lease_generation == lease.generation,
                AgentTurnRun.lease_expires_at.is_not(None),
                AgentTurnRun.lease_expires_at > now,
                AgentTurnRun.status.not_in(TERMINAL_STATUSES | {"cancel_requested"}),
            )
            .values(lease_expires_at=now + timedelta(seconds=TURN_LEASE_SECONDS))
        )
        db.commit()
        return bool(result.rowcount)
    finally:
        db.close()


@contextmanager
def _turn_heartbeat(
    run_id: str,
    lease: TurnLease,
    *,
    session_factory: SessionFactory,
) -> Iterator[threading.Event]:
    stopped = threading.Event()
    lease_lost = threading.Event()

    def heartbeat() -> None:
        while not stopped.wait(max(1.0, TURN_LEASE_SECONDS / 3)):
            try:
                if not _renew_turn_lease(
                    run_id,
                    lease,
                    session_factory=session_factory,
                ):
                    lease_lost.set()
                    return
            except Exception:  # noqa: BLE001 - fenced finalization remains authoritative.
                lease_lost.set()
                return

    thread = threading.Thread(
        target=heartbeat,
        name=f"agent-turn-heartbeat-{run_id[:8]}",
        daemon=True,
    )
    thread.start()
    try:
        yield lease_lost
    finally:
        stopped.set()
        thread.join(timeout=1.0)


def _known_error(exc: Exception) -> tuple[str, str, bool]:
    if isinstance(exc, agent_turn_input_service.AgentTurnError):
        return exc.code, exc.message, True
    if isinstance(exc, agent_turn_payload_service.AgentTurnPayloadError):
        return exc.code, exc.message, True
    if isinstance(exc, agent_runtime_adapter.AgentRuntimeAdapterError):
        return exc.code, exc.message, True
    return "agent_turn_failed", "Agent Turn 处理失败，请重试", False


def _process_claimed_turn(
    run_id: str,
    lease: TurnLease,
    executor: TurnExecutor,
    *,
    session_factory: SessionFactory,
    finalize_fn: FinalizeTurn = finalize_turn,
) -> bool:
    db = session_factory()
    execution_started = False
    try:
        run = db.get(AgentTurnRun, run_id)
        if run is None or not run.requested_by_user_id or not run.agent_id:
            raise agent_turn_input_service.AgentTurnError(
                "agent_turn_owner_unavailable",
                "Agent Turn 发起人或 Agent 已失效",
            )
        db.info["tenant_id"] = run.tenant_id
        db.info["user_id"] = run.requested_by_user_id
        permission_service.require_principal(db)
        agent_turn_input_service._require_agent(db, run.agent_id)
        user_message = db.get(Message, run.user_message_id) if run.user_message_id else None
        if user_message is None or user_message.conversation_id != run.conversation_id:
            raise agent_turn_input_service.AgentTurnError(
                "agent_turn_message_unavailable",
                "Agent Turn 原始消息已失效",
            )
        payload = agent_turn_input_service._open_authenticated_request(
            run,
            user_message.content,
        )
        resolved_payload, _pending_uploads = (
            agent_turn_input_service._resolve_upload_attachments(
                db,
                payload,
                user_id=run.requested_by_user_id,
            )
        )
        if resolved_payload is None:
            if run.status != "preparing_inputs":
                if not transition_claimed_turn(
                    db,
                    run_id,
                    lease=lease,
                    status="preparing_inputs",
                ):
                    finalize_fn(db, run_id, lease=lease, status="cancelled")
                    return False
            return _release_lease(
                db,
                run,
                lease,
                delay_seconds=TURN_RECHECK_SECONDS,
            )
        payload = resolved_payload
        table_ids = agent_turn_input_service._table_asset_version_ids(db, payload)
        dataset_result: Mapping[str, Any] | None = None
        if table_ids and not run.preparation_run_id:
            job = validation_dataset_service.enqueue_validation_dataset_job(
                db,
                ValidationDatasetBuildIn(
                    asset_version_ids=table_ids,
                    name="Agent 输入数据包",
                ),
            )
            run = db.scalar(
                select(AgentTurnRun)
                .where(AgentTurnRun.id == run_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                run is None
                or not _lease_matches(
                    run,
                    lease,
                    as_of=agent_turn_input_service._now(),
                )
            ):
                db.rollback()
                return False
            if run.status == "cancel_requested":
                db.rollback()
                return finalize_fn(db, run_id, lease=lease, status="cancelled")
            run.preparation_run_id = str(job["id"])
            _transition(
                db,
                run,
                "preparing_inputs",
                data={"preparation_run_id": run.preparation_run_id},
            )
            return _release_lease(db, run, lease)
        if run.preparation_run_id:
            job = validation_dataset_service.get_validation_dataset_job(
                db,
                run.preparation_run_id,
            )
            if job["status"] in {"queued", "running"}:
                return _release_lease(db, run, lease)
            if job["status"] != "succeeded" or not isinstance(job.get("result"), Mapping):
                raise agent_turn_input_service.AgentTurnError(
                    "input_preparation_failed",
                    str(job.get("error") or "输入资料准备失败")[:1000],
                )
            dataset_result = job["result"]
        payload = agent_turn_input_service._prepared_payload(
            payload,
            table_asset_ids=set(table_ids),
            dataset_result=dataset_result,
        )
        for next_status in ("validating_contracts", "planning", "invoking_tools"):
            if not transition_claimed_turn(
                db,
                run_id,
                lease=lease,
                status=next_status,
            ):
                finalize_fn(db, run_id, lease=lease, status="cancelled")
                return False
        run = db.get(AgentTurnRun, run_id)
        if run is None:
            return False
        with _turn_heartbeat(
            run_id,
            lease,
            session_factory=session_factory,
        ) as lease_lost:
            execution_started = True
            progress = agent_turn_progress_service.TurnProgress(run_id, lease, session_factory)
            result = executor(
                run.agent_id,
                message=payload.message,
                conversation_id=run.conversation_id,
                db=db,
                inputs=payload.inputs,
                release_id=payload.release_id,
                managed_inputs=payload.managed_inputs,
                attachments=payload.attachments,
                capability=(
                    payload.capability.model_dump(mode="json")
                    if payload.capability is not None
                    else None
                ),
                idempotency_key=f"agent-turn:{run.id}",
                user_message_id=run.user_message_id,
                assistant_message_id=run.assistant_message_id,
                turn_run_id=run.id,
                turn_lease_token=lease.token,
                turn_lease_generation=lease.generation,
                defer_terminal_commit=True,
                on_event=progress,
            )
            progress.flush()
        if lease_lost.is_set():
            # The executor may have staged the final Message in this Session.
            # A lost fence must discard it before another owner reconciles.
            db.rollback()
            cancellation_db = session_factory()
            try:
                current = cancellation_db.get(AgentTurnRun, run_id)
                if current is not None and current.status == "cancel_requested":
                    return finalize_fn(
                        cancellation_db,
                        run_id,
                        lease=lease,
                        status="cancelled",
                    )
            finally:
                cancellation_db.close()
            return False
        return finalize_fn(
            db,
            run_id,
            lease=lease,
            status="succeeded",
            result=result,
        )
    except Exception as exc:  # noqa: BLE001 - worker maps to stable public errors.
        db.rollback()
        code, message, deterministic = _known_error(exc)
        final_status = "failed"
        if execution_started and not deterministic:
            final_status = "indeterminate"
            code = "agent_turn_result_indeterminate"
            message = "执行结果无法确认，请核对执行记录后再决定是否重试"
        return finalize_fn(
            db,
            run_id,
            lease=lease,
            status=final_status,
            error_code=code,
            error_message=message,
        )
    finally:
        db.close()


def process_turn(
    run_id: str,
    executor: TurnExecutor,
    *,
    session_factory: SessionFactory,
    claim_fn: ClaimTurn = claim_turn,
    process_claimed_fn: ProcessClaimedTurn | None = None,
    finalize_fn: FinalizeTurn = finalize_turn,
) -> bool:
    db = session_factory()
    try:
        lease = claim_fn(db, run_id)
    finally:
        db.close()
    if lease is None:
        return False
    if process_claimed_fn is not None:
        return process_claimed_fn(run_id, lease, executor)
    return _process_claimed_turn(
        run_id,
        lease,
        executor,
        session_factory=session_factory,
        finalize_fn=finalize_fn,
    )


def process_next_turn(
    executor: TurnExecutor,
    *,
    session_factory: SessionFactory,
    process_fn: ProcessTurn,
) -> bool:
    db = session_factory()
    try:
        now = agent_turn_input_service._now()
        run_id = db.scalar(
            select(AgentTurnRun.id)
            .where(
                AgentTurnRun.status.in_(RUNNABLE_STATUSES),
                AgentTurnRun.available_at <= now,
                or_(
                    AgentTurnRun.lease_token == "",
                    AgentTurnRun.lease_expires_at.is_(None),
                    AgentTurnRun.lease_expires_at <= now,
                ),
            )
            .order_by(AgentTurnRun.available_at, AgentTurnRun.created_at, AgentTurnRun.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        db.commit()
    finally:
        db.close()
    return process_fn(str(run_id), executor) if run_id else False


__all__ = [
    "CANCEL_RECONCILIATION_STATUSES",
    "RUNNABLE_STATUSES",
    "STATUS_LABELS",
    "TERMINAL_STATUSES",
    "TURN_LEASE_SECONDS",
    "TURN_RECHECK_SECONDS",
    "TurnLease",
    "claim_turn",
    "finalize_turn",
    "process_next_turn",
    "process_turn",
    "transition_claimed_turn",
]
