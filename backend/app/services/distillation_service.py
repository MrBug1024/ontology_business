"""Authorized discovery drafts and atomic, immutable modeling handoffs."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..distillation_models import (
    DistillationProject,
    DistillationPublication,
    DistillationScenarioState,
)
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


def scenario_state(
    db: Session, scenario_id: str, *, write: bool = False, lock: bool = False, create: bool = True,
) -> DistillationScenarioState | None:
    principal = permission_service.require_principal(db)
    authorize_scope(db, scenario_id, write=write)
    query = select(DistillationScenarioState).where(
        DistillationScenarioState.scenario_id == scenario_id,
        DistillationScenarioState.tenant_id == principal.tenant_id,
    )
    if lock:
        query = query.with_for_update()
    row = db.scalar(query)
    if row is None and write and create:
        row = DistillationScenarioState(
            tenant_id=principal.tenant_id,
            scenario_id=scenario_id,
            document=DistillationDocument().model_dump(),
            created_by=principal.user_id,
            updated_by=principal.user_id,
        )
        db.add(row)
        db.flush()
    return row


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
    document = payload.document
    if payload.scenario_id:
        state = scenario_state(db, payload.scenario_id, write=True, lock=True, create=False)
        if state is None:
            state = DistillationScenarioState(
                tenant_id=principal.tenant_id,
                scenario_id=payload.scenario_id,
                document=payload.document.model_dump(),
                created_by=principal.user_id,
                updated_by=principal.user_id,
            )
            db.add(state)
            db.flush()
        else:
            document = DistillationDocument.model_validate(state.document)
    validate_document(db, document, payload.scenario_id)
    if release_service.safe_snapshot_content({"name": payload.name}) != {"name": payload.name}:
        raise HTTPException(422, "项目名称不能包含凭据")
    row = DistillationProject(tenant_id=principal.tenant_id, scenario_id=payload.scenario_id,
        name=payload.name, document=document.model_dump(), created_by=principal.user_id,
        updated_by=principal.user_id)
    db.add(row)
    db.flush()
    return row


def update_project(db: Session, project_id: str, payload: ProjectUpdate) -> DistillationProject:
    from ..distillation_conversation_models import DistillationConversationTurn
    from ..distillation_attachment_models import DistillationAttachment

    principal = permission_service.require_principal(db)
    row = project(db, project_id, write=True, lock=True)
    assert_revision(row, payload.expected_revision)
    authorize_scope(db, payload.scenario_id, write=True)
    if row.scenario_id != payload.scenario_id:
        published = db.scalar(select(DistillationPublication.id).where(DistillationPublication.project_id == row.id).limit(1))
        investigated = db.scalar(select(DistillationConversationTurn.id).where(DistillationConversationTurn.project_id == row.id).limit(1))
        attached = db.scalar(select(DistillationAttachment.id).where(DistillationAttachment.project_id == row.id).limit(1))
        if published or investigated or attached:
            raise HTTPException(409, "已有附件、调查历史或交接记录的项目不能改变场景归属，请创建新项目")
    scenario_state_row = None
    if payload.scenario_id:
        scenario_state_row = scenario_state(db, payload.scenario_id, write=True, lock=True, create=False)
        if scenario_state_row is None:
            scenario_state_row = DistillationScenarioState(
                tenant_id=principal.tenant_id,
                scenario_id=payload.scenario_id,
                document=payload.document.model_dump(),
                created_by=principal.user_id,
                updated_by=principal.user_id,
            )
            db.add(scenario_state_row)
            db.flush()
        elif scenario_state_row.document != row.document:
            raise HTTPException(409, "场景业务蒸馏基线已变化，请刷新会话后合并")
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
    if scenario_state_row is not None:
        scenario_state_row.document = payload.document.model_dump()
        scenario_state_row.revision += 1
        scenario_state_row.updated_at = datetime.now(timezone.utc)
        scenario_state_row.updated_by = permission_service.require_principal(db).user_id
        db.flush()
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
        project_id=row.id, scenario_id=row.scenario_id, project_revision=row.revision, data_source_id=source_id,
        document=document.model_dump(), artifacts=artifacts,
        created_by=permission_service.require_principal(db).user_id)
    db.add(publication)
    db.flush()
    return publication


def list_scenario_publications(
    db: Session, scenario_id: str, *, limit: int = 50, offset: int = 0,
) -> list[DistillationPublication]:
    principal = permission_service.require_principal(db)
    authorize_scope(db, scenario_id)
    return list(db.scalars(select(DistillationPublication).where(
        DistillationPublication.tenant_id == principal.tenant_id,
        DistillationPublication.scenario_id == scenario_id,
    ).order_by(DistillationPublication.created_at.desc(), DistillationPublication.id.desc()).offset(offset).limit(limit)))


def scenario_publication(
    db: Session, scenario_id: str, publication_id: str, *, write: bool = False,
) -> DistillationPublication:
    principal = permission_service.require_principal(db)
    authorize_scope(db, scenario_id, write=write)
    row = db.scalar(select(DistillationPublication).where(
        DistillationPublication.id == publication_id,
        DistillationPublication.tenant_id == principal.tenant_id,
        DistillationPublication.scenario_id == scenario_id,
    ))
    if row is None:
        raise HTTPException(404, "业务蒸馏产物不存在")
    return row


def delete_publication(
    db: Session,
    project_id: str,
    publication_id: str,
) -> DistillationPublication:
    owning_project = project(db, project_id, write=True)
    publication = db.scalar(
        select(DistillationPublication)
        .where(
            DistillationPublication.id == publication_id,
            DistillationPublication.project_id == project_id,
            DistillationPublication.tenant_id == owning_project.tenant_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if publication is None:
        raise HTTPException(404, "业务蒸馏产物不存在")
    return _delete_publication_row(db, publication)


def delete_scenario_publication(
    db: Session,
    scenario_id: str,
    publication_id: str,
) -> DistillationPublication:
    """Delete a product even after its source project was removed.

    A publication is a user-owned handoff product.  Its catalog projection is
    disposable and is removed with it when the product is explicitly deleted;
    deleting that projection alone keeps the publication row and document.
    """

    principal = permission_service.require_principal(db)
    authorize_scope(db, scenario_id, write=True)
    publication = db.scalar(
        select(DistillationPublication)
        .where(
            DistillationPublication.id == publication_id,
            DistillationPublication.scenario_id == scenario_id,
            DistillationPublication.tenant_id == principal.tenant_id,
        )
        .with_for_update()
    )
    if publication is None:
        raise HTTPException(404, "业务蒸馏产物不存在")
    return _delete_publication_row(db, publication)


def delete_publication_by_id(
    db: Session,
    publication_id: str,
) -> DistillationPublication:
    """Delete one tenant-owned handoff through its own governed scope."""

    principal = permission_service.require_principal(db)
    publication = db.scalar(
        select(DistillationPublication)
        .where(
            DistillationPublication.id == publication_id,
            DistillationPublication.tenant_id == principal.tenant_id,
        )
        .with_for_update()
    )
    if publication is None:
        raise HTTPException(404, "业务蒸馏产物不存在")
    if publication.scenario_id:
        authorize_scope(db, publication.scenario_id, write=True)
    elif publication.project_id:
        project(db, publication.project_id, write=True)
    else:
        permission_service.require_tenant_permission(db, "write")
    return _delete_publication_row(db, publication)


def detach_publication_data_source(
    db: Session,
    publication: DistillationPublication,
) -> None:
    """Remove only the disposable modeling projection from a publication."""

    detached = db.scalar(select(func.detach_distillation_publication(
        publication.id,
        publication.tenant_id,
        "data_source",
    )))
    if not detached:
        raise HTTPException(404, "业务蒸馏产物不存在")
    db.refresh(publication)


def detach_publication_project(
    db: Session,
    publication: DistillationPublication,
) -> None:
    """Keep a publication readable after its editable project is removed."""

    detached = db.scalar(select(func.detach_distillation_publication(
        publication.id,
        publication.tenant_id,
        "project",
    )))
    if not detached:
        raise HTTPException(404, "业务蒸馏产物不存在")
    db.refresh(publication)


def publication_by_id(
    db: Session,
    publication_id: str,
    *,
    write: bool = False,
) -> DistillationPublication:
    """Resolve a handoff without requiring its source project to survive."""

    principal = permission_service.require_principal(db)
    row = db.scalar(select(DistillationPublication).where(
        DistillationPublication.id == publication_id,
        DistillationPublication.tenant_id == principal.tenant_id,
    ))
    if row is None:
        raise HTTPException(404, "业务蒸馏产物不存在")
    if row.scenario_id:
        authorize_scope(db, row.scenario_id, write=write)
    elif row.project_id:
        project(db, row.project_id, write=write)
    else:
        permission_service.require_tenant_permission(db, "write" if write else "read")
    return row


def _delete_publication_row(
    db: Session,
    publication: DistillationPublication,
) -> DistillationPublication:
    deleted = db.scalar(select(func.delete_distillation_publication(
        publication.id,
        publication.tenant_id,
    )))
    if not deleted:
        raise HTTPException(404, "业务蒸馏产物不存在")
    db.flush()
    return publication


def delete_publication_record(
    db: Session,
    publication: DistillationPublication,
) -> None:
    """Delete a locked publication through the tenant-bound database function."""

    _delete_publication_row(db, publication)


def delete_project(db: Session, project_id: str) -> None:
    from ..distillation_access_models import DistillationSystemAccess
    from ..distillation_attachment_models import DistillationAttachment, DistillationTurnAttachment
    from ..distillation_conversation_models import DistillationConversationTurn

    row = project(db, project_id, write=True, lock=True)
    if db.scalar(select(DistillationConversationTurn.id).where(
        DistillationConversationTurn.project_id == row.id,
        DistillationConversationTurn.status.in_(("queued", "running")),
    ).limit(1)):
        raise HTTPException(409, "会话调查仍在进行，完成或取消后再删除")
    publications = list(db.scalars(select(DistillationPublication).where(
        DistillationPublication.project_id == row.id,
        DistillationPublication.tenant_id == row.tenant_id,
    ).with_for_update()))
    for publication in publications:
        detach_publication_project(db, publication)
    db.execute(DistillationTurnAttachment.__table__.delete().where(
        DistillationTurnAttachment.project_id == row.id,
        DistillationTurnAttachment.tenant_id == row.tenant_id,
    ))
    db.execute(DistillationConversationTurn.__table__.delete().where(
        DistillationConversationTurn.project_id == row.id,
        DistillationConversationTurn.tenant_id == row.tenant_id,
    ))
    db.execute(DistillationAttachment.__table__.delete().where(
        DistillationAttachment.project_id == row.id,
        DistillationAttachment.tenant_id == row.tenant_id,
    ))
    db.execute(DistillationSystemAccess.__table__.delete().where(
        DistillationSystemAccess.project_id == row.id,
        DistillationSystemAccess.tenant_id == row.tenant_id,
    ))
    db.delete(row)
    db.flush()


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
    publication_group = func.coalesce(
        DistillationPublication.project_id,
        DataSource.config["distillation_project_id"].as_string(),
    )
    query = select(DistillationPublication, DataSource).join(DataSource,
        DataSource.id == DistillationPublication.data_source_id).where(
        DistillationPublication.tenant_id == principal.tenant_id,
        DataSource.tenant_id == principal.tenant_id, DataSource.resource_scope == "modeling",
        DataSource.type == "distillation",
        DataSource.scenario_id == scenario_id,
    ).distinct(publication_group).order_by(
        publication_group, DistillationPublication.project_revision.desc(),
        DistillationPublication.created_at.desc(), DistillationPublication.id.desc()).limit(21)
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
