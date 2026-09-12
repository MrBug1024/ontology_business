"""Durable upload intake and background, format-generic catalog profiling."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import threading
from typing import Any, Iterator, Mapping
import uuid

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..catalog_schemas import CatalogManagedUploadMetadata
from ..config import get_settings
from ..database import SessionLocal
from ..managed_upload_schemas import ManagedUploadCreateIn
from ..models import (
    BucketFile,
    Conversation,
    DataAsset,
    DataAssetVersion,
    DataSource,
    ManagedUploadRun,
)
from . import (
    agent_scope_access_service,
    catalog_ingestion_service,
    catalog_service,
    datasource_service,
    doc_parser,
    managed_asset_lifecycle,
    object_deletion_service,
    object_storage_service,
    permission_service,
    tenant_service,
)
from . import managed_attachment_access
from . import managed_upload_content_service, managed_upload_processing_service
from .managed_upload_run_contracts import (
    ContentUploadSnapshot,
    ManagedInvocationAttachment,
    ManagedUploadConflict,
    ManagedUploadError,
    UploadLease,
    UploadProcessingSnapshot,
    as_utc,
    lease_matches,
    utc_now,
)


PROCESSING_LEASE_SECONDS = managed_upload_processing_service.PROCESSING_LEASE_SECONDS
CONTENT_RETRY_GRACE_SECONDS = managed_upload_content_service.CONTENT_RETRY_GRACE_SECONDS
_now = utc_now
_as_utc = as_utc


def _resolve_agent_scope(
    db: Session,
    payload: ManagedUploadCreateIn,
    *,
    tenant_id: str,
    user_id: str,
) -> str | None:
    """Authorize an optional Agent/conversation upload scope.

    Global Assistant callers intentionally omit the scope.  Agent callers may
    upload before a conversation exists, so ``conversation_id`` is validated
    when present but is not persisted as ownership; the durable Agent owner is
    the isolation boundary.
    """
    if payload.agent_id is None:
        if payload.conversation_id:
            raise ManagedUploadError(
                "invalid_managed_upload_scope",
                "conversation_id 必须与 agent_id 一起提供",
                status_code=422,
            )
        permission_service.require_tenant_permission(db, "write")
        return None
    return _authorize_agent_scope(
        db,
        payload.agent_id,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=payload.conversation_id,
        active_runtime=True,
        permission_verb="write",
        lock=True,
    )


def _authorize_agent_scope(
    db: Session,
    agent_id: str,
    *,
    tenant_id: str,
    user_id: str,
    conversation_id: str | None = None,
    active_runtime: bool = False,
    permission_verb: str = "read",
    lock: bool = False,
) -> str:
    """Authorize an existing Agent scope for a follow-up upload operation.

    Creation validates the full request DTO through ``_resolve_agent_scope``;
    status/retry/cancel/content endpoints only receive the scope token, so
    they use this smaller helper.  The same tenant and scenario ACL checks are
    deliberately repeated at each boundary instead of trusting a browser
    supplied owner id.
    """

    if not agent_id:
        raise ManagedUploadError(
            "invalid_managed_upload_scope",
            "agent_id 不能为空",
            status_code=422,
        )
    if permission_verb not in {"read", "write"}:
        raise ManagedUploadError(
            "invalid_managed_upload_scope",
            "不支持的 Agent 权限动作",
            status_code=422,
        )
    principal = permission_service.require_principal(db)
    if principal.tenant_id != tenant_id or principal.user_id != user_id:
        raise ManagedUploadError(
            "managed_upload_unavailable",
            "上传作用域不存在或无权使用",
            status_code=404,
        )
    try:
        # Upload-run creation is an ownership write.  Keep the Agent row lock
        # through the INSERT/commit so deletion cannot pass its owner scan and
        # then leave a newly-created run behind.  Follow-up reads/writes use
        # the durable run lock and lease CAS instead of holding this lock.
        agent = agent_scope_access_service.require_optional_agent_permission(
            db,
            agent_id,
            permission_verb,
            lock=lock,
            message="没有该 Agent 所属业务场景的权限",
        )
    except agent_scope_access_service.AgentScopeNotFoundError as exc:
        raise ManagedUploadError(
            "managed_upload_unavailable",
            "Agent 不存在或无权使用",
            status_code=404,
        ) from exc
    if agent is None:
        raise ManagedUploadError(
            "invalid_managed_upload_scope",
            "agent_id 不能为空",
            status_code=422,
        )
    if agent.scenario_id:
        scenario = tenant_service.require_scenario(db, agent.scenario_id)
        if active_runtime and scenario.status == "retired":
            raise ManagedUploadError(
                "scenario_retired",
                "业务场景已退役，不能创建新的附件上传任务",
                status_code=409,
            )
    if conversation_id:
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.agent_id == agent.id,
                Conversation.created_by_user_id == user_id,
            )
        )
        if conversation is None:
            raise ManagedUploadError(
                "managed_upload_unavailable",
                "对话不存在或无权使用",
                status_code=404,
            )
    return agent.id


def _canonical_hash(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ManagedUploadError(
            "invalid_managed_upload",
            "上传任务元数据必须是有限 JSON",
            status_code=422,
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _metadata(run: ManagedUploadRun) -> CatalogManagedUploadMetadata:
    document = dict(run.metadata_document or {})
    return CatalogManagedUploadMetadata.model_validate(
        {
            **document,
            "file_bucket_id": run.data_source_id,
            "purpose": run.purpose,
        }
    )


def _owned_run(
    db: Session,
    run_id: str,
    *,
    writable: bool = False,
    lock: bool = False,
    agent_id: str | None = None,
) -> ManagedUploadRun:
    principal = permission_service.require_principal(db)
    # An omitted scope is the Global Assistant namespace.  It must never be a
    # wildcard over Agent-owned rows; the owner NULL predicate is intentional.
    owner_clause = ManagedUploadRun.owner_agent_id.is_(None)
    if agent_id is not None:
        _authorize_agent_scope(
            db,
            agent_id,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            permission_verb="write" if writable else "read",
        )
        owner_clause = ManagedUploadRun.owner_agent_id == agent_id
    else:
        permission_service.require_tenant_permission(
            db, "write" if writable else "read"
        )
    statement = select(ManagedUploadRun).where(
        ManagedUploadRun.id == run_id,
        ManagedUploadRun.tenant_id == principal.tenant_id,
        ManagedUploadRun.requested_by_user_id == principal.user_id,
        # Agent deletion keeps a compact, fenced audit row on PostgreSQL, but
        # that tombstone is no longer a readable upload run.  Filtering it at
        # the ownership lookup prevents guessed ids from exposing lifecycle
        # metadata or re-entering a retry path.
        ManagedUploadRun.error_code != "agent_deleted",
        owner_clause,
    )
    if lock:
        statement = statement.with_for_update()
    run = db.scalar(statement.execution_options(populate_existing=True))
    if run is None:
        raise ManagedUploadError(
            "managed_upload_unavailable",
            "上传任务不存在",
            status_code=404,
        )
    return run


def _public_run(db: Session, run: ManagedUploadRun) -> dict[str, Any]:
    result = None
    owner_agent_id = getattr(run, "owner_agent_id", None)
    if run.status == "ready" and run.asset_id and run.asset_version_id:
        asset = db.get(DataAsset, run.asset_id)
        version = db.get(DataAssetVersion, run.asset_version_id)
        # The compatibility fallbacks keep old unit fixtures and pre-ownership
        # rows readable while real ORM rows still have to prove every pointer
        # in the immutable upload chain.
        run_file_id = getattr(run, "bucket_file_id", None)
        version_file_id = getattr(version, "bucket_file_id", None) if version else None
        run_source_id = getattr(run, "data_source_id", None)
        version_source_id = (
            getattr(version, "bucket_data_source_id", None) if version else None
        )
        pointer_valid = True
        if hasattr(run, "bucket_file_id"):
            pointer_valid = bool(run_file_id and version_file_id and run_file_id == version_file_id)
        if hasattr(run, "data_source_id") and hasattr(version, "bucket_data_source_id"):
            pointer_valid = pointer_valid and bool(
                run_source_id and version_source_id and run_source_id == version_source_id
            )
        if (
            asset is not None
            and version is not None
            and asset.tenant_id == run.tenant_id
            and version.tenant_id == run.tenant_id
            and version.asset_id == asset.id
            and getattr(asset, "lifecycle_status", "active") == "active"
            and getattr(version, "status", "ready") == "ready"
            and asset.owner_agent_id == owner_agent_id
            and pointer_valid
        ):
            try:
                # The logical owner pair is not enough: the shared upload
                # bucket can contain another Agent's file.  Re-prove the
                # immutable version/source lineage before rendering it.
                managed_attachment_access.require_asset_version_scope(
                    db,
                    asset,
                    version,
                    tenant_id=run.tenant_id,
                    agent_id=owner_agent_id,
                )
            except managed_attachment_access.AttachmentAccessError:
                result = None
            else:
                result = catalog_ingestion_service.managed_upload_document(
                    asset,
                    version,
                    fallback_purpose=run.purpose,
                    created=bool((run.metadata_document or {}).get("created", True)),
                )
    return {
        "id": run.id,
        "parent_run_id": getattr(run, "parent_run_id", None),
        "purpose": run.purpose,
        "filename": run.filename,
        "declared_byte_size": run.declared_byte_size,
        "byte_size": run.byte_size,
        "status": run.status,
        "revision": run.revision,
        "asset_id": run.asset_id,
        "asset_version_id": run.asset_version_id,
        "content_sha256": run.content_sha256,
        "error": (
            {"code": run.error_code, "message": run.error_message}
            if run.error_code
            else None
        ),
        "result": result,
        "expires_at": run.expires_at,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "finished_at": run.finished_at,
        # ``SimpleNamespace`` fixtures and pre-ownership rows may not expose
        # the additive field; keep the public serializer backward compatible.
        "owner_agent_id": owner_agent_id,
    }


def create_upload_run(db: Session, payload: ManagedUploadCreateIn) -> dict[str, Any]:
    principal = permission_service.require_principal(db)
    owner_agent_id = _resolve_agent_scope(
        db,
        payload,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
    )
    safe_name = datasource_service.validate_bucket_filename(payload.filename)
    maximum = int(get_settings().catalog_max_upload_bytes)
    if payload.byte_size > maximum:
        raise ManagedUploadError(
            "managed_upload_too_large",
            f"文件超过大小限制（{maximum // (1024 * 1024)} MB）",
            status_code=413,
        )
    source = catalog_ingestion_service.require_external_upload_bucket(
        db,
        ensure_storage=False,
        owner_agent_id=owner_agent_id,
    )
    safe_labels = catalog_service.safe_catalog_document(
        payload.labels,
        label="资产标签",
        maximum=32_000,
    )
    metadata = CatalogManagedUploadMetadata(
        file_bucket_id=source.id,
        purpose=payload.purpose,
        name=payload.name or safe_name,
        description=payload.description,
        labels=safe_labels,
        expires_in_seconds=payload.expires_in_seconds,
    )
    fingerprint = _canonical_hash(
        {
            "contract": "managed-upload-request/v1",
            "tenant_id": principal.tenant_id,
            "user_id": principal.user_id,
            "filename": safe_name,
            "byte_size": payload.byte_size,
            "media_type": payload.media_type.strip().lower(),
            "agent_id": owner_agent_id,
            "conversation_id": payload.conversation_id,
            "metadata": metadata.model_dump(mode="json", exclude_none=True),
        }
    )
    existing = db.scalar(
        select(ManagedUploadRun).where(
            ManagedUploadRun.tenant_id == principal.tenant_id,
            ManagedUploadRun.requested_by_user_id == principal.user_id,
            ManagedUploadRun.idempotency_key == payload.idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise ManagedUploadConflict("同一幂等键不能登记不同文件")
        return _public_run(db, existing)
    now = _now()
    expiry_seconds = 24 * 60 * 60
    if payload.purpose == "invocation_attachment":
        expiry_seconds = (
            metadata.expires_in_seconds
            or catalog_ingestion_service.DEFAULT_ATTACHMENT_TTL_SECONDS
        )
    run = ManagedUploadRun(
        tenant_id=principal.tenant_id,
        requested_by_user_id=principal.user_id,
        owner_agent_id=owner_agent_id,
        data_source_id=source.id,
        idempotency_key=payload.idempotency_key,
        request_fingerprint=fingerprint,
        purpose=payload.purpose,
        filename=safe_name,
        client_media_type=payload.media_type.strip().lower(),
        declared_byte_size=payload.byte_size,
        metadata_document=metadata.model_dump(
            mode="json",
            exclude={"file_bucket_id", "purpose"},
            exclude_none=True,
        ),
        status="awaiting_upload",
        revision=1,
        available_at=now,
        expires_at=now + timedelta(seconds=expiry_seconds),
        created_at=now,
        updated_at=now,
    )
    db.add(run)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        replay = db.scalar(
            select(ManagedUploadRun).where(
                ManagedUploadRun.tenant_id == principal.tenant_id,
                ManagedUploadRun.requested_by_user_id == principal.user_id,
                ManagedUploadRun.idempotency_key == payload.idempotency_key,
            )
        )
        if replay is None or replay.request_fingerprint != fingerprint:
            raise ManagedUploadConflict("上传任务并发登记发生冲突") from exc
        return _public_run(db, replay)
    db.refresh(run)
    return _public_run(db, run)


def get_upload_run(
    db: Session,
    run_id: str,
    *,
    agent_id: str | None = None,
) -> dict[str, Any]:
    return _public_run(db, _owned_run(db, run_id, agent_id=agent_id))


def get_invocation_upload_runs(
    db: Session,
    run_ids: list[str],
    *,
    agent_id: str | None = None,
) -> list[ManagedUploadRun]:
    """Return exact owner-scoped temporary runs in caller order."""

    if not run_ids:
        return []
    unique_ids = list(dict.fromkeys(str(item or "") for item in run_ids))
    if len(unique_ids) != len(run_ids) or any(not item for item in unique_ids):
        raise ManagedUploadError(
            "managed_upload_unavailable",
            "临时附件上传任务不可用",
            status_code=409,
        )
    principal = permission_service.require_principal(db)
    if agent_id is not None:
        _authorize_agent_scope(
            db,
            agent_id,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
        )
    else:
        permission_service.require_tenant_permission(db, "read")
    rows = list(
        db.scalars(
            select(ManagedUploadRun).where(
                ManagedUploadRun.id.in_(unique_ids),
                ManagedUploadRun.tenant_id == principal.tenant_id,
                ManagedUploadRun.requested_by_user_id == principal.user_id,
                ManagedUploadRun.purpose == "invocation_attachment",
                ManagedUploadRun.error_code != "agent_deleted",
                *(
                    [ManagedUploadRun.owner_agent_id == agent_id]
                    if agent_id is not None
                    else [ManagedUploadRun.owner_agent_id.is_(None)]
                ),
            )
        ).all()
    )
    by_id = {row.id: row for row in rows}
    if len(by_id) != len(unique_ids):
        raise ManagedUploadError(
            "managed_upload_unavailable",
            "临时附件上传任务不可用",
            status_code=409,
        )
    now = _now()
    ordered = [by_id[item] for item in unique_ids]
    if any((_as_utc(row.expires_at) or now) <= now for row in ordered):
        raise ManagedUploadError(
            "managed_upload_expired",
            "临时附件上传任务已过期",
            status_code=410,
        )
    return ordered


def invocation_attachment_documents(
    db: Session,
    run_ids: list[str],
    *,
    agent_id: str | None = None,
) -> list[ManagedInvocationAttachment]:
    """Resolve ready runs into retrieval inputs without exposing object locators."""

    runs = get_invocation_upload_runs(db, run_ids, agent_id=agent_id)
    if any(run.status not in {"ready", "failed", "cancelled"} for run in runs):
        raise ManagedUploadConflict("临时附件仍在后台准备")
    failed = [run for run in runs if run.status in {"failed", "cancelled"}]
    if failed:
        first = failed[0]
        raise ManagedUploadError(
            first.error_code or "managed_upload_failed",
            first.error_message or "临时附件处理失败，请重试或移除后重新发送",
            status_code=409,
        )
    output: list[ManagedInvocationAttachment] = []
    for run in runs:
        asset = db.get(DataAsset, run.asset_id) if run.asset_id else None
        version = db.get(DataAssetVersion, run.asset_version_id) if run.asset_version_id else None
        bucket_file = db.get(BucketFile, version.bucket_file_id) if version else None
        owner_agent_id = getattr(run, "owner_agent_id", None)
        run_file_id = getattr(run, "bucket_file_id", None)
        version_file_id = getattr(version, "bucket_file_id", None) if version else None
        version_source_id = (
            getattr(version, "bucket_data_source_id", None) if version else None
        )
        file_pointer_valid = (
            not hasattr(run, "bucket_file_id")
            or bool(run_file_id and version_file_id and run_file_id == version_file_id)
        )
        source_pointer_valid = (
            not hasattr(version, "bucket_data_source_id")
            or bool(
                version_source_id
                and getattr(run, "data_source_id", None)
                and version_source_id == run.data_source_id
            )
        )
        if (
            asset is None
            or version is None
            or bucket_file is None
            or asset.tenant_id != run.tenant_id
            or asset.owner_agent_id != owner_agent_id
            or getattr(asset, "lifecycle_status", "active") != "active"
            or version.tenant_id != run.tenant_id
            or version.asset_id != asset.id
            or version.asset_id != run.asset_id
            or getattr(version, "status", "ready") != "ready"
            or not file_pointer_valid
            or not source_pointer_valid
            or version.content_sha256 != run.content_sha256
            or getattr(bucket_file, "id", None) != (version_file_id or bucket_file.id)
            or bucket_file.data_source_id != run.data_source_id
            or bucket_file.status != "parsed"
            or bucket_file.content_sha256 != run.content_sha256
        ):
            raise ManagedUploadError(
                "managed_upload_object_invalid",
                "临时附件准备结果不可用",
                status_code=409,
            )
        try:
            managed_asset_lifecycle.require_current_asset_version(
                version.version_document
            )
        except managed_asset_lifecycle.ManagedAssetLifecycleError as exc:
            expired = exc.code == "managed_reference_expired"
            raise ManagedUploadError(
                (
                    "managed_upload_expired"
                    if expired
                    else "managed_upload_object_invalid"
                ),
                (
                    "临时附件上传任务已过期"
                    if expired
                    else "临时附件缺少有效生命周期"
                ),
                status_code=410 if expired else 409,
            ) from None
        output.append(
            ManagedInvocationAttachment(
                id=run.id,
                filename=run.filename,
                mime=str(bucket_file.mime or run.client_media_type or ""),
                size=int(run.byte_size or 0),
                status="parsed",
                content_hash=run.content_sha256,
                parsed_text=str(bucket_file.parsed_text or ""),
                error="",
            )
        )
    return output


def preflight_content_upload(
    db: Session,
    run_id: str,
    *,
    expected_revision: int,
    agent_id: str | None = None,
) -> int:
    return managed_upload_content_service.preflight_content_upload(
        db,
        run_id,
        expected_revision=expected_revision,
        agent_id=agent_id,
        owned_run=_owned_run,
    )


def claim_content_upload(
    db: Session,
    run_id: str,
    *,
    expected_revision: int,
    agent_id: str | None = None,
) -> UploadLease:
    return managed_upload_content_service.claim_content_upload(
        db,
        run_id,
        expected_revision=expected_revision,
        agent_id=agent_id,
        owned_run=_owned_run,
    )


def _lease_matches(run: ManagedUploadRun, lease: UploadLease, *, as_of: datetime) -> bool:
    return lease_matches(run, lease, as_of=as_of)


def _renew_lease(run_id: str, lease: UploadLease, status: str, seconds: int) -> bool:
    return managed_upload_content_service.renew_lease(
        run_id,
        lease,
        status,
        seconds,
        session_factory=SessionLocal,
    )


@contextmanager
def _lease_heartbeat(
    run_id: str,
    lease: UploadLease,
    *,
    status: str,
    seconds: int,
) -> Iterator[threading.Event]:
    with managed_upload_content_service.lease_heartbeat(
        run_id,
        lease,
        status=status,
        seconds=seconds,
        renew=_renew_lease,
    ) as lost:
        yield lost


def _reset_content_claim(run_id: str, lease: UploadLease) -> None:
    managed_upload_content_service.reset_content_claim(
        run_id,
        lease,
        session_factory=SessionLocal,
    )


def store_uploaded_content(
    run_id: str,
    lease: UploadLease,
    source_path: str | Path,
    *,
    content_sha256: str,
    byte_size: int,
) -> dict[str, Any]:
    return managed_upload_content_service.store_uploaded_content(
        run_id,
        lease,
        source_path,
        content_sha256=content_sha256,
        byte_size=byte_size,
        session_factory=SessionLocal,
        public_run=_public_run,
        heartbeat_factory=_lease_heartbeat,
        reset_claim=_reset_content_claim,
    )


def _claim_processing_run(db: Session, run_id: str) -> UploadLease | None:
    return managed_upload_processing_service.claim_processing_run(db, run_id)


def _load_processing_snapshot(
    run_id: str,
    lease: UploadLease,
) -> UploadProcessingSnapshot | None:
    return managed_upload_processing_service.load_processing_snapshot(
        run_id,
        lease,
        session_factory=SessionLocal,
        metadata_loader=_metadata,
    )


def _profile_stored_upload(run_id: str, lease: UploadLease) -> bool:
    return managed_upload_processing_service.profile_stored_upload(
        run_id,
        lease,
        session_factory=SessionLocal,
        load_snapshot=_load_processing_snapshot,
        heartbeat_factory=_lease_heartbeat,
    )


def process_upload_run(run_id: str) -> bool:
    db = SessionLocal()
    try:
        lease = _claim_processing_run(db, run_id)
    finally:
        db.close()
    return _profile_stored_upload(run_id, lease) if lease is not None else False


def process_next_upload_run() -> bool:
    if recover_upload_runs(limit=16):
        return True
    db = SessionLocal()
    try:
        now = _now()
        candidate = db.scalar(
            select(ManagedUploadRun.id)
            .where(
                or_(
                    (
                        (ManagedUploadRun.status == "stored")
                        & (ManagedUploadRun.available_at <= now)
                    ),
                    (
                        (ManagedUploadRun.status == "processing")
                        & (ManagedUploadRun.lease_expires_at.is_not(None))
                        & (ManagedUploadRun.lease_expires_at <= now)
                    ),
                )
            )
            .order_by(ManagedUploadRun.available_at, ManagedUploadRun.created_at)
            .limit(1)
        )
    finally:
        db.close()
    return process_upload_run(candidate) if candidate else False


def recover_upload_runs(*, limit: int = 100) -> int:
    return managed_upload_content_service.recover_upload_runs(
        limit=limit,
        session_factory=SessionLocal,
    )


def create_failed_upload_retry(
    db: Session,
    run: ManagedUploadRun,
    *,
    idempotency_key: str,
    require_stored_content: bool = False,
) -> ManagedUploadRun:
    """Create or replay one immutable child for a failed upload run."""

    if run.status != "failed":
        raise ManagedUploadConflict("只有失败的上传任务可以重试")
    if require_stored_content and not run.bucket_file_id:
        raise ManagedUploadError(
            "managed_upload_requires_reupload",
            "附件内容未完整保存，请重新选择文件并发送新的请求",
            status_code=409,
        )
    fingerprint = _canonical_hash(
        {
            "contract": "managed-upload-retry/v1",
            "parent_run_id": run.id,
            "parent_request_fingerprint": run.request_fingerprint,
        }
    )
    exact_replay = db.scalar(
        select(ManagedUploadRun).where(
            ManagedUploadRun.tenant_id == run.tenant_id,
            ManagedUploadRun.parent_run_id == run.id,
            ManagedUploadRun.idempotency_key == idempotency_key,
        )
    )
    if exact_replay is not None:
        if exact_replay.request_fingerprint != fingerprint:
            raise ManagedUploadConflict("上传重试身份已绑定到不同的执行输入")
        return exact_replay
    key_collision = db.scalar(
        select(ManagedUploadRun).where(
            ManagedUploadRun.tenant_id == run.tenant_id,
            ManagedUploadRun.requested_by_user_id == run.requested_by_user_id,
            ManagedUploadRun.idempotency_key == idempotency_key,
        )
    )
    if key_collision is not None:
        raise ManagedUploadConflict("上传重试幂等键已用于其他执行链路")
    different_retry = db.scalar(
        select(ManagedUploadRun.id).where(
            ManagedUploadRun.tenant_id == run.tenant_id,
            ManagedUploadRun.parent_run_id == run.id,
        )
    )
    if different_retry is not None:
        raise ManagedUploadConflict("该上传任务已使用其他幂等键创建重试任务")

    now = _now()
    has_stored_content = bool(run.bucket_file_id)
    child = ManagedUploadRun(
        id=uuid.uuid4().hex,
        tenant_id=run.tenant_id,
        requested_by_user_id=run.requested_by_user_id,
        owner_agent_id=run.owner_agent_id,
        parent_run_id=run.id,
        data_source_id=run.data_source_id,
        bucket_file_id=run.bucket_file_id if has_stored_content else None,
        asset_id=None,
        asset_version_id=None,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        purpose=run.purpose,
        filename=run.filename,
        client_media_type=run.client_media_type,
        declared_byte_size=run.declared_byte_size,
        byte_size=run.byte_size if has_stored_content else 0,
        content_sha256=run.content_sha256 if has_stored_content else "",
        metadata_document=dict(run.metadata_document or {}),
        status="stored" if has_stored_content else "awaiting_upload",
        revision=1,
        available_at=now,
        expires_at=run.expires_at,
        created_at=now,
        updated_at=now,
    )
    db.add(child)
    return child


def retry_upload_run(
    db: Session,
    run_id: str,
    *,
    expected_revision: int,
    idempotency_key: str,
    agent_id: str | None = None,
) -> dict[str, Any]:
    run = _owned_run(
        db,
        run_id,
        writable=True,
        lock=True,
        agent_id=agent_id,
    )
    replay = db.scalar(
        select(ManagedUploadRun).where(
            ManagedUploadRun.tenant_id == run.tenant_id,
            ManagedUploadRun.parent_run_id == run.id,
            ManagedUploadRun.idempotency_key == idempotency_key,
        )
    )
    if replay is not None:
        return _public_run(db, replay)
    if run.revision != expected_revision:
        raise ManagedUploadConflict()
    child = create_failed_upload_retry(
        db,
        run,
        idempotency_key=idempotency_key,
    )
    expected_fingerprint = child.request_fingerprint
    tenant_id = run.tenant_id
    requested_by_user_id = run.requested_by_user_id
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        concurrent = db.scalar(
            select(ManagedUploadRun).where(
                ManagedUploadRun.tenant_id == tenant_id,
                ManagedUploadRun.requested_by_user_id == requested_by_user_id,
                ManagedUploadRun.idempotency_key == idempotency_key,
            )
        )
        if (
            concurrent is None
            or concurrent.parent_run_id != run_id
            or concurrent.request_fingerprint != expected_fingerprint
        ):
            raise ManagedUploadConflict("上传重试并发登记冲突") from exc
        return _public_run(db, concurrent)
    db.refresh(child)
    return _public_run(db, child)


def cancel_upload_run(
    db: Session,
    run_id: str,
    *,
    expected_revision: int,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Fence unfinished work and durably cancel one owner-scoped upload."""

    run = _owned_run(
        db,
        run_id,
        writable=True,
        lock=True,
        agent_id=agent_id,
    )
    if run.revision != expected_revision:
        raise ManagedUploadConflict()
    if run.status == "cancelled":
        return _public_run(db, run)
    if run.status == "ready":
        raise ManagedUploadConflict("已完成的临时附件无需取消，可直接从本次需求移除")
    if run.bucket_file_id:
        source = db.scalar(
            select(DataSource).where(
                DataSource.id == run.data_source_id,
                DataSource.tenant_id == run.tenant_id,
            )
        )
        bucket_file = db.scalar(
            select(BucketFile).where(
                BucketFile.id == run.bucket_file_id,
                BucketFile.data_source_id == run.data_source_id,
            )
        )
        if source is None or bucket_file is None:
            raise ManagedUploadError(
                "managed_upload_object_invalid",
                "临时附件存储状态不可用",
                status_code=409,
            )
        shared_run_id = db.scalar(
            select(ManagedUploadRun.id).where(
                ManagedUploadRun.id != run.id,
                ManagedUploadRun.tenant_id == run.tenant_id,
                ManagedUploadRun.bucket_file_id == bucket_file.id,
            )
        )
        run.bucket_file_id = None
        if shared_run_id is None:
            object_deletion_service.enqueue_bucket_file_deletion(
                db,
                bucket_file,
                source,
            )
            db.delete(bucket_file)
    now = _now()
    run.status = "cancelled"
    run.revision += 1
    run.lease_token = ""
    run.lease_expires_at = None
    run.error_code = "managed_upload_cancelled"
    run.error_message = "附件上传已取消"
    run.finished_at = now
    run.updated_at = now
    db.commit()
    db.refresh(run)
    return _public_run(db, run)
