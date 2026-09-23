"""Authorized library discovery and frozen receipts for tool-selected materials."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from ..distillation_schemas import DistillationDocument, Evidence
from ..models import BucketFile, DataSource
from . import (
    distillation_analysis_service as analysis,
    distillation_service,
    permission_service,
    release_service,
    tenant_service,
)
from .distillation_evidence_service import capture_evidence_identity


LIBRARY_TYPES = ("postgres", "mysql", "sqlite3", "file_bucket", "dataset", "distillation")
MAX_LIBRARY_READS = 6


def _safe_label(value: str) -> str:
    text = release_service.safe_snapshot_content({"content": value}).get("content")
    return text[:200] if isinstance(text, str) else "已隐藏敏感名称"


def authorized_source_filters(
    db: Session,
    scenario_id: str | None,
    *,
    include_shared: bool,
) -> tuple[ColumnElement[bool], ...]:
    """Authorize and build the exact source scope for a catalog page."""

    principal = permission_service.require_principal(db)
    external_scenario_id = db.info.get("external_scenario_id")
    common = (
        DataSource.resource_scope == "modeling",
        DataSource.type.in_(LIBRARY_TYPES),
    )
    if scenario_id is None:
        if external_scenario_id is not None:
            raise HTTPException(403, "场景凭据不能访问工作区共享资料")
        permission_service.require_tenant_permission(db, "read")
        return (
            *common,
            DataSource.tenant_id == principal.tenant_id,
            DataSource.scenario_id.is_(None),
        )

    scenario = tenant_service.require_scenario(db, scenario_id)
    permission_service.require_scenario_permission(db, scenario, "read")
    if scenario.tenant_id != principal.tenant_id:
        # A public scenario and a public source are separate visibility grants.
        # Never blend the caller's workspace-shared rows into a foreign scenario.
        return (
            *common,
            DataSource.tenant_id == scenario.tenant_id,
            DataSource.scenario_id == scenario.id,
            DataSource.is_public.is_(True),
        )

    scenario_scope = and_(
        DataSource.tenant_id == principal.tenant_id,
        DataSource.scenario_id == scenario.id,
    )
    shared_allowed = False
    if include_shared and external_scenario_id is None:
        try:
            permission_service.require_tenant_permission(db, "read")
            shared_allowed = True
        except HTTPException:
            # A scenario-specific allow must not implicitly grant workspace scope.
            pass
    if shared_allowed:
        scope = or_(
            scenario_scope,
            and_(
                DataSource.tenant_id == principal.tenant_id,
                DataSource.scenario_id.is_(None),
            ),
        )
    else:
        scope = scenario_scope
    return (*common, scope)


def list_sources(db: Session, scenario_id: str | None, offset: int, limit: int) -> dict:
    rows = db.execute(select(DataSource.id, DataSource.name, DataSource.type, DataSource.scenario_id).where(
        *authorized_source_filters(db, scenario_id, include_shared=True))
        .order_by(DataSource.created_at.desc(), DataSource.id).offset(offset).limit(limit + 1)).all()
    return {"sources": [{"data_source_id": row.id, "name": _safe_label(row.name), "type": row.type,
        "scope": "scenario" if row.scenario_id else "shared"} for row in rows[:limit]], "has_more": len(rows) > limit,
        "next_offset": offset + limit if len(rows) > limit else None}


def list_files(db: Session, scenario_id: str | None, source_id: str, offset: int, limit: int) -> dict:
    source = distillation_service.evidence_source(db, source_id, scenario_id)
    if source.type not in {"file_bucket", "sqlite3"}:
        raise HTTPException(422, "该资料库不是受管文件库")
    rows = db.execute(select(BucketFile.id, BucketFile.filename, BucketFile.status, BucketFile.size).where(
        BucketFile.data_source_id == source.id).order_by(BucketFile.id).offset(offset).limit(limit + 1)).all()
    return {"files": [{"bucket_file_id": row.id, "filename": _safe_label(row.filename), "status": row.status,
        "byte_size": row.size} for row in rows[:limit]], "has_more": len(rows) > limit,
        "next_offset": offset + limit if len(rows) > limit else None}


def identity_hash(identity: dict) -> str:
    return hashlib.sha256(b"distillation-library-read:v1\0" + json.dumps(identity,
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class LibraryRead:
    evidence: Evidence
    identity: dict
    content: dict


def read_source(db: Session, scenario_id: str | None, source_id: str, file_id: str | None) -> LibraryRead:
    source = distillation_service.evidence_source(db, source_id, scenario_id)
    if source.type not in LIBRARY_TYPES:
        raise HTTPException(422, "该资料库类型不支持业务调查")
    if source.type == "file_bucket" and file_id:
        filename = db.scalar(select(BucketFile.filename).where(
            BucketFile.id == file_id, BucketFile.data_source_id == source.id))
        if filename and filename.lower().endswith((".xlsx", ".xlsm")):
            from ..distillation_sample_schemas import DatabaseSampleArguments
            from .distillation_sample_service import read_sample

            return read_sample(db, scenario_id, DatabaseSampleArguments(
                data_source_id=source_id, bucket_file_id=file_id))
    evidence = Evidence(key="library_" + uuid.uuid4().hex[:20], title=_safe_label(source.name),
        kind="material", data_source_id=source.id, bucket_file_id=file_id,
        coverage="已通过受控工具读取的有界资料内容或结构", limitations="结构与文本不能单独证明业务结果；具体读取限制见调查记录。")
    document = DistillationDocument(evidence=[evidence])
    distillation_service.validate_document(db, document, scenario_id)
    identity = capture_evidence_identity(db, document, scenario_id)
    materials, databases, limits = analysis._collect_materials(db, document, scenario_id)
    db.commit()
    schemas, schema_limits = analysis._database_schemas(databases)
    permission_service.refresh_request_authorization(db)
    if capture_evidence_identity(db, document, scenario_id) != identity:
        raise HTTPException(409, "读取期间资料内容或连接已变化，请重新调查")
    content = {"evidence_key": evidence.key, "materials": materials, "database_schemas": schemas,
        "limitations": limits + schema_limits}
    if len(json.dumps(content, ensure_ascii=False).encode()) > 220_000:
        raise HTTPException(422, "资料读取结果超过本轮边界，请选择具体文件")
    return LibraryRead(evidence, identity, content)


def assert_read_current(db: Session, record: dict, scenario_id: str | None) -> None:
    evidence = Evidence.model_validate(record["evidence"]).model_copy(update={"library_read": None})
    current = capture_evidence_identity(db, DistillationDocument(evidence=[evidence]), scenario_id)
    if current != record["identity"]:
        raise HTTPException(409, "调查使用的资料已更新，旧建议不能作为当前资料结果采用，请重新读取")


def resolve_read(db: Session, evidence: Evidence, scenario_id: str | None) -> dict:
    from ..distillation_conversation_models import DistillationConversationTurn
    from ..distillation_models import DistillationProject

    reference = evidence.library_read
    principal = permission_service.require_principal(db)
    row = db.scalar(select(DistillationConversationTurn).where(
        DistillationConversationTurn.id == reference.turn_id, DistillationConversationTurn.tenant_id == principal.tenant_id))
    project = db.get(DistillationProject, row.project_id) if row else None
    if project is None or (project.scenario_id is not None and project.scenario_id != scenario_id):
        raise HTTPException(404, "资料调查回执不存在")
    distillation_service.authorize_scope(db, project.scenario_id)
    record = next((item for item in row.context.get("library_reads", [])
        if item["step_id"] == reference.step_id
        and Evidence.model_validate(item["evidence"]).key == evidence.key), None)
    if record is None or identity_hash(record["identity"]) != reference.identity_sha256:
        raise HTTPException(422, "资料调查回执无效")
    original = Evidence.model_validate(record["evidence"])
    if (evidence.data_source_id, evidence.bucket_file_id, evidence.key) != (original.data_source_id, original.bucket_file_id, original.key):
        raise HTTPException(422, "资料引用与实际读取回执不一致")
    assert_read_current(db, record, scenario_id)
    return record
