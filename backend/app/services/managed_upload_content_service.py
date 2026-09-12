"""Durable request-body intake and object-storage writes for managed uploads."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
import threading
from typing import Any, Callable, Iterator
import uuid

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import BucketFile, DataSource, ManagedUploadRun
from . import agent_scope_access_service, datasource_service, object_deletion_service
from .managed_upload_run_contracts import (
    ContentUploadSnapshot,
    ManagedUploadConflict,
    ManagedUploadError,
    UploadLease,
    as_utc,
    lease_matches,
    utc_now,
)


CONTENT_RETRY_GRACE_SECONDS = 10


def _require_run_write_scope(
    db: Session,
    owner_agent_id: str | None,
    *,
    lock: bool = False,
) -> None:
    try:
        agent_scope_access_service.require_optional_agent_permission(
            db,
            owner_agent_id,
            "write",
            lock=lock,
            message="没有该 Agent 所属业务场景的附件写入权限",
        )
    except agent_scope_access_service.AgentScopeNotFoundError as exc:
        raise ManagedUploadError(
            "managed_upload_owner_unavailable",
            "所属 Agent 已删除，附件上传已取消",
            status_code=409,
        ) from exc


def preflight_content_upload(
    db: Session,
    run_id: str,
    *,
    expected_revision: int,
    agent_id: str | None = None,
    owned_run: Callable[..., ManagedUploadRun],
) -> int:
    """Authorize and bound an upload before the request body is consumed."""

    run = (
        owned_run(db, run_id, writable=True, agent_id=agent_id)
        if agent_id is not None
        else owned_run(db, run_id, writable=True)
    )
    now = utc_now()
    if run.revision != expected_revision:
        raise ManagedUploadConflict()
    if run.status == "ready" or run.bucket_file_id:
        raise ManagedUploadConflict("该上传任务已接收文件内容")
    if run.status not in {"awaiting_upload", "failed"}:
        raise ManagedUploadConflict("该上传任务当前不能接收文件内容")
    if (as_utc(run.expires_at) or now) <= now:
        raise ManagedUploadError(
            "managed_upload_expired",
            "上传任务已过期",
            status_code=410,
        )
    return int(run.declared_byte_size)


def claim_content_upload(
    db: Session,
    run_id: str,
    *,
    expected_revision: int,
    agent_id: str | None = None,
    owned_run: Callable[..., ManagedUploadRun],
) -> UploadLease:
    run = (
        owned_run(db, run_id, writable=True, lock=True, agent_id=agent_id)
        if agent_id is not None
        else owned_run(db, run_id, writable=True, lock=True)
    )
    now = utc_now()
    if run.revision != expected_revision:
        raise ManagedUploadConflict()
    if run.status == "ready" or run.bucket_file_id:
        raise ManagedUploadConflict("该上传任务已接收文件内容")
    if run.status not in {"awaiting_upload", "failed"}:
        raise ManagedUploadConflict("该上传任务当前不能接收文件内容")
    if (as_utc(run.expires_at) or now) <= now:
        run.status = "cancelled"
        run.revision += 1
        run.error_code = "managed_upload_expired"
        run.error_message = "上传任务已过期"
        run.finished_at = now
        run.updated_at = now
        db.commit()
        raise ManagedUploadError(
            "managed_upload_expired",
            "上传任务已过期",
            status_code=410,
        )
    token = uuid.uuid4().hex
    timeout = int(get_settings().minio_upload_intent_timeout_seconds)
    run.status = "uploading"
    run.revision += 1
    run.lease_generation += 1
    run.lease_token = token
    run.lease_expires_at = now + timedelta(seconds=timeout)
    run.error_code = ""
    run.error_message = ""
    run.finished_at = None
    run.updated_at = now
    db.commit()
    return UploadLease(token, run.lease_generation, run.lease_expires_at)


def renew_lease(
    run_id: str,
    lease: UploadLease,
    status: str,
    seconds: int,
    *,
    session_factory: Callable[[], Session],
) -> bool:
    db = session_factory()
    try:
        now = utc_now()
        result = db.execute(
            update(ManagedUploadRun)
            .where(
                ManagedUploadRun.id == run_id,
                ManagedUploadRun.status == status,
                ManagedUploadRun.lease_token == lease.token,
                ManagedUploadRun.lease_generation == lease.generation,
                ManagedUploadRun.lease_expires_at.is_not(None),
                ManagedUploadRun.lease_expires_at > now,
            )
            .values(lease_expires_at=now + timedelta(seconds=seconds), updated_at=now)
        )
        db.commit()
        return bool(result.rowcount)
    finally:
        db.close()


@contextmanager
def lease_heartbeat(
    run_id: str,
    lease: UploadLease,
    *,
    status: str,
    seconds: int,
    renew: Callable[[str, UploadLease, str, int], bool],
) -> Iterator[threading.Event]:
    stopped = threading.Event()
    lost = threading.Event()

    def heartbeat() -> None:
        while not stopped.wait(max(1.0, seconds / 3)):
            try:
                if not renew(run_id, lease, status, seconds):
                    lost.set()
                    return
            except Exception:  # noqa: BLE001 - final writes remain fenced.
                lost.set()
                return

    thread = threading.Thread(
        target=heartbeat,
        name=f"managed-upload-heartbeat-{run_id[:8]}",
        daemon=True,
    )
    thread.start()
    try:
        yield lost
    finally:
        stopped.set()
        thread.join(timeout=1.0)


def reset_content_claim(
    run_id: str,
    lease: UploadLease,
    *,
    session_factory: Callable[[], Session],
) -> None:
    db = session_factory()
    try:
        run = db.scalar(
            select(ManagedUploadRun)
            .where(ManagedUploadRun.id == run_id)
            .with_for_update()
        )
        if run is None or run.status != "uploading" or not lease_matches(
            run, lease, as_of=utc_now()
        ):
            db.rollback()
            return
        now = utc_now()
        run.status = "awaiting_upload"
        run.revision += 1
        run.lease_token = ""
        run.lease_expires_at = None
        run.error_code = "managed_upload_content_failed"
        run.error_message = "文件内容上传失败，请重试"
        run.available_at = now + timedelta(seconds=CONTENT_RETRY_GRACE_SECONDS)
        run.updated_at = now
        db.commit()
    finally:
        db.close()


def store_uploaded_content(
    run_id: str,
    lease: UploadLease,
    source_path: str | Path,
    *,
    content_sha256: str,
    byte_size: int,
    session_factory: Callable[[], Session],
    public_run: Callable[[Session, ManagedUploadRun], dict[str, Any]],
    heartbeat_factory: Callable[..., Any],
    reset_claim: Callable[[str, UploadLease], None],
) -> dict[str, Any]:
    """Persist raw bytes only; schema profiling is deliberately worker-owned."""
    upload_claim = None
    bucket_file: BucketFile | None = None
    try:
        path = Path(source_path).resolve(strict=True)
        if not path.is_file() or path.stat().st_size != byte_size:
            raise ManagedUploadError(
                "managed_upload_staging_invalid",
                "上传暂存文件无效",
                status_code=422,
            )
        db = session_factory()
        try:
            run = db.get(ManagedUploadRun, run_id)
            now = utc_now()
            if (
                run is None
                or not run.requested_by_user_id
                or not lease_matches(run, lease, as_of=now)
            ):
                raise ManagedUploadConflict("上传任务租约已失效")
            if byte_size != run.declared_byte_size:
                raise ManagedUploadError(
                    "managed_upload_size_mismatch",
                    "文件实际大小与登记值不一致",
                    status_code=422,
            )
            db.info["tenant_id"] = run.tenant_id
            db.info["user_id"] = run.requested_by_user_id
            _require_run_write_scope(db, run.owner_agent_id)
            source = db.scalar(
                select(DataSource).where(
                    DataSource.id == run.data_source_id,
                    DataSource.tenant_id == run.tenant_id,
                )
            )
            if source is None:
                raise ManagedUploadError(
                    "managed_upload_storage_unavailable",
                    "受管上传存储不可用",
                    status_code=503,
                )
            db.expunge(source)
            snapshot = ContentUploadSnapshot(
                tenant_id=run.tenant_id,
                user_id=run.requested_by_user_id,
                data_source_id=run.data_source_id,
                filename=run.filename,
                client_media_type=run.client_media_type,
                source=source,
            )
            db.rollback()
        finally:
            db.close()
        file_id = uuid.uuid4().hex
        upload_claim = object_deletion_service.prepare_bucket_file_upload(
            snapshot.source,
            file_id,
            snapshot.filename,
        )
        timeout = int(get_settings().minio_upload_intent_timeout_seconds)
        with heartbeat_factory(
            run_id,
            lease,
            status="uploading",
            seconds=timeout,
        ) as run_lease_lost:
            with object_deletion_service.heartbeat_upload_intent(
                upload_claim
            ) as object_heartbeat:
                object_deletion_service.begin_upload_put(upload_claim)
                bucket_file = datasource_service.save_bucket_file_path(
                    snapshot.source,
                    snapshot.filename,
                    path,
                    mime=snapshot.client_media_type or None,
                    stable_file_id=file_id,
                    upload_object_key=upload_claim.object_key,
                    content_sha256=content_sha256,
                )
                object_deletion_service.assert_upload_active(
                    object_heartbeat,
                    upload_claim,
                    bucket_file,
                )
            if run_lease_lost.is_set():
                raise ManagedUploadConflict("上传任务租约已失效")
        db = session_factory()
        try:
            db.info["tenant_id"] = snapshot.tenant_id
            db.info["user_id"] = snapshot.user_id
            owner_agent_id = db.scalar(
                select(ManagedUploadRun.owner_agent_id).where(
                    ManagedUploadRun.id == run_id,
                    ManagedUploadRun.tenant_id == snapshot.tenant_id,
                )
            )
            _require_run_write_scope(
                db,
                owner_agent_id,
                lock=bool(owner_agent_id),
            )
            current = db.scalar(
                select(ManagedUploadRun)
                .where(
                    ManagedUploadRun.id == run_id,
                    ManagedUploadRun.tenant_id == snapshot.tenant_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            source = db.scalar(
                select(DataSource).where(
                    DataSource.id == snapshot.data_source_id,
                    DataSource.tenant_id == snapshot.tenant_id,
                )
            )
            if (
                current is None
                or source is None
                or not lease_matches(current, lease, as_of=utc_now())
                or str(current.owner_agent_id or "") != str(owner_agent_id or "")
            ):
                raise ManagedUploadConflict("上传任务租约已失效")
            bucket_file.status = "pending"
            bucket_file.error = ""
            bucket_file.parsed_text = ""
            db.add(bucket_file)
            object_deletion_service.retain_bucket_file_upload(
                db,
                upload_claim,
                bucket_file,
                source,
            )
            db.flush()
            current.bucket_file_id = bucket_file.id
            current.byte_size = byte_size
            current.content_sha256 = content_sha256
            current.status = "stored"
            current.revision += 1
            current.lease_token = ""
            current.lease_expires_at = None
            current.available_at = utc_now()
            current.error_code = ""
            current.error_message = ""
            current.updated_at = utc_now()
            db.commit()
            return public_run(db, current)
        finally:
            db.close()
    except Exception:
        if upload_claim is not None and bucket_file is not None:
            object_deletion_service.schedule_abandoned_upload_best_effort(
                upload_claim,
                bucket_file,
            )
        reset_claim(run_id, lease)
        raise


def recover_upload_runs(
    *,
    limit: int = 100,
    session_factory: Callable[[], Session],
) -> int:
    """Release abandoned body leases under a row lock without racing a live PUT."""
    db = session_factory()
    recovered = 0
    try:
        now = utc_now()
        runs = list(
            db.scalars(
                select(ManagedUploadRun)
                .where(
                    ManagedUploadRun.status == "uploading",
                    ManagedUploadRun.lease_expires_at.is_not(None),
                    ManagedUploadRun.lease_expires_at <= now,
                    ManagedUploadRun.bucket_file_id.is_(None),
                )
                .order_by(ManagedUploadRun.lease_expires_at, ManagedUploadRun.id)
                .with_for_update(skip_locked=True)
                .limit(max(1, min(limit, 100)))
            )
        )
        for run in runs:
            run.status = "awaiting_upload"
            run.revision += 1
            run.lease_token = ""
            run.lease_expires_at = None
            run.error_code = "managed_upload_interrupted"
            run.error_message = "文件上传中断，请重试"
            run.available_at = now + timedelta(seconds=CONTENT_RETRY_GRACE_SECONDS)
            run.updated_at = now
            recovered += 1
        db.commit()
        return recovered
    finally:
        db.close()
