"""Atomic attachment of one validated SQLite snapshot using upload intents."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BucketFile, DataSource
from . import datasource_service, library_database_service, object_deletion_service, permission_service, tenant_service
from .library_sqlite_adapter import inspect_path
from .upload_staging_service import StagedUpload


def _current_source(db: Session, source_id: str, revision: int, scenario_id: str | None) -> DataSource:
    permission_service.refresh_request_authorization(db)
    current = db.scalar(select(DataSource).where(DataSource.id == source_id,
        DataSource.tenant_id == tenant_service.current_tenant_id(db)).execution_options(populate_existing=True).with_for_update())
    if current is None:
        raise HTTPException(404, "资料库不存在")
    if current.type != "sqlite3" or current.resource_scope != "modeling" or current.owner_agent_id:
        raise HTTPException(404, "资料库不存在")
    if current.connector_revision != revision or current.scenario_id != scenario_id:
        raise HTTPException(409, "资料库已变化，请刷新后重试")
    if current.scenario_id:
        scenario = tenant_service.require_scenario(db, current.scenario_id, writable=True)
        permission_service.require_scenario_permission(db, scenario, "write")
    else:
        permission_service.require_tenant_permission(db, "write")
    return current


def attach_snapshot(db: Session, source: DataSource, staged: StagedUpload, filename: str) -> BucketFile:
    source = _current_source(db, source.id, source.connector_revision, source.scenario_id)
    if source.type != "sqlite3" or source.resource_scope != "modeling":
        raise HTTPException(422, "请选择 SQLite3 资料库")
    if Path(filename).suffix.casefold() not in {".db", ".sqlite", ".sqlite3"}:
        raise HTTPException(422, "请选择 .db、.sqlite 或 .sqlite3 快照文件")
    revision, source_id, scenario_id = source.connector_revision, source.id, source.scenario_id
    frozen = library_database_service.snapshot(source)
    if frozen.files:
        existing = frozen.files[0]
        if len(frozen.files) == 1 and existing.content_sha256 == staged.content_sha256 and existing.size == staged.byte_size:
            # Retrying a completed upload returns the immutable receipt. The
            # digest comes from server-side streaming, never a caller claim.
            return source.files[0]
        raise HTTPException(409, "该库已有 SQLite3 快照；新快照请创建新的资料库")
    db.commit()
    try:
        inspect_path(staged.path)
    except (ValueError, OSError, sqlite3.Error, TimeoutError) as exc:
        raise HTTPException(422, "SQLite3 快照无效或超过结构边界，请导出完整、已关闭写入的数据库文件") from exc
    claim = object_deletion_service.prepare_bucket_file_upload(frozen, uuid.uuid4().hex, filename)
    uploaded = None
    try:
        with object_deletion_service.heartbeat_upload_intent(claim) as heartbeat:
            object_deletion_service.begin_upload_put(claim)
            uploaded = datasource_service.save_bucket_file_path(frozen, filename, staged.path,
                mime="application/vnd.sqlite3", stable_file_id=claim.origin_id,
                upload_object_key=claim.object_key, content_sha256=staged.content_sha256)
            object_deletion_service.assert_upload_active(heartbeat, claim, uploaded)
        current = _current_source(db, source_id, revision, scenario_id)
        if db.scalar(select(BucketFile.id).where(BucketFile.data_source_id == current.id).limit(1)):
            raise HTTPException(409, "该库已保存快照，请刷新查看")
        uploaded.status = "parsed"
        uploaded.index_status = "not_applicable"
        current.status = "ok"
        current.last_error = ""
        current.config = dict(current.config, snapshot_file_id=uploaded.id,
                              snapshot_sha256=uploaded.content_sha256)
        db.add(uploaded)
        object_deletion_service.retain_bucket_file_upload(db, claim, uploaded, current)
        db.commit()
        db.refresh(uploaded)
        return uploaded
    except Exception:
        db.rollback()
        if uploaded is not None:
            object_deletion_service.schedule_abandoned_upload_best_effort(claim, uploaded)
        raise
