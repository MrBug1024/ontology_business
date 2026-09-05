"""Background parsing and catalog publication for stored managed uploads."""
from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
import tempfile
from typing import Any, Callable
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import BucketFile, DataSource, ManagedUploadRun
from . import (
    catalog_ingestion_service,
    catalog_service,
    doc_parser,
    object_deletion_service,
    object_storage_service,
    permission_service,
)
from .managed_upload_run_contracts import (
    ManagedUploadError,
    UploadLease,
    UploadProcessingSnapshot,
    as_utc,
    lease_matches,
    utc_now,
)


PROCESSING_LEASE_SECONDS = 120


def claim_processing_run(db: Session, run_id: str) -> UploadLease | None:
    run = db.scalar(
        select(ManagedUploadRun)
        .where(ManagedUploadRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = utc_now()
    if run is None:
        return None
    expires_at = as_utc(run.lease_expires_at)
    if run.status != "stored" and not (
        run.status == "processing" and expires_at is not None and expires_at <= now
    ):
        return None
    token = uuid.uuid4().hex
    run.status = "processing"
    run.revision += 1
    run.lease_generation += 1
    run.lease_token = token
    run.lease_expires_at = now + timedelta(seconds=PROCESSING_LEASE_SECONDS)
    run.error_code = ""
    run.error_message = ""
    run.updated_at = now
    db.commit()
    return UploadLease(token, run.lease_generation, run.lease_expires_at)


def load_processing_snapshot(
    run_id: str,
    lease: UploadLease,
    *,
    session_factory: Callable[[], Session],
    metadata_loader: Callable[[ManagedUploadRun], Any],
) -> UploadProcessingSnapshot | None:
    db = session_factory()
    try:
        run = db.get(ManagedUploadRun, run_id)
        if run is None or not run.requested_by_user_id or not lease_matches(
            run, lease, as_of=utc_now()
        ):
            return None
        db.info["tenant_id"] = run.tenant_id
        db.info["user_id"] = run.requested_by_user_id
        permission_service.require_principal(db)
        permission_service.require_tenant_permission(db, "write")
        source = db.get(DataSource, run.data_source_id)
        bucket_file = db.get(BucketFile, run.bucket_file_id) if run.bucket_file_id else None
        if (
            source is None
            or source.tenant_id != run.tenant_id
            or bucket_file is None
            or bucket_file.data_source_id != source.id
            or bucket_file.content_sha256 != run.content_sha256
            or int(bucket_file.size or 0) != run.byte_size
        ):
            raise ManagedUploadError(
                "managed_upload_object_invalid",
                "上传对象不可用或完整性信息不一致",
            )
        snapshot = UploadProcessingSnapshot(
            tenant_id=run.tenant_id,
            user_id=run.requested_by_user_id,
            data_source_id=source.id,
            bucket_file_id=bucket_file.id,
            filename=run.filename,
            client_media_type=run.client_media_type,
            byte_size=run.byte_size,
            content_sha256=run.content_sha256,
            created_at=as_utc(run.created_at),
            metadata=metadata_loader(run),
            bucket_name=bucket_file.bucket_name,
            object_key=bucket_file.object_key,
            object_version_id=bucket_file.object_version_id,
        )
        # No database transaction remains open while MinIO or format parsers run.
        db.rollback()
        return snapshot
    finally:
        db.close()


def profile_stored_upload(
    run_id: str,
    lease: UploadLease,
    *,
    session_factory: Callable[[], Session],
    load_snapshot: Callable[[str, UploadLease], UploadProcessingSnapshot | None],
    heartbeat_factory: Callable[..., Any],
) -> bool:
    try:
        snapshot = load_snapshot(run_id, lease)
        if snapshot is None:
            return False
        maximum = int(get_settings().catalog_max_upload_bytes)
        suffix = Path(snapshot.filename).suffix.casefold()
        if not suffix or len(suffix) > 11:
            suffix = ".part"
        with tempfile.TemporaryDirectory(prefix="ontology-managed-upload-") as raw_dir:
            local_path = Path(raw_dir).resolve() / f"content{suffix}"
            with heartbeat_factory(
                run_id,
                lease,
                status="processing",
                seconds=PROCESSING_LEASE_SECONDS,
            ) as lease_lost:
                downloaded = object_storage_service.download_object_to_file(
                    snapshot.bucket_name,
                    snapshot.object_key,
                    local_path,
                    version_id=snapshot.object_version_id,
                    max_bytes=maximum,
                )
                if downloaded.size != snapshot.byte_size:
                    raise ManagedUploadError(
                        "managed_upload_object_invalid",
                        "上传对象大小校验失败",
                    )
                hasher = hashlib.sha256()
                with local_path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                        hasher.update(chunk)
                if hasher.hexdigest() != snapshot.content_sha256:
                    raise ManagedUploadError(
                        "managed_upload_object_invalid",
                        "上传对象内容校验失败",
                    )
                prepared = catalog_ingestion_service.prepare_upload_path(
                    local_path,
                    snapshot.filename,
                    snapshot.client_media_type or None,
                    snapshot.metadata,
                    content_sha256=snapshot.content_sha256,
                    byte_size=snapshot.byte_size,
                    now=snapshot.created_at,
                )
                parsed_text = ""
                if snapshot.metadata.purpose == "invocation_attachment":
                    profile = prepared.profile if isinstance(prepared.profile, dict) else {}
                    if profile.get("category") == "table":
                        parsed_text = catalog_ingestion_service.profile_summary_text(
                            profile,
                            snapshot.filename,
                        )
                    else:
                        parse_limit = int(get_settings().document_parse_max_bytes)
                        if snapshot.byte_size > parse_limit:
                            raise ManagedUploadError(
                                "managed_upload_document_too_large",
                                f"文档解析上限为 {parse_limit // 1024 // 1024}MB；超大结构化数据请使用数据集通道",
                                status_code=413,
                            )
                        canonical_extension = str(profile.get("extension") or ".txt")
                        parsed = doc_parser.parse_file(
                            local_path,
                            f"document{canonical_extension}",
                        )
                        if parsed.get("status") != "success":
                            raise ManagedUploadError(
                                "managed_upload_content_invalid",
                                str(parsed.get("message") or "附件解析失败")[:1000],
                                status_code=422,
                            )
                        parsed_text = str(parsed.get("text") or "")
                        if not parsed_text.strip():
                            raise ManagedUploadError(
                                "managed_upload_content_invalid",
                                "附件未解析出可检索文本",
                                status_code=422,
                            )
                        if len(parsed_text) > 1_000_000:
                            raise ManagedUploadError(
                                "managed_upload_content_too_large",
                                "附件解析文本超过临时会话上限，请拆分后重试",
                                status_code=413,
                            )
                if lease_lost.is_set():
                    return False

        db = session_factory()
        try:
            db.info["tenant_id"] = snapshot.tenant_id
            db.info["user_id"] = snapshot.user_id
            permission_service.require_principal(db)
            permission_service.require_tenant_permission(db, "write")
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
            bucket_file = db.scalar(
                select(BucketFile).where(
                    BucketFile.id == snapshot.bucket_file_id,
                    BucketFile.data_source_id == snapshot.data_source_id,
                )
            )
            if current is None or not lease_matches(current, lease, as_of=utc_now()):
                db.rollback()
                return False
            if (
                source is None
                or bucket_file is None
                or current.bucket_file_id != bucket_file.id
                or bucket_file.content_sha256 != snapshot.content_sha256
                or int(bucket_file.size or 0) != snapshot.byte_size
            ):
                raise ManagedUploadError(
                    "managed_upload_object_invalid",
                    "上传对象在后台处理期间已失效",
                )
            with catalog_ingestion_service._serialize_upload_identity(
                db,
                tenant_id=current.tenant_id,
                asset_key=prepared.asset_key,
            ):
                asset, duplicate, created, replace_expired = (
                    catalog_ingestion_service.find_or_create_asset(db, prepared)
                )
                version = duplicate
                if version is None:
                    bucket_file.status = "parsed"
                    bucket_file.error = ""
                    bucket_file.parsed_text = parsed_text
                    version = catalog_ingestion_service.register_prepared_version(
                        db,
                        asset,
                        bucket_file.id,
                        prepared,
                        allow_duplicate_content=replace_expired,
                    )
                owns_object = version.bucket_file_id == bucket_file.id
                if not owns_object:
                    object_deletion_service.enqueue_bucket_file_deletion(
                        db,
                        bucket_file,
                        source,
                    )
                    current.bucket_file_id = None
                    db.delete(bucket_file)
                current.asset_id = asset.id
                current.asset_version_id = version.id
                current.status = "ready"
                current.revision += 1
                current.lease_token = ""
                current.lease_expires_at = None
                current.error_code = ""
                current.error_message = ""
                current.metadata_document = {
                    **dict(current.metadata_document or {}),
                    "created": bool(created and owns_object),
                }
                current.finished_at = utc_now()
                current.updated_at = current.finished_at
                db.commit()
            return True
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 - public task error remains stable.
        db = session_factory()
        try:
            failed = db.scalar(
                select(ManagedUploadRun)
                .where(
                    ManagedUploadRun.id == run_id,
                    ManagedUploadRun.status == "processing",
                    ManagedUploadRun.lease_token == lease.token,
                    ManagedUploadRun.lease_generation == lease.generation,
                    ManagedUploadRun.lease_expires_at.is_not(None),
                    ManagedUploadRun.lease_expires_at > utc_now(),
                )
                .with_for_update()
            )
            if failed is not None:
                failed.status = "failed"
                failed.revision += 1
                failed.lease_token = ""
                failed.lease_expires_at = None
                if isinstance(exc, ManagedUploadError):
                    failed.error_code = exc.code
                    failed.error_message = exc.message
                elif isinstance(exc, catalog_service.CatalogError):
                    failed.error_code = "managed_upload_content_invalid"
                    failed.error_message = str(exc)[:1000]
                else:
                    failed.error_code = "managed_upload_processing_failed"
                    failed.error_message = "文件后台解析失败"
                failed.finished_at = utc_now()
                failed.updated_at = failed.finished_at
                db.commit()
            return False
        finally:
            db.close()
