"""Durable dispatch for attachment-backed GlobalAssistant requests."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Any, Callable
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import (
    AssistantMessage,
    AssistantRequestRun,
    AssistantThread,
    ManagedUploadRun,
)
from . import (
    assistant_request_worker_service,
    managed_upload_run_service,
    permission_service,
    tenant_service,
)


LEASE_SECONDS = 120


class AssistantRequestError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class AssistantRequestConflict(AssistantRequestError):
    def __init__(self, message: str = "助手请求状态已变化，请刷新后重试") -> None:
        super().__init__("assistant_request_conflict", message, status_code=409)


@dataclass(frozen=True)
class RequestLease:
    token: str
    generation: int
    expires_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def request_message_id(
    kind: str,
    *,
    tenant_id: str,
    user_id: str,
    request_id: str,
) -> str:
    """Return the canonical durable message identity for one Assistant request."""

    if kind not in {"thread", "run", "user", "assistant"}:
        raise ValueError("unsupported assistant request identity kind")
    return hashlib.sha256(
        f"assistant-request/{kind}/v1\0{tenant_id}\0{user_id}\0{request_id}".encode(
            "utf-8"
        )
    ).hexdigest()[:32]


def _public(run: AssistantRequestRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "parent_run_id": run.parent_run_id,
        "request_id": run.request_id,
        "thread_id": run.thread_id,
        "user_message_id": run.user_message_id,
        "assistant_message_id": run.assistant_message_id,
        "status": run.status,
        "revision": run.revision,
        "upload_run_ids": list(run.upload_run_ids or []),
        "error": (
            {"code": run.error_code, "message": run.error_message}
            if run.error_code
            else None
        ),
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "finished_at": run.finished_at,
    }


def _authorize_run_thread(
    db: Session,
    run: AssistantRequestRun,
    *,
    scenario_verb: str = "read",
) -> AssistantThread:
    thread = db.scalar(
        select(AssistantThread)
        .where(
            AssistantThread.id == run.thread_id,
            AssistantThread.tenant_id == run.tenant_id,
            AssistantThread.created_by_user_id == run.requested_by_user_id,
        )
        .execution_options(populate_existing=True)
    )
    if thread is None:
        raise AssistantRequestError(
            "assistant_request_unavailable",
            "助手请求不存在",
            status_code=404,
        )
    if thread.scenario_id:
        scenario = tenant_service.require_scenario(
            db,
            thread.scenario_id,
            writable=scenario_verb == "write",
        )
        permission_service.require_scenario_permission(
            db,
            scenario,
            scenario_verb,
            message="没有当前业务场景的权限",
        )
    else:
        permission_service.require_tenant_permission(db, "read")
    return thread


def _owned(db: Session, run_id: str, *, writable: bool = False) -> AssistantRequestRun:
    principal = permission_service.require_principal(db)
    permission_service.require_tenant_permission(db, "write" if writable else "read")
    run = db.scalar(
        select(AssistantRequestRun).where(
            AssistantRequestRun.id == run_id,
            AssistantRequestRun.tenant_id == principal.tenant_id,
            AssistantRequestRun.requested_by_user_id == principal.user_id,
        )
    )
    if run is None:
        raise AssistantRequestError(
            "assistant_request_unavailable",
            "助手请求不存在",
            status_code=404,
        )
    _authorize_run_thread(db, run)
    return run


def enqueue_request(
    db: Session,
    *,
    run_id: str,
    request_id: str,
    request_fingerprint: str,
    thread_id: str,
    user_message_id: str,
    assistant_message_id: str,
    payload_document: dict[str, Any],
    upload_run_ids: list[str],
) -> dict[str, Any]:
    principal = permission_service.require_principal(db)
    permission_service.require_tenant_permission(db, "write")
    existing = db.scalar(
        select(AssistantRequestRun).where(
            AssistantRequestRun.tenant_id == principal.tenant_id,
            AssistantRequestRun.requested_by_user_id == principal.user_id,
            AssistantRequestRun.request_id == request_id,
        )
    )
    if existing is not None:
        if existing.request_fingerprint != request_fingerprint:
            raise AssistantRequestConflict("request_id 已用于不同的助手输入")
        _authorize_run_thread(db, existing)
        return _public(existing)
    now = _now()
    try:
        # SessionLocal disables autoflush. Materialize the atomically staged
        # thread/messages before their composite foreign keys are referenced
        # by the run; both flushes remain inside the same transaction.
        db.flush()
        run = AssistantRequestRun(
            id=run_id,
            tenant_id=principal.tenant_id,
            requested_by_user_id=principal.user_id,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
            thread_id=thread_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            payload_document=payload_document,
            upload_run_ids=list(upload_run_ids),
            status="waiting_upload",
            revision=1,
            available_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(run)
        db.flush()
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        replay = db.scalar(
            select(AssistantRequestRun).where(
                AssistantRequestRun.tenant_id == principal.tenant_id,
                AssistantRequestRun.requested_by_user_id == principal.user_id,
                AssistantRequestRun.request_id == request_id,
            )
        )
        if replay is None or replay.request_fingerprint != request_fingerprint:
            raise AssistantRequestConflict("助手请求并发登记冲突") from exc
        _authorize_run_thread(db, replay)
        return _public(replay)
    db.refresh(run)
    return _public(run)


def get_request(db: Session, run_id: str) -> dict[str, Any]:
    return _public(_owned(db, run_id))


def retry_request(
    db: Session,
    run_id: str,
    *,
    expected_revision: int,
    idempotency_key: str,
) -> dict[str, Any]:
    run = _owned(db, run_id, writable=True)
    db.refresh(run, with_for_update=True)
    fingerprint = hashlib.sha256(
        (
            "assistant-request-retry/v1\0"
            f"{run.id}\0{run.request_fingerprint}"
        ).encode("utf-8")
    ).hexdigest()
    exact_replay = db.scalar(
        select(AssistantRequestRun).where(
            AssistantRequestRun.tenant_id == run.tenant_id,
            AssistantRequestRun.parent_run_id == run.id,
            AssistantRequestRun.request_id == idempotency_key,
        )
    )
    if exact_replay is not None:
        if exact_replay.request_fingerprint != fingerprint:
            raise AssistantRequestConflict("助手重试身份已绑定到不同的执行输入")
        return _public(exact_replay)
    if run.revision != expected_revision:
        raise AssistantRequestConflict()
    if run.status != "failed":
        raise AssistantRequestConflict("只有失败的助手请求可以重试")
    key_collision = db.scalar(
        select(AssistantRequestRun).where(
            AssistantRequestRun.tenant_id == run.tenant_id,
            AssistantRequestRun.requested_by_user_id == run.requested_by_user_id,
            AssistantRequestRun.request_id == idempotency_key,
        )
    )
    if key_collision is not None:
        raise AssistantRequestConflict("助手重试幂等键已用于其他执行链路")
    different_retry = db.scalar(
        select(AssistantRequestRun.id).where(
            AssistantRequestRun.tenant_id == run.tenant_id,
            AssistantRequestRun.parent_run_id == run.id,
        )
    )
    if different_retry is not None:
        raise AssistantRequestConflict("该助手请求已使用其他幂等键创建重试任务")
    upload_ids = list(run.upload_run_ids or [])
    uploads = list(
        db.scalars(
            select(ManagedUploadRun)
            .where(
                ManagedUploadRun.id.in_(upload_ids),
                ManagedUploadRun.tenant_id == run.tenant_id,
                ManagedUploadRun.requested_by_user_id == run.requested_by_user_id,
                ManagedUploadRun.purpose == "invocation_attachment",
            )
            .order_by(ManagedUploadRun.id)
            .with_for_update()
        ).all()
    )
    if len(uploads) != len(upload_ids):
        raise AssistantRequestError(
            "assistant_upload_unavailable",
            "本次请求的临时附件已不可用，请重新上传",
            status_code=409,
        )
    uploads_by_id = {upload.id: upload for upload in uploads}
    child_upload_ids: list[str] = []
    upload_retry_ids: dict[str, str] = {}
    now = _now()
    for upload_id in upload_ids:
        upload = uploads_by_id[upload_id]
        if upload.status == "cancelled" or _upload_requires_reupload(
            upload, as_of=now
        ):
            raise AssistantRequestError(
                "assistant_upload_requires_reupload",
                "临时附件正文不可恢复，请重新选择文件并发送新的请求",
                status_code=409,
            )
        if upload.status == "failed":
            try:
                upload_retry_key = "assistant-upload-retry:" + hashlib.sha256(
                    (
                        "assistant-upload-retry/v1\0"
                        f"{run.tenant_id}\0{run.id}\0{idempotency_key}\0{upload.id}"
                    ).encode("utf-8")
                ).hexdigest()
                upload = managed_upload_run_service.create_failed_upload_retry(
                    db,
                    upload,
                    idempotency_key=upload_retry_key,
                    require_stored_content=True,
                )
            except managed_upload_run_service.ManagedUploadError as exc:
                raise AssistantRequestError(
                    exc.code, exc.message, status_code=exc.status_code
                ) from exc
        upload_retry_ids[upload_id] = upload.id
        child_upload_ids.append(upload.id)
    now = _now()
    payload_document = {
        **dict(run.payload_document or {}),
        "request_id": idempotency_key,
        "upload_run_ids": child_upload_ids,
    }
    child_id = uuid.uuid4().hex
    child_user_message_id = request_message_id(
        "user",
        tenant_id=run.tenant_id,
        user_id=run.requested_by_user_id,
        request_id=idempotency_key,
    )
    child_assistant_message_id = request_message_id(
        "assistant",
        tenant_id=run.tenant_id,
        user_id=run.requested_by_user_id,
        request_id=idempotency_key,
    )
    parent_user_message = db.get(AssistantMessage, run.user_message_id)
    parent_assistant_message = db.get(AssistantMessage, run.assistant_message_id)
    if (
        parent_user_message is None
        or parent_assistant_message is None
        or parent_user_message.thread_id != run.thread_id
        or parent_assistant_message.thread_id != run.thread_id
    ):
        raise AssistantRequestConflict("原助手请求的消息上下文已失效")
    child_context = {
        **dict(parent_user_message.context or {}),
        "request_id": idempotency_key,
        "upload_run_ids": child_upload_ids,
        "status": "waiting_for_upload",
        "assistant_request_run_id": child_id,
        "assistant_request_status": "waiting_upload",
        "assistant_request_revision": 1,
    }
    child_attachments = []
    for attachment in list(parent_user_message.attachments or []):
        if not isinstance(attachment, dict):
            child_attachments.append(attachment)
            continue
        previous_upload_id = str(
            attachment.get("upload_run_id") or attachment.get("id") or ""
        )
        next_upload_id = upload_retry_ids.get(previous_upload_id)
        child_attachments.append(
            {
                **attachment,
                **(
                    {"id": next_upload_id, "upload_run_id": next_upload_id}
                    if next_upload_id
                    else {}
                ),
            }
        )
    child_user_message = AssistantMessage(
        id=child_user_message_id,
        thread_id=run.thread_id,
        role="user",
        content=parent_user_message.content,
        context=dict(child_context),
        attachments=child_attachments,
        proposal={},
        thinking=[],
        created_at=now,
    )
    child_assistant_message = AssistantMessage(
        id=child_assistant_message_id,
        thread_id=run.thread_id,
        role="assistant",
        content="正在重新检查附件并恢复本次请求。",
        context=child_context,
        attachments=[],
        proposal={},
        thinking=[],
        created_at=now,
    )
    db.add_all([child_user_message, child_assistant_message])
    db.flush()
    child = AssistantRequestRun(
        id=child_id,
        tenant_id=run.tenant_id,
        requested_by_user_id=run.requested_by_user_id,
        parent_run_id=run.id,
        request_id=idempotency_key,
        request_fingerprint=fingerprint,
        thread_id=run.thread_id,
        user_message_id=child_user_message_id,
        assistant_message_id=child_assistant_message_id,
        payload_document=payload_document,
        upload_run_ids=child_upload_ids,
        status="waiting_upload",
        revision=1,
        available_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(child)
    thread = db.get(AssistantThread, run.thread_id)
    if thread is not None:
        thread.updated_at = now
    tenant_id = run.tenant_id
    requested_by_user_id = run.requested_by_user_id
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        concurrent = db.scalar(
            select(AssistantRequestRun).where(
                AssistantRequestRun.tenant_id == tenant_id,
                AssistantRequestRun.requested_by_user_id == requested_by_user_id,
                AssistantRequestRun.request_id == idempotency_key,
            )
        )
        if (
            concurrent is None
            or concurrent.parent_run_id != run_id
            or concurrent.request_fingerprint != fingerprint
        ):
            raise AssistantRequestConflict("助手重试并发登记冲突") from exc
        return _public(concurrent)
    db.refresh(child)
    return _public(child)


def cancel_request(db: Session, run_id: str, *, expected_revision: int) -> dict[str, Any]:
    run = _owned(db, run_id, writable=True)
    db.refresh(run, with_for_update=True)
    if run.revision != expected_revision:
        raise AssistantRequestConflict()
    if run.status not in {"waiting_upload", "queued"}:
        raise AssistantRequestConflict("只有尚未开始执行的助手请求可以取消")
    now = _now()
    run.status = "cancelled"
    run.revision += 1
    run.lease_token = ""
    run.lease_expires_at = None
    run.finished_at = now
    run.updated_at = now
    message = db.get(AssistantMessage, run.assistant_message_id)
    if message is not None and message.thread_id == run.thread_id:
        message.content = "本次助手请求已取消。"
        message.context = {
            **dict(message.context or {}),
            "status": "cancelled",
            "assistant_request_status": "cancelled",
            "assistant_request_revision": run.revision,
        }
    db.commit()
    db.refresh(run)
    return _public(run)


def assert_execution_lease(
    db: Session,
    run_id: str,
    *,
    lease_token: str,
    lease_generation: int,
    for_update: bool = False,
    scenario_verb: str = "read",
) -> None:
    assistant_request_worker_service.assert_execution_lease(
        db,
        run_id,
        lease_token=lease_token,
        lease_generation=lease_generation,
        for_update=for_update,
        scenario_verb=scenario_verb,
        conflict_factory=AssistantRequestConflict,
        authorize_run_thread=_authorize_run_thread,
    )


def _upload_requires_reupload(upload: ManagedUploadRun, *, as_of: datetime) -> bool:
    return assistant_request_worker_service.upload_requires_reupload(
        upload,
        as_of=as_of,
    )


def _upload_state(db: Session, run: AssistantRequestRun) -> tuple[str, str, str]:
    return assistant_request_worker_service.upload_state(db, run)


def process_request(
    run_id: str,
    executor: Callable[[dict[str, Any], str, str, str, str, int], None],
) -> bool:
    return assistant_request_worker_service.process_request(
        run_id,
        executor,
        lease_seconds=LEASE_SECONDS,
        session_factory=SessionLocal,
        upload_state_reader=_upload_state,
    )


def process_next_request(
    executor: Callable[[dict[str, Any], str, str, str, str, int], None],
) -> bool:
    return assistant_request_worker_service.process_next_request(
        executor,
        session_factory=SessionLocal,
        request_processor=process_request,
    )
