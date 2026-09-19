"""Authorized discovery drafts and atomic, immutable modeling handoffs."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..distillation_models import DistillationProject, DistillationPublication
from ..distillation_schemas import DistillationDocument, ProjectCreate, ProjectUpdate
from ..models import BucketFile, BusinessScenario, DataSource
from . import permission_service, release_service
from .distillation_artifact_service import generate_artifacts


def authorize_scope(db: Session, scenario_id: str | None, *, write: bool = False) -> None:
    principal = permission_service.require_principal(db)
    if scenario_id:
        scenario = db.scalar(select(BusinessScenario).where(
            BusinessScenario.id == scenario_id, BusinessScenario.tenant_id == principal.tenant_id,
        ))
        if scenario is None:
            raise HTTPException(404, "业务场景不存在")
        permission_service.require_scenario_permission(db, scenario, "write" if write else "read")
        if write and scenario.status == "retired":
            raise HTTPException(409, "该场景已退役")
    else:
        permission_service.require_tenant_permission(db, "write" if write else "read")


def project(db: Session, project_id: str, *, write: bool = False, lock: bool = False) -> DistillationProject:
    principal = permission_service.require_principal(db)
    query = select(DistillationProject).where(
        DistillationProject.id == project_id, DistillationProject.tenant_id == principal.tenant_id,
    ).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update()
    row = db.scalar(query)
    if row is None:
        raise HTTPException(404, "业务蒸馏项目不存在")
    authorize_scope(db, row.scenario_id, write=write)
    return row


def assert_revision(row: DistillationProject, expected_revision: int) -> None:
    if row.revision != expected_revision:
        raise HTTPException(409, "项目已被更新，请保留当前草稿并刷新后合并")


def can_write(db: Session, row: DistillationProject) -> bool:
    try:
        authorize_scope(db, row.scenario_id, write=True)
        return True
    except HTTPException:
        return False


def evidence_source(db: Session, source_id: str, scenario_id: str | None) -> DataSource:
    principal = permission_service.require_principal(db)
    source = db.scalar(select(DataSource).where(
        DataSource.id == source_id, DataSource.tenant_id == principal.tenant_id,
        DataSource.resource_scope == "modeling",
    ))
    if source is None:
        raise HTTPException(404, "建模资料不存在")
    authorize_scope(db, source.scenario_id)
    if source.scenario_id and source.scenario_id != scenario_id:
        raise HTTPException(422, "证据必须来自项目所属场景或工作区共享资料")
    return source


def validate_document(db: Session, document: DistillationDocument, scenario_id: str | None) -> None:
    content = document.model_dump()
    if release_service.safe_snapshot_content(content) != content:
        raise HTTPException(422, "蒸馏文档包含疑似凭据，请移除密码、令牌或带凭据的连接地址")
    for evidence in document.evidence:
        if evidence.interview:
            from .distillation_interview_service import resolve

            resolve(db, evidence, scenario_id)
        if evidence.mcp_read:
            from .distillation_mcp_evidence_service import resolve_read as resolve_mcp_read

            resolve_mcp_read(db, evidence, scenario_id)
        if evidence.investigation_source:
            from .distillation_evidence_service import investigation_observation

            investigation_observation(db, evidence, scenario_id)
        if evidence.library_read:
            from .distillation_library_service import resolve_read

            resolve_read(db, evidence, scenario_id)
        if evidence.data_source_id:
            source = evidence_source(db, evidence.data_source_id, scenario_id)
            if evidence.bucket_file_id:
                file_id = db.scalar(select(BucketFile.id).where(
                    BucketFile.id == evidence.bucket_file_id, BucketFile.data_source_id == source.id,
                ))
                if file_id is None or source.type not in {"file_bucket", "sqlite3"}:
                    raise HTTPException(404, "证据文件不存在")


def create_project(db: Session, payload: ProjectCreate) -> DistillationProject:
    principal = permission_service.require_principal(db)
    authorize_scope(db, payload.scenario_id, write=True)
    validate_document(db, payload.document, payload.scenario_id)
    if release_service.safe_snapshot_content({"name": payload.name}) != {"name": payload.name}:
        raise HTTPException(422, "项目名称不能包含凭据")
    row = DistillationProject(tenant_id=principal.tenant_id, scenario_id=payload.scenario_id,
        name=payload.name, document=payload.document.model_dump(), created_by=principal.user_id,
        updated_by=principal.user_id)
    db.add(row)
    db.flush()
    return row


def update_project(db: Session, project_id: str, payload: ProjectUpdate) -> DistillationProject:
    from ..distillation_conversation_models import DistillationConversationTurn
    from ..distillation_attachment_models import DistillationAttachment

    row = project(db, project_id, write=True, lock=True)
    assert_revision(row, payload.expected_revision)
    authorize_scope(db, payload.scenario_id, write=True)
    if row.scenario_id != payload.scenario_id:
        published = db.scalar(select(DistillationPublication.id).where(DistillationPublication.project_id == row.id).limit(1))
        investigated = db.scalar(select(DistillationConversationTurn.id).where(DistillationConversationTurn.project_id == row.id).limit(1))
        attached = db.scalar(select(DistillationAttachment.id).where(DistillationAttachment.project_id == row.id).limit(1))
        if published or investigated or attached:
            raise HTTPException(409, "已有附件、调查历史或交接记录的项目不能改变场景归属，请创建新项目")
    validate_document(db, payload.document, payload.scenario_id)
    if release_service.safe_snapshot_content({"name": payload.name}) != {"name": payload.name}:
        raise HTTPException(422, "项目名称不能包含凭据")
    result = db.execute(update(DistillationProject).where(
        DistillationProject.id == row.id, DistillationProject.tenant_id == row.tenant_id,
        DistillationProject.revision == payload.expected_revision,
    ).values(name=payload.name, scenario_id=payload.scenario_id, document=payload.document.model_dump(),
             revision=payload.expected_revision + 1, updated_at=datetime.now(timezone.utc),
             updated_by=permission_service.require_principal(db).user_id).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise HTTPException(409, "项目已被更新，请保留当前草稿并刷新后合并")
    return project(db, project_id, write=True)


def publish(db: Session, project_id: str, expected_revision: int) -> DistillationPublication:
    # A short row lock serializes publish with edit. No external I/O is performed
    # in this transaction; all generated files and their catalog entry commit once.
    row = project(db, project_id, write=True, lock=True)
    assert_revision(row, expected_revision)
    existing = db.scalar(select(DistillationPublication).where(
        DistillationPublication.project_id == row.id,
        DistillationPublication.project_revision == row.revision,
    ))
    if existing is not None:
        return existing
    document = DistillationDocument.model_validate(row.document)
    validate_document(db, document, row.scenario_id)
    from .distillation_evidence_service import capture_evidence_identity

    evidence_identity = capture_evidence_identity(db, document, row.scenario_id)
    if not all((document.beneficiary, document.pain, document.desired_outcome,
                document.success_metric, document.decision_reason)) or document.decision == "undecided":
        raise HTTPException(422, "交接前请填写受益者、痛点、期望结果、成功标准和人工决策理由")
    publication_id, source_id = uuid.uuid4().hex, uuid.uuid4().hex
    source = DataSource(id=source_id, tenant_id=row.tenant_id, scenario_id=row.scenario_id,
        name=f"{row.name[:160]} · 业务蒸馏 v{row.revision}", type="distillation", resource_scope="modeling",
        config={"distillation_project_id": row.id, "publication_id": publication_id, "project_revision": row.revision},
        status="ok", is_public=False)
    db.add(source)
    db.flush()
    artifacts = generate_artifacts(row.name, row.revision, document)
    provenance = json.dumps(evidence_identity, ensure_ascii=False, sort_keys=True, indent=2)
    artifacts.append({"key": "provenance", "filename": "evidence-provenance.json", "mime": "application/json",
                      "content": provenance, "sha256": hashlib.sha256(provenance.encode("utf-8")).hexdigest()})
    publication = DistillationPublication(id=publication_id, tenant_id=row.tenant_id,
        project_id=row.id, project_revision=row.revision, data_source_id=source_id,
        document=document.model_dump(), artifacts=artifacts,
        created_by=permission_service.require_principal(db).user_id)
    db.add(publication)
    db.flush()
    return publication


def artifact_content(publication: DistillationPublication, artifact_key: str) -> dict[str, str]:
    for artifact in publication.artifacts:
        if artifact["key"] == artifact_key:
            if hashlib.sha256(artifact["content"].encode("utf-8")).hexdigest() != artifact["sha256"]:
                raise HTTPException(409, "资料完整性校验失败")
            return dict(artifact)
    raise HTTPException(404, "交接文件不存在")


def modeling_documents(db: Session, scenario_id: str) -> list[dict]:
    """Only explicitly scenario-bound handoffs are injected into its advisor."""
    principal = permission_service.require_principal(db)
    authorize_scope(db, scenario_id)
    query = select(DistillationPublication, DataSource).join(DataSource,
        DataSource.id == DistillationPublication.data_source_id).where(
        DistillationPublication.tenant_id == principal.tenant_id,
        DataSource.tenant_id == principal.tenant_id, DataSource.resource_scope == "modeling",
        DataSource.type == "distillation",
        DataSource.scenario_id == scenario_id,
    ).distinct(DistillationPublication.project_id).order_by(
        DistillationPublication.project_id, DistillationPublication.project_revision.desc()).limit(21)
    rows = db.execute(query).all()
    if len(rows) > 20:
        raise HTTPException(422, "业务蒸馏交接资料超过单次 20 个项目，请缩小场景资料范围")
    documents = []
    for publication, source in rows:
        artifact = artifact_content(publication, "brief")
        documents.append({"id": f"distillation:{publication.id}", "filename": source.name,
            "status": "parsed", "parsed_text": artifact["content"], "usage_plane": "modeling_material",
            "semantic_role": "business_distillation_handoff", "data_source_id": source.id,
            "business_decision": publication.document.get("decision", "undecided"),
            "publication_id": publication.id, "content_hash": artifact["sha256"]})
    return documents
