"""Freeze bounded evidence identities without copying connector secrets."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..distillation_models import DistillationPublication
from ..distillation_schemas import DistillationDocument, Evidence
from ..models import BucketFile
from . import distillation_service, release_service


def investigation_observation(db: Session, evidence: Evidence, scenario_id: str | None) -> dict:
    from ..distillation_conversation_models import DistillationConversationTurn
    from ..distillation_models import DistillationProject
    from . import permission_service

    reference = evidence.investigation_source
    if reference is None:
        raise HTTPException(422, "缺少实际调查来源")
    principal = permission_service.require_principal(db)
    row = db.scalar(select(DistillationConversationTurn).where(
        DistillationConversationTurn.id == reference.turn_id,
        DistillationConversationTurn.tenant_id == principal.tenant_id))
    project = db.get(DistillationProject, row.project_id) if row else None
    if project is None or (project.scenario_id is not None and project.scenario_id != scenario_id):
        raise HTTPException(404, "调查来源不存在")
    distillation_service.authorize_scope(db, project.scenario_id)
    step = next((item for item in row.steps if item["id"] == reference.step_id
        and item["status"] == "succeeded" and item.get("source")), None)
    if step is None or step["source"]["content_sha256"] != reference.content_sha256:
        raise HTTPException(422, "调查来源没有匹配的实际读取回执")
    return {"turn_id": row.id, "step_id": step["id"], **step["source"]}


def capture_evidence_identity(db: Session, document: DistillationDocument, scenario_id: str | None) -> dict:
    items = []
    file_count = 0
    for evidence in document.evidence:
        item = {"evidence_key": evidence.key, "kind": evidence.kind, "role": evidence.role}
        if evidence.interview:
            from .distillation_interview_service import resolve

            item.update(basis="human_interview", reference=evidence.interview.model_dump(), observation=resolve(db, evidence, scenario_id))
            items.append(item)
            continue
        if evidence.mcp_read:
            from .distillation_mcp_evidence_service import resolve_read as resolve_mcp_read

            record = resolve_mcp_read(db, evidence, scenario_id)
            item.update(basis="server_readonly_mcp_observation", reference=evidence.mcp_read.model_dump(), **record)
            items.append(item)
            continue
        if evidence.library_read:
            from .distillation_library_service import resolve_read

            record = resolve_read(db, evidence, scenario_id)
            item.update(basis="server_library_read", receipt=evidence.library_read.model_dump(),
                frozen_identity=record["identity"], reading=record["content"])
            items.append(item)
            continue
        if evidence.investigation_source:
            item["basis"] = "server_readonly_web_observation"
            item["observation"] = investigation_observation(db, evidence, scenario_id)
            items.append(item)
            continue
        if not evidence.data_source_id:
            item["basis"] = "human_recorded_observation"
            items.append(item)
            continue
        source = distillation_service.evidence_source(db, evidence.data_source_id, scenario_id)
        item.update({"data_source_id": source.id, "source_type": source.type,
                     "connector_revision": source.connector_revision})
        if source.type in {"file_bucket", "sqlite3"}:
            # Hash text inside PostgreSQL so identity capture never materializes
            # complete documents in Python just to detect concurrent reparsing.
            text_hash = func.encode(func.sha256(func.convert_to(
                func.coalesce(BucketFile.parsed_text, ""), "UTF8")), "hex")
            query = select(BucketFile.id, BucketFile.filename, BucketFile.content_sha256,
                BucketFile.index_version, BucketFile.indexed_content_hash, BucketFile.status,
                text_hash.label("parsed_text_sha256")).where(BucketFile.data_source_id == source.id)
            if evidence.bucket_file_id:
                query = query.where(BucketFile.id == evidence.bucket_file_id)
            rows = db.execute(query.order_by(BucketFile.id).limit(101 - file_count)).mappings().all()
            if evidence.bucket_file_id and not rows:
                raise HTTPException(404, "证据文件不存在")
            file_count += len(rows)
            if file_count > 100:
                raise HTTPException(422, "证据文件超过本次 100 个边界，请选择具体文件缩小范围")
            files = []
            for row in rows:
                file = dict(row)
                filename = release_service.safe_snapshot_content({"filename": file["filename"]}).get("filename")
                file["filename"] = filename if isinstance(filename, str) else "已隐藏敏感文件名"
                files.append(file)
            item["files"] = files
        elif source.type == "distillation":
            publication = db.scalar(select(DistillationPublication).where(
                DistillationPublication.data_source_id == source.id,
                DistillationPublication.tenant_id == source.tenant_id,
            ))
            if publication is None:
                raise HTTPException(404, "交接证据不存在")
            item["publication_id"] = publication.id
            item["content_sha256"] = distillation_service.artifact_content(publication, "brief")["sha256"]
        else:
            item["coverage"] = "connector configuration revision; live database content is not frozen"
        items.append(item)
    return {"identity_version": "business-distillation-evidence:v1", "evidence": items}
