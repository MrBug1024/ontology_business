"""Worker, lease, and attachment coordination for durable Assistant requests."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import threading
import traceback
from typing import Any, Callable, Iterator
import uuid

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ..models import AssistantMessage, AssistantRequestRun, AssistantThread, ManagedUploadRun
from . import permission_service


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _RequestLease:
    token: str
    generation: int
    expires_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def upload_requires_reupload(upload: ManagedUploadRun, *, as_of: datetime) -> bool:
    expires_at = _as_utc(upload.expires_at)
    return bool(
        (expires_at is not None and expires_at <= as_of)
        or (
            upload.status == "awaiting_upload"
            and bool(str(upload.error_code or "").strip())
        )
    )


def upload_state(
    db: Session,
    run: AssistantRequestRun,
) -> tuple[str, str, str]:
    ids = list(run.upload_run_ids or [])
    rows = list(
        db.scalars(
            select(ManagedUploadRun).where(
                ManagedUploadRun.id.in_(ids),
                ManagedUploadRun.tenant_id == run.tenant_id,
                ManagedUploadRun.requested_by_user_id == run.requested_by_user_id,
                ManagedUploadRun.purpose == "invocation_attachment",
            )
        ).all()
    )
    if len(rows) != len(ids):
        return "failed", "assistant_upload_unavailable", "临时附件上传任务不可用"
    now = _now()
    if any(upload_requires_reupload(item, as_of=now) for item in rows):
        return (
            "failed",
            "assistant_upload_requires_reupload",
            "附件正文上传失败或已过期，请重新选择文件并发送新的请求",
        )
    failed = [item for item in rows if item.status in {"failed", "cancelled"}]
    if failed:
        first = failed[0]
        return (
            "failed",
            first.error_code or "assistant_upload_failed",
            first.error_message or "临时附件处理失败",
        )
    if all(item.status == "ready" for item in rows):
        return "ready", "", ""
    return "waiting", "", ""


def assert_execution_lease(
    db: Session,
    run_id: str,
    *,
    lease_token: str,
    lease_generation: int,
    for_update: bool,
    scenario_verb: str,
    conflict_factory: Callable[[str], Exception],
    authorize_run_thread: Callable[..., AssistantThread],
) -> None:
    principal = permission_service.require_principal(db)
    permission_service.require_tenant_permission(db, "read")
    now = _now()
    statement = select(AssistantRequestRun).where(
        AssistantRequestRun.id == run_id,
        AssistantRequestRun.tenant_id == principal.tenant_id,
        AssistantRequestRun.requested_by_user_id == principal.user_id,
        AssistantRequestRun.status == "running",
        AssistantRequestRun.lease_token == lease_token,
        AssistantRequestRun.lease_generation == lease_generation,
        AssistantRequestRun.lease_expires_at > now,
    )
    if for_update:
        statement = statement.with_for_update()
    run = db.scalar(statement.execution_options(populate_existing=True))
    if run is None:
        raise conflict_factory("助手请求执行权已失效，迟到结果已拒绝")
    authorize_run_thread(db, run, scenario_verb=scenario_verb)


def _committed_outcome(
    run: AssistantRequestRun,
    message: AssistantMessage | None,
) -> tuple[str, str, str]:
    if (
        message is None
        or message.thread_id != run.thread_id
        or not isinstance(message.context, dict)
        or str(message.context.get("assistant_request_run_id") or "") != run.id
    ):
        return "", "", ""
    status = str(message.context.get("assistant_request_status") or "")
    if status == "result_committed":
        return status, "", ""
    if status == "failure_committed":
        return (
            status,
            str(
                message.context.get("assistant_request_error_code")
                or "assistant_request_failed"
            ),
            str(message.content or "这次助手请求未完成，请显式重试。"),
        )
    return "", "", ""


def _mark_succeeded(
    run: AssistantRequestRun,
    message: AssistantMessage,
) -> None:
    now = _now()
    run.status = "succeeded"
    run.revision += 1
    run.lease_token = ""
    run.lease_expires_at = None
    run.error_code = ""
    run.error_message = ""
    run.finished_at = now
    run.updated_at = now
    message.context = {
        **dict(message.context or {}),
        "assistant_request_run_id": run.id,
        "assistant_request_status": "succeeded",
        "assistant_request_revision": run.revision,
    }


def _mark_failed(
    db: Session,
    run: AssistantRequestRun,
    code: str,
    message: str,
) -> None:
    now = _now()
    run.status = "failed"
    run.revision += 1
    run.lease_token = ""
    run.lease_expires_at = None
    run.error_code = code[:80]
    run.error_message = message[:1000]
    run.finished_at = now
    run.updated_at = now
    assistant_message = db.get(AssistantMessage, run.assistant_message_id)
    committed_status, _committed_code, _committed_message = _committed_outcome(
        run, assistant_message
    )
    if assistant_message is not None and assistant_message.thread_id == run.thread_id:
        if committed_status != "failure_committed":
            assistant_message.content = run.error_message
        assistant_message.context = {
            **dict(assistant_message.context or {}),
            "status": "failed",
            "error_code": run.error_code,
            "assistant_request_run_id": run.id,
            "assistant_request_status": "failed",
            "assistant_request_revision": run.revision,
        }


def _claim(
    db: Session,
    run_id: str,
    *,
    lease_seconds: int,
    upload_state_reader: Callable[
        [Session, AssistantRequestRun], tuple[str, str, str]
    ],
) -> _RequestLease | None:
    run = db.scalar(
        select(AssistantRequestRun)
        .where(AssistantRequestRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None:
        return None
    now = _now()
    expired = _as_utc(run.lease_expires_at)
    if run.status == "running" and (expired is None or expired > now):
        return None
    if run.status not in {"waiting_upload", "queued", "running"}:
        return None
    assistant_message = db.get(AssistantMessage, run.assistant_message_id)
    committed_status, committed_code, committed_message = _committed_outcome(
        run, assistant_message
    )
    if committed_status == "result_committed":
        assert assistant_message is not None
        _mark_succeeded(run, assistant_message)
        db.commit()
        return None
    if committed_status == "failure_committed":
        _mark_failed(
            db,
            run,
            committed_code,
            committed_message,
        )
        db.commit()
        return None
    state, code, message = upload_state_reader(db, run)
    if state == "failed":
        _mark_failed(db, run, code, message)
        db.commit()
        return None
    if state == "waiting":
        run.status = "waiting_upload"
        run.lease_token = ""
        run.lease_expires_at = None
        run.available_at = now + timedelta(seconds=1)
        run.updated_at = now
        db.commit()
        return None
    token = uuid.uuid4().hex
    run.status = "running"
    run.revision += 1
    run.lease_generation += 1
    run.lease_token = token
    run.lease_expires_at = now + timedelta(seconds=lease_seconds)
    run.error_code = ""
    run.error_message = ""
    run.updated_at = now
    assistant_message = db.get(AssistantMessage, run.assistant_message_id)
    result_committed = bool(
        assistant_message is not None
        and assistant_message.thread_id == run.thread_id
        and isinstance(assistant_message.context, dict)
        and assistant_message.context.get("assistant_request_status")
        == "result_committed"
    )
    if (
        assistant_message is not None
        and assistant_message.thread_id == run.thread_id
        and not result_committed
    ):
        assistant_message.content = "附件准备完成；正在根据本次需求规划处理步骤。"
        assistant_message.context = {
            **dict(assistant_message.context or {}),
            "status": "processing",
            "assistant_request_run_id": run.id,
            "assistant_request_status": "running",
            "assistant_request_revision": run.revision,
        }
    db.commit()
    return _RequestLease(token, run.lease_generation, run.lease_expires_at)


def _renew(
    run_id: str,
    lease: _RequestLease,
    *,
    lease_seconds: int,
    session_factory: Callable[[], Session],
) -> bool:
    db = session_factory()
    try:
        now = _now()
        result = db.execute(
            update(AssistantRequestRun)
            .where(
                AssistantRequestRun.id == run_id,
                AssistantRequestRun.status == "running",
                AssistantRequestRun.lease_token == lease.token,
                AssistantRequestRun.lease_generation == lease.generation,
                AssistantRequestRun.lease_expires_at > now,
            )
            .values(
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                updated_at=now,
            )
        )
        db.commit()
        return bool(result.rowcount)
    finally:
        db.close()


@contextmanager
def _heartbeat(
    run_id: str,
    lease: _RequestLease,
    *,
    lease_seconds: int,
    session_factory: Callable[[], Session],
) -> Iterator[threading.Event]:
    stopped = threading.Event()
    lost = threading.Event()

    def pulse() -> None:
        while not stopped.wait(lease_seconds / 3):
            try:
                if not _renew(
                    run_id,
                    lease,
                    lease_seconds=lease_seconds,
                    session_factory=session_factory,
                ):
                    lost.set()
                    return
            except Exception:
                lost.set()
                return

    thread = threading.Thread(
        target=pulse,
        name=f"assistant-request-{run_id[:8]}",
        daemon=True,
    )
    thread.start()
    try:
        yield lost
    finally:
        stopped.set()
        thread.join(timeout=1)


def process_request(
    run_id: str,
    executor: Callable[[dict[str, Any], str, str, str, str, int], None],
    *,
    lease_seconds: int,
    session_factory: Callable[[], Session],
    upload_state_reader: Callable[
        [Session, AssistantRequestRun], tuple[str, str, str]
    ],
) -> bool:
    db = session_factory()
    try:
        lease = _claim(
            db,
            run_id,
            lease_seconds=lease_seconds,
            upload_state_reader=upload_state_reader,
        )
        if lease is None:
            return False
        run = db.get(AssistantRequestRun, run_id)
        if run is None:
            return False
        tenant_id = run.tenant_id
        user_id = run.requested_by_user_id
        payload = dict(run.payload_document or {})
        assistant_message_id = run.assistant_message_id
        db.rollback()
    finally:
        db.close()
    try:
        with _heartbeat(
            run_id,
            lease,
            lease_seconds=lease_seconds,
            session_factory=session_factory,
        ) as lost:
            executor(
                payload,
                tenant_id,
                user_id,
                run_id,
                lease.token,
                lease.generation,
            )
            if lost.is_set():
                return False
        db = session_factory()
        try:
            now = _now()
            run = db.scalar(
                select(AssistantRequestRun)
                .where(
                    AssistantRequestRun.id == run_id,
                    AssistantRequestRun.status == "running",
                    AssistantRequestRun.lease_token == lease.token,
                    AssistantRequestRun.lease_generation == lease.generation,
                    AssistantRequestRun.lease_expires_at > now,
                )
                .with_for_update()
            )
            if run is None:
                db.rollback()
                return False
            message = db.get(AssistantMessage, assistant_message_id)
            committed_status, committed_code, committed_message = _committed_outcome(
                run, message
            )
            if committed_status == "failure_committed":
                _mark_failed(db, run, committed_code, committed_message)
                db.commit()
                return False
            if committed_status != "result_committed":
                _mark_failed(
                    db,
                    run,
                    "assistant_result_not_committed",
                    "这次助手请求未形成可恢复结果，请显式重试。",
                )
                db.commit()
                return False
            assert message is not None
            _mark_succeeded(run, message)
            db.commit()
            return True
        finally:
            db.close()
    except Exception as exc:
        # Exception strings may contain SQL parameters, payloads or credentials.
        # Record only trusted code locations and the exception type.
        frames = traceback.extract_tb(exc.__traceback__, limit=8)
        logger.error(
            "助手请求执行失败 (%s; %s)",
            type(exc).__name__,
            " -> ".join(f"{frame.name}:{frame.lineno}" for frame in frames),
            extra={"assistant_request_run_id": run_id},
        )
        db = session_factory()
        try:
            run = db.scalar(
                select(AssistantRequestRun)
                .where(
                    AssistantRequestRun.id == run_id,
                    AssistantRequestRun.status == "running",
                    AssistantRequestRun.lease_token == lease.token,
                    AssistantRequestRun.lease_generation == lease.generation,
                    AssistantRequestRun.lease_expires_at > _now(),
                )
                .with_for_update()
            )
            if run is not None:
                assistant_message = db.get(AssistantMessage, run.assistant_message_id)
                committed_status, committed_code, committed_message = (
                    _committed_outcome(run, assistant_message)
                )
                if committed_status == "result_committed":
                    assert assistant_message is not None
                    _mark_succeeded(run, assistant_message)
                    db.commit()
                    return True
                _mark_failed(
                    db,
                    run,
                    (
                        committed_code
                        if committed_status == "failure_committed"
                        else "assistant_request_failed"
                    ),
                    (
                        committed_message
                        if committed_status == "failure_committed"
                        else "这次助手请求未完成，系统未执行任何正式变更；请显式重试。"
                    ),
                )
                db.commit()
            return False
        finally:
            db.close()


def process_next_request(
    executor: Callable[[dict[str, Any], str, str, str, str, int], None],
    *,
    session_factory: Callable[[], Session],
    request_processor: Callable[
        [str, Callable[[dict[str, Any], str, str, str, str, int], None]], bool
    ],
) -> bool:
    db = session_factory()
    try:
        now = _now()
        run_id = db.scalar(
            select(AssistantRequestRun.id)
            .where(
                or_(
                    (
                        AssistantRequestRun.status.in_(["waiting_upload", "queued"])
                        & (AssistantRequestRun.available_at <= now)
                    ),
                    (
                        (AssistantRequestRun.status == "running")
                        & (AssistantRequestRun.lease_expires_at <= now)
                    ),
                )
            )
            .order_by(AssistantRequestRun.available_at, AssistantRequestRun.created_at)
            .limit(1)
        )
    finally:
        db.close()
    return request_processor(run_id, executor) if run_id else False
