"""Transactional outbox for deleting durable objects after metadata commits."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import secrets
import threading
import uuid

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..models import AssistantAttachment, BucketFile, DataSource, ObjectDeletionJob
from ..config import get_settings
from . import datasource_service, object_storage_service


logger = logging.getLogger(__name__)
_ACTIVE_STATUSES = ("pending", "retry")
_UPLOAD_LEASE_STATUSES = ("uploading", "putting")
_ABANDONED_UPLOAD_ORIGIN = "abandoned_upload_version"


class UploadIntentLeaseLostError(RuntimeError):
    """The caller may not bind this object to authoritative metadata."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _job_id(
    provider: str,
    bucket_name: str,
    object_key: str,
    object_version_id: str,
    object_url: str,
) -> str:
    identity = "\0".join(
        ("delete", provider, bucket_name, object_key, object_version_id, object_url)
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _upload_intent_id(
    provider: str,
    bucket_name: str,
    object_key: str,
    object_url: str,
) -> str:
    identity = "\0".join(
        ("upload", provider, bucket_name, object_key, object_url)
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class UploadIntentClaim:
    job_id: str
    lease_token: str
    lease_generation: int
    provider: str
    bucket_name: str
    object_key: str
    object_url: str
    origin_type: str
    origin_id: str
    tenant_id: str | None
    scenario_id: str | None
    session_factory: Callable[[], Session] = field(repr=False, compare=False)


def _default_session_factory() -> Session:
    from ..database import SessionLocal

    return SessionLocal()


def _prepare_upload_intent(
    *,
    provider: str,
    bucket_name: str,
    object_key: str,
    object_url: str,
    origin_type: str,
    origin_id: str,
    tenant_id: str | None,
    scenario_id: str | None,
    session_factory: Callable[[], Session] | None = None,
) -> UploadIntentClaim:
    factory = session_factory or _default_session_factory
    job_id = _upload_intent_id(
        provider, bucket_name, object_key, object_url
    )
    timeout = int(get_settings().minio_upload_intent_timeout_seconds)
    for attempt in range(2):
        db = factory()
        try:
            job = db.scalar(
                select(ObjectDeletionJob)
                .where(ObjectDeletionJob.id == job_id)
                .with_for_update()
            )
            now = _now()
            token = secrets.token_hex(32)
            if job is None:
                job = ObjectDeletionJob(
                    id=job_id,
                    provider=provider,
                    bucket_name=bucket_name,
                    object_key=object_key,
                    object_version_id="",
                    object_url=object_url,
                    origin_type=origin_type,
                    origin_id=origin_id,
                    tenant_id=tenant_id,
                    scenario_id=scenario_id,
                    status="uploading",
                    attempts=0,
                    lease_token=token,
                    lease_generation=1,
                    last_error="",
                    next_attempt_at=now + timedelta(seconds=timeout),
                )
                db.add(job)
            else:
                raise RuntimeError("对象上传键已经使用，禁止再次建立上传事务")
            db.commit()
            return UploadIntentClaim(
                job_id=job_id,
                lease_token=token,
                lease_generation=int(job.lease_generation or 0),
                provider=provider,
                bucket_name=bucket_name,
                object_key=object_key,
                object_url=object_url,
                origin_type=origin_type,
                origin_id=origin_id,
                tenant_id=tenant_id,
                scenario_id=scenario_id,
                session_factory=factory,
            )
        except Exception as exc:
            db.rollback()
            # Two first-time claimants can race on the deterministic PK.  The
            # loser retries and then serializes on the existing row lock.
            from sqlalchemy.exc import IntegrityError

            if attempt == 0 and isinstance(exc, IntegrityError):
                continue
            raise
        finally:
            db.close()
    raise RuntimeError("无法建立上传意图")


def _fence_upload_key_for_deletion(
    db: Session,
    *,
    provider: str,
    bucket_name: str,
    object_key: str,
    object_url: str,
    origin_type: str,
    origin_id: str,
    tenant_id: str | None,
    scenario_id: str | None,
) -> str:
    guard_id = _upload_intent_id(
        provider, bucket_name, object_key, object_url
    )
    guard = db.scalar(
        select(ObjectDeletionJob)
        .where(ObjectDeletionJob.id == guard_id)
        .with_for_update()
    )
    now = _now()
    if guard is None:
        guard = ObjectDeletionJob(
            id=guard_id,
            provider=provider,
            bucket_name=bucket_name,
            object_key=object_key,
            object_version_id="",
            object_url=object_url,
            origin_type=origin_type,
            origin_id=origin_id,
            tenant_id=tenant_id,
            scenario_id=scenario_id,
            status="deleting",
            attempts=0,
            lease_token="",
            lease_generation=1,
            last_error="",
            next_attempt_at=now,
        )
        db.add(guard)
    else:
        if (
            guard.provider,
            guard.bucket_name,
            guard.object_key,
            guard.object_url,
        ) != (provider, bucket_name, object_key, object_url):
            raise ValueError("对象生命周期保护记录身份冲突")
        guard.origin_type = origin_type
        guard.origin_id = origin_id
        guard.tenant_id = tenant_id
        guard.scenario_id = scenario_id
        guard.status = "deleting"
        guard.lease_token = ""
        guard.lease_generation = int(guard.lease_generation or 0) + 1
        guard.last_error = ""
        guard.next_attempt_at = now
        guard.completed_at = None
        guard.updated_at = now
    return guard_id


def prepare_bucket_file_upload(
    data_source: DataSource,
    file_id: str,
    filename: str,
    *,
    session_factory: Callable[[], Session] | None = None,
) -> UploadIntentClaim:
    object_key = datasource_service.build_bucket_object_key(
        data_source,
        file_id,
        filename,
        upload_id=uuid.uuid4().hex,
    )
    bucket_name, _prefix = datasource_service.managed_minio_location(data_source)
    return _prepare_upload_intent(
        provider="minio",
        bucket_name=bucket_name,
        object_key=object_key,
        object_url=object_storage_service.stable_object_url(
            bucket_name, object_key
        ),
        origin_type="bucket_file_upload",
        origin_id=file_id,
        tenant_id=data_source.tenant_id,
        scenario_id=data_source.scenario_id,
        session_factory=session_factory,
    )


def prepare_assistant_attachment_upload(
    attachment: AssistantAttachment,
    *,
    session_factory: Callable[[], Session] | None = None,
) -> UploadIntentClaim:
    configured = object_storage_service.require_configuration()
    object_key = datasource_service.build_assistant_attachment_object_key(
        attachment.tenant_id,
        attachment.id,
        attachment.filename,
        upload_id=uuid.uuid4().hex,
    )
    return _prepare_upload_intent(
        provider="minio",
        bucket_name=configured.bucket_name,
        object_key=object_key,
        object_url=object_storage_service.stable_object_url(
            configured.bucket_name, object_key
        ),
        origin_type="assistant_attachment_upload",
        origin_id=attachment.id,
        tenant_id=attachment.tenant_id,
        scenario_id=None,
        session_factory=session_factory,
    )


def begin_upload_put(claim: UploadIntentClaim) -> None:
    """Atomically consume a claim before its one permitted object PUT."""
    db = claim.session_factory()
    try:
        job = db.scalar(
            select(ObjectDeletionJob)
            .where(ObjectDeletionJob.id == claim.job_id)
            .with_for_update()
        )
        if job is None or (
            job.provider,
            job.bucket_name,
            job.object_key,
            job.object_url,
        ) != (
            claim.provider,
            claim.bucket_name,
            claim.object_key,
            claim.object_url,
        ):
            raise UploadIntentLeaseLostError("上传意图不存在或对象身份已变化")
        if (
            job.status != "uploading"
            or job.lease_token != claim.lease_token
            or int(job.lease_generation or 0) != claim.lease_generation
        ):
            raise UploadIntentLeaseLostError(
                "上传意图已消费或回收，禁止重复写入对象"
            )
        job.status = "putting"
        job.updated_at = _now()
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _renew_upload_intent(claim: UploadIntentClaim) -> None:
    db = claim.session_factory()
    try:
        job = db.scalar(
            select(ObjectDeletionJob)
            .where(ObjectDeletionJob.id == claim.job_id)
            .with_for_update()
        )
        if (
            job is None
            or job.status not in _UPLOAD_LEASE_STATUSES
            or job.lease_token != claim.lease_token
            or int(job.lease_generation or 0) != claim.lease_generation
        ):
            raise RuntimeError("上传意图租约已失效")
        now = _now()
        job.next_attempt_at = now + timedelta(
            seconds=int(get_settings().minio_upload_intent_timeout_seconds)
        )
        job.updated_at = now
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


class UploadIntentHeartbeat:
    """Renew an upload lease while a potentially slow object PUT is running."""

    def __init__(self, claim: UploadIntentClaim) -> None:
        self.claim = claim
        self._stop = threading.Event()
        self._lost = threading.Event()
        timeout = int(get_settings().minio_upload_intent_timeout_seconds)
        self._interval = max(30, timeout // 3)
        self._thread = threading.Thread(
            target=self._run,
            name=f"object-upload-{claim.job_id[:8]}",
            daemon=True,
        )

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                _renew_upload_intent(self.claim)
            except Exception:  # noqa: BLE001 - the request fails closed below.
                self._lost.set()
                return

    def __enter__(self) -> "UploadIntentHeartbeat":
        self._thread.start()
        return self

    def assert_active(self) -> None:
        if self._lost.is_set():
            raise UploadIntentLeaseLostError(
                "上传意图续租失败，禁止提交对象元数据"
            )

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def heartbeat_upload_intent(claim: UploadIntentClaim) -> UploadIntentHeartbeat:
    return UploadIntentHeartbeat(claim)


def _retain_upload_intent(
    db: Session,
    claim: UploadIntentClaim,
    *,
    object_version_id: str,
    origin_type: str,
    origin_id: str,
) -> None:
    # Fence the lease before flushing the new metadata row. On lease loss the
    # caller can roll back without ever exposing or locking that row.
    with db.no_autoflush:
        job = db.scalar(
            select(ObjectDeletionJob)
            .where(ObjectDeletionJob.id == claim.job_id)
            .with_for_update()
        )
    expected = (
        claim.provider,
        claim.bucket_name,
        claim.object_key,
        claim.object_url,
    )
    if job is None or (
        job.provider,
        job.bucket_name,
        job.object_key,
        job.object_url,
    ) != expected:
        raise RuntimeError("上传意图不存在或对象身份已变化")
    if (
        job.status != "putting"
        or job.lease_token != claim.lease_token
        or int(job.lease_generation or 0) != claim.lease_generation
    ):
        raise UploadIntentLeaseLostError(
            "上传意图已被回收，禁止提交对象元数据"
        )
    now = _now()
    job.object_version_id = object_version_id
    job.origin_type = origin_type
    job.origin_id = origin_id
    job.status = "retained"
    job.lease_token = ""
    job.last_error = ""
    job.completed_at = now
    job.updated_at = now


def retain_bucket_file_upload(
    db: Session,
    claim: UploadIntentClaim,
    bucket_file: BucketFile,
    data_source: DataSource,
) -> None:
    bucket_name, object_key, _safe_name = (
        datasource_service.minio_file_identity(bucket_file, data_source)
    )
    if (
        claim.provider != "minio"
        or claim.bucket_name != bucket_name
        or claim.object_key != object_key
    ):
        raise RuntimeError("上传结果与上传意图不一致")
    _retain_upload_intent(
        db,
        claim,
        object_version_id=bucket_file.object_version_id,
        origin_type="bucket_file_upload",
        origin_id=bucket_file.id,
    )


def retain_assistant_attachment_upload(
    db: Session,
    claim: UploadIntentClaim,
    attachment: AssistantAttachment,
) -> None:
    identity = datasource_service.assistant_attachment_object_identity(attachment)
    if identity is None:
        raise RuntimeError("助手附件缺少托管对象身份")
    bucket_name, object_key, version_id = identity
    if claim.bucket_name != bucket_name or claim.object_key != object_key:
        raise RuntimeError("助手附件上传结果与上传意图不一致")
    _retain_upload_intent(
        db,
        claim,
        object_version_id=version_id,
        origin_type="assistant_attachment_upload",
        origin_id=attachment.id,
    )


def _enqueue(
    db: Session,
    *,
    provider: str,
    bucket_name: str,
    object_key: str,
    object_version_id: str,
    object_url: str,
    origin_type: str,
    origin_id: str,
    tenant_id: str | None,
    scenario_id: str | None,
) -> str:
    job_id = _job_id(
        provider,
        bucket_name,
        object_key,
        object_version_id,
        object_url,
    )
    existing = db.get(ObjectDeletionJob, job_id)
    if existing is not None:
        recorded = (
            existing.provider,
            existing.bucket_name,
            existing.object_key,
            existing.object_version_id,
            existing.object_url,
        )
        expected = (
            provider,
            bucket_name,
            object_key,
            object_version_id,
            object_url,
        )
        if recorded != expected:
            raise ValueError("对象删除任务身份冲突")
        if existing.status not in _ACTIVE_STATUSES:
            existing.status = "pending"
            existing.attempts = 0
            existing.last_error = ""
            existing.next_attempt_at = _now()
            existing.completed_at = None
        existing.origin_type = origin_type
        existing.origin_id = origin_id
        existing.tenant_id = tenant_id
        existing.scenario_id = scenario_id
        return job_id

    db.add(
        ObjectDeletionJob(
            id=job_id,
            provider=provider,
            bucket_name=bucket_name,
            object_key=object_key,
            object_version_id=object_version_id,
            object_url=object_url,
            origin_type=origin_type,
            origin_id=origin_id,
            tenant_id=tenant_id,
            scenario_id=scenario_id,
            status="pending",
            attempts=0,
            last_error="",
            next_attempt_at=_now(),
        )
    )
    return job_id


def enqueue_abandoned_upload_version(
    claim: UploadIntentClaim,
    *,
    object_version_id: str,
) -> str:
    """Persist cleanup for a completed PUT after its upload lease was lost."""
    version_id = str(object_version_id or "").strip()
    if not version_id and not datasource_service.is_generation_scoped_object_key(
        claim.object_key
    ):
        raise RuntimeError("迟到上传缺少不可变对象身份，拒绝模糊删除")
    db = claim.session_factory()
    try:
        guard = db.scalar(
            select(ObjectDeletionJob)
            .where(ObjectDeletionJob.id == claim.job_id)
            .with_for_update()
        )
        if guard is None or (
            guard.provider,
            guard.bucket_name,
            guard.object_key,
            guard.object_url,
        ) != (
            claim.provider,
            claim.bucket_name,
            claim.object_key,
            claim.object_url,
        ):
            raise RuntimeError("迟到上传与生命周期保护记录不一致")
        if int(guard.lease_generation or 0) < claim.lease_generation:
            raise RuntimeError("迟到上传 generation 无效")
        job_id = _enqueue(
            db,
            provider=claim.provider,
            bucket_name=claim.bucket_name,
            object_key=claim.object_key,
            object_version_id=version_id,
            object_url=claim.object_url,
            origin_type=_ABANDONED_UPLOAD_ORIGIN,
            origin_id=claim.origin_id,
            tenant_id=claim.tenant_id,
            scenario_id=claim.scenario_id,
        )
        db.commit()
        return job_id
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _schedule_abandoned_record_best_effort(
    claim: UploadIntentClaim,
    record: BucketFile | AssistantAttachment,
) -> None:
    if not datasource_service.managed_object_was_created(record):
        return
    try:
        enqueue_abandoned_upload_version(
            claim,
            object_version_id=str(
                getattr(record, "object_version_id", "") or ""
            ),
        )
    except Exception:  # noqa: BLE001 - the delayed final sweep is the fallback.
        logger.exception("迟到上传精确对象删除任务登记失败")


def schedule_abandoned_upload_best_effort(
    claim: UploadIntentClaim,
    record: BucketFile | AssistantAttachment,
) -> None:
    """Register post-rollback cleanup for a PUT whose retain lease was lost."""
    _schedule_abandoned_record_best_effort(claim, record)


def assert_upload_active(
    heartbeat: UploadIntentHeartbeat,
    claim: UploadIntentClaim,
    record: BucketFile | AssistantAttachment,
) -> None:
    try:
        heartbeat.assert_active()
    except UploadIntentLeaseLostError:
        _schedule_abandoned_record_best_effort(claim, record)
        raise


def enqueue_bucket_file_deletion(
    db: Session,
    bucket_file: BucketFile,
    data_source: DataSource,
) -> str:
    provider, bucket_or_path, object_key, version_id = (
        datasource_service.bucket_file_deletion_identity(bucket_file, data_source)
    )
    if provider == "minio":
        bucket_name = bucket_or_path
        object_url = object_storage_service.stable_object_url(
            bucket_name, object_key
        )
        _fence_upload_key_for_deletion(
            db,
            provider="minio",
            bucket_name=bucket_name,
            object_key=object_key,
            object_url=object_url,
            origin_type="bucket_file_delete",
            origin_id=bucket_file.id,
            tenant_id=data_source.tenant_id,
            scenario_id=data_source.scenario_id,
        )
    else:
        bucket_name = ""
        object_url = bucket_or_path
    return _enqueue(
        db,
        provider=provider,
        bucket_name=bucket_name,
        object_key=object_key,
        object_version_id=version_id,
        object_url=object_url,
        origin_type="bucket_file",
        origin_id=bucket_file.id,
        tenant_id=data_source.tenant_id,
        scenario_id=data_source.scenario_id,
    )


def enqueue_assistant_attachment_deletion(
    db: Session,
    attachment: AssistantAttachment,
) -> str | None:
    identity = datasource_service.assistant_attachment_object_identity(attachment)
    if identity is None:
        return None
    bucket_name, object_key, version_id = identity
    object_url = object_storage_service.stable_object_url(
        bucket_name, object_key
    )
    _fence_upload_key_for_deletion(
        db,
        provider="minio",
        bucket_name=bucket_name,
        object_key=object_key,
        object_url=object_url,
        origin_type="assistant_attachment_delete",
        origin_id=attachment.id,
        tenant_id=attachment.tenant_id,
        scenario_id=None,
    )
    return _enqueue(
        db,
        provider="minio",
        bucket_name=bucket_name,
        object_key=object_key,
        object_version_id=version_id,
        object_url=object_url,
        origin_type="assistant_attachment",
        origin_id=attachment.id,
        tenant_id=attachment.tenant_id,
        scenario_id=None,
    )


def _delete_job_object(job: ObjectDeletionJob) -> None:
    if job.provider == "minio":
        bucket_name, object_key = object_storage_service.parse_object_url(
            job.object_url
        )
        if bucket_name != job.bucket_name or object_key != job.object_key:
            raise ValueError("对象删除任务字段与地址不一致")
        object_storage_service.delete_object(
            bucket_name,
            object_key,
            version_id=job.object_version_id,
        )
        return
    if job.provider == "local":
        datasource_service.delete_bucket_file_path(job.object_url)
        return
    raise ValueError("对象删除任务存储提供方无效")


def _upload_object_is_referenced(db: Session, job: ObjectDeletionJob) -> bool:
    if job.provider != "minio":
        return False
    bucket_file_id = db.scalar(
        select(BucketFile.id)
        .where(
            BucketFile.storage_provider == "minio",
            BucketFile.bucket_name == job.bucket_name,
            BucketFile.object_key == job.object_key,
        )
        .limit(1)
    )
    if bucket_file_id is not None:
        return True
    attachment_id = db.scalar(
        select(AssistantAttachment.id)
        .where(
            AssistantAttachment.storage_provider == "minio",
            AssistantAttachment.bucket_name == job.bucket_name,
            AssistantAttachment.object_key == job.object_key,
        )
        .limit(1)
    )
    return attachment_id is not None


def _upload_version_is_referenced(db: Session, job: ObjectDeletionJob) -> bool:
    if job.provider != "minio" or not job.object_version_id:
        return False
    bucket_file_id = db.scalar(
        select(BucketFile.id)
        .where(
            BucketFile.storage_provider == "minio",
            BucketFile.bucket_name == job.bucket_name,
            BucketFile.object_key == job.object_key,
            BucketFile.object_version_id == job.object_version_id,
        )
        .limit(1)
    )
    if bucket_file_id is not None:
        return True
    attachment_id = db.scalar(
        select(AssistantAttachment.id)
        .where(
            AssistantAttachment.storage_provider == "minio",
            AssistantAttachment.bucket_name == job.bucket_name,
            AssistantAttachment.object_key == job.object_key,
            AssistantAttachment.object_version_id == job.object_version_id,
        )
        .limit(1)
    )
    return attachment_id is not None


def _lock_upload_guard_for_abandoned_version(
    db: Session,
    job: ObjectDeletionJob,
) -> ObjectDeletionJob | None:
    guard_id = _upload_intent_id(
        job.provider,
        job.bucket_name,
        job.object_key,
        job.object_url,
    )
    guard = db.scalar(
        select(ObjectDeletionJob)
        .where(ObjectDeletionJob.id == guard_id)
        .with_for_update(skip_locked=True)
    )
    if guard is None:
        return None
    if (
        guard.provider,
        guard.bucket_name,
        guard.object_key,
        guard.object_url,
    ) != (
        job.provider,
        job.bucket_name,
        job.object_key,
        job.object_url,
    ):
        raise RuntimeError("迟到上传任务与生命周期保护记录不一致")
    return guard


def _lock_upload_guard_for_delete(
    db: Session, job: ObjectDeletionJob
) -> ObjectDeletionJob | None:
    if job.provider != "minio":
        return None
    guard_id = _upload_intent_id(
        job.provider,
        job.bucket_name,
        job.object_key,
        job.object_url,
    )
    guard = db.scalar(
        select(ObjectDeletionJob)
        .where(ObjectDeletionJob.id == guard_id)
        .with_for_update()
    )
    if guard is None:
        raise RuntimeError("对象删除任务缺少上传生命周期保护记录")
    if (
        guard.provider,
        guard.bucket_name,
        guard.object_key,
        guard.object_url,
    ) != (
        job.provider,
        job.bucket_name,
        job.object_key,
        job.object_url,
    ):
        raise RuntimeError("对象删除任务与生命周期保护记录不一致")
    guard.status = "deleting"
    guard.lease_token = ""
    guard.updated_at = _now()
    return guard


def process_object_deletion_jobs(
    db: Session,
    *,
    limit: int = 100,
    job_ids: list[str] | tuple[str, ...] | None = None,
) -> int:
    """Drain a bounded batch with generation fencing for upload recovery."""
    bounded_limit = max(1, min(int(limit), 1000))
    normalized_ids = (
        sorted({str(item) for item in job_ids if str(item)})
        if job_ids is not None
        else None
    )
    if job_ids is not None:
        if not normalized_ids:
            return 0

    completed = 0
    processed_ids: set[str] = set()
    for _index in range(bounded_limit):
        now = _now()
        statement = (
            select(ObjectDeletionJob)
            .where(
                or_(
                    and_(
                        ObjectDeletionJob.status.in_(_ACTIVE_STATUSES),
                        ObjectDeletionJob.next_attempt_at <= now,
                    ),
                    and_(
                        ObjectDeletionJob.status.in_(
                            (
                                "uploading",
                                "putting",
                                "reclaiming",
                                "reclaim_wait",
                                "cleanup_retry",
                            )
                        ),
                        ObjectDeletionJob.next_attempt_at <= now,
                    ),
                ),
                ObjectDeletionJob.id.not_in(processed_ids),
            )
            .order_by(
                ObjectDeletionJob.next_attempt_at,
                ObjectDeletionJob.created_at,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if normalized_ids is not None:
            statement = statement.where(
                ObjectDeletionJob.id.in_(normalized_ids)
            )
        job = db.scalar(statement)
        if job is None:
            break
        processed_ids.add(job.id)

        if job.origin_type == _ABANDONED_UPLOAD_ORIGIN:
            generation_scoped = datasource_service.is_generation_scoped_object_key(
                job.object_key
            )
            if job.provider != "minio" or (
                not job.object_version_id and not generation_scoped
            ):
                job.status = "retry"
                job.attempts = int(job.attempts or 0) + 1
                job.last_error = "迟到上传精确对象删除任务身份无效"
                job.next_attempt_at = now + timedelta(minutes=5)
                job.updated_at = now
                db.commit()
                continue
            guard = _lock_upload_guard_for_abandoned_version(db, job)
            if guard is None or guard.status in {
                "uploading",
                "putting",
                "reclaiming",
                "reclaim_wait",
                "cleanup_retry",
                "deleting",
            }:
                job.status = "retry"
                job.last_error = "迟到上传等待对象生命周期稳定"
                job.next_attempt_at = now + timedelta(seconds=5)
                job.updated_at = now
                db.commit()
                continue
            referenced = (
                _upload_version_is_referenced(db, job)
                if job.object_version_id
                else _upload_object_is_referenced(db, job)
            )
            if referenced:
                job.status = "completed"
                job.last_error = ""
                job.completed_at = now
                job.updated_at = now
                completed += 1
                db.commit()
                continue
            try:
                bucket_name, object_key = object_storage_service.parse_object_url(
                    job.object_url
                )
                if bucket_name != job.bucket_name or object_key != job.object_key:
                    raise ValueError("迟到上传删除任务字段与地址不一致")
                object_storage_service.delete_object(
                    bucket_name,
                    object_key,
                    version_id=job.object_version_id,
                )
            except Exception:  # noqa: BLE001 - exact-object deletion is retryable.
                job.status = "retry"
                job.attempts = int(job.attempts or 0) + 1
                job.last_error = "迟到上传精确对象删除失败，等待重试"
                job.next_attempt_at = now + timedelta(
                    seconds=min(300, 2 ** min(job.attempts, 8))
                )
                job.updated_at = now
            else:
                job.status = "completed"
                job.last_error = ""
                job.completed_at = now
                job.updated_at = now
                completed += 1
            db.commit()
            continue

        if job.status in {
            "uploading",
            "putting",
            "reclaiming",
            "reclaim_wait",
            "cleanup_retry",
        }:
            final_sweep = job.status == "reclaim_wait"
            if _upload_object_is_referenced(db, job):
                job.status = "retained"
                job.lease_token = ""
                job.last_error = ""
                job.completed_at = now
                job.updated_at = now
                db.commit()
                continue
            if job.provider != "minio":
                job.status = "cleanup_retry"
                job.lease_token = ""
                job.last_error = "上传意图存储提供方无效"
                job.next_attempt_at = now + timedelta(minutes=5)
                job.updated_at = now
                db.commit()
                continue

            # Commit an irreversible generation fence before touching MinIO.
            # A crashed or delayed uploader can no longer finalize this token,
            # even when the external delete succeeds and its response is lost.
            job.status = "reclaiming"
            job.lease_token = ""
            job.lease_generation = int(job.lease_generation or 0) + 1
            job.next_attempt_at = now + timedelta(minutes=5)
            job.updated_at = now
            reclaim_id = job.id
            reclaim_generation = job.lease_generation
            bucket_name = job.bucket_name
            object_key = job.object_key
            db.commit()
            cleanup_error = False
            try:
                object_storage_service.delete_all_object_versions(
                    bucket_name, object_key
                )
            except Exception:  # noqa: BLE001 - fenced cleanup is retried.
                cleanup_error = True
            fenced = db.scalar(
                select(ObjectDeletionJob)
                .where(ObjectDeletionJob.id == reclaim_id)
                .with_for_update()
            )
            if (
                fenced is None
                or fenced.status != "reclaiming"
                or fenced.lease_generation != reclaim_generation
            ):
                db.rollback()
                raise RuntimeError("上传回收 generation 在处理期间发生变化")
            fenced.attempts = int(fenced.attempts or 0) + int(cleanup_error)
            fenced.lease_token = ""
            fenced.updated_at = _now()
            if cleanup_error:
                fenced.status = "cleanup_retry"
                fenced.last_error = "对象存储上传残留清理失败，等待重试"
                fenced.next_attempt_at = _now() + timedelta(
                    seconds=min(300, 2 ** min(fenced.attempts, 8))
                )
            else:
                fenced.last_error = ""
                if final_sweep:
                    fenced.status = "completed"
                    fenced.completed_at = _now()
                    completed += 1
                else:
                    fenced.status = "reclaim_wait"
                    fenced.completed_at = None
                    fenced.next_attempt_at = _now() + timedelta(
                        seconds=int(
                            get_settings().minio_late_put_cleanup_grace_seconds
                        )
                    )
            db.commit()
            continue

        guard = _lock_upload_guard_for_delete(db, job)
        try:
            _delete_job_object(job)
        except Exception:  # noqa: BLE001 - persist a safe retry state only.
            job.attempts = int(job.attempts or 0) + 1
            job.status = "retry"
            job.last_error = "对象存储删除失败，等待重试"
            job.next_attempt_at = now + timedelta(
                seconds=min(300, 2 ** min(job.attempts, 8))
            )
            job.updated_at = now
        else:
            job.status = "completed"
            job.last_error = ""
            job.completed_at = now
            job.updated_at = now
            if guard is not None:
                guard.status = "completed"
                guard.last_error = ""
                guard.completed_at = now
                guard.updated_at = now
            completed += 1
        db.commit()
    return completed


def drain_jobs_best_effort(db: Session, job_ids: list[str]) -> int:
    """Try immediate cleanup after commit; durable jobs remain on any failure."""
    if not job_ids:
        return 0
    try:
        return process_object_deletion_jobs(
            db,
            limit=len(job_ids),
            job_ids=job_ids,
        )
    except Exception:  # noqa: BLE001 - the already-committed outbox is authoritative.
        db.rollback()
        logger.exception("对象删除任务即时处理失败，将由后台任务重试")
        return 0
