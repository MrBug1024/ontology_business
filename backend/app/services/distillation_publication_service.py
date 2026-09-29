"""One material handoff path for conversations and retained scenario products."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..distillation_models import DistillationPublication
from ..distillation_schemas import DistillationDocument, ScenarioPublishRequest
from ..models import BusinessScenario, DataSource
from . import permission_service
from .distillation_artifact_service import generate_artifacts


def create_publication(db: Session, *, tenant_id: str, scenario_id: str, project_id: str | None,
                       revision: int, name: str, document: DistillationDocument,
                       scenario_state_id: str | None = None) -> DistillationPublication:
    from . import distillation_service
    from .distillation_evidence_service import capture_evidence_identity

    distillation_service.validate_document(db, document, scenario_id)
    if not all((document.beneficiary, document.pain, document.desired_outcome,
                document.success_metric, document.decision_reason)) or document.decision == "undecided":
        raise HTTPException(422, "交接前请填写受益者、痛点、期望结果、成功标准和人工决策理由")
    evidence_identity = capture_evidence_identity(db, document, scenario_id)
    publication_id, source_id = uuid.uuid4().hex, uuid.uuid4().hex
    source = DataSource(id=source_id, tenant_id=tenant_id, scenario_id=scenario_id,
        name=f"{name[:160]} · 业务蒸馏 v{revision}", type="distillation", resource_scope="modeling",
        config={"distillation_project_id": project_id, "publication_id": publication_id,
                "project_revision": revision, **({"distillation_scenario_state_id": scenario_state_id}
                                                 if scenario_state_id else {})}, status="ok", is_public=False)
    db.add(source)
    db.flush()
    artifacts = generate_artifacts(name, revision, document)
    provenance = json.dumps(evidence_identity, ensure_ascii=False, sort_keys=True, indent=2)
    artifacts.append({"key": "provenance", "filename": "evidence-provenance.json", "mime": "application/json",
                      "content": provenance, "sha256": hashlib.sha256(provenance.encode("utf-8")).hexdigest()})
    publication = DistillationPublication(id=publication_id, tenant_id=tenant_id,
        project_id=project_id, scenario_id=scenario_id, project_revision=revision, data_source_id=source_id,
        document=document.model_dump(), artifacts=artifacts,
        created_by=permission_service.require_principal(db).user_id)
    db.add(publication)
    db.flush()
    return publication


def publish_scenario(db: Session, scenario_id: str, payload: ScenarioPublishRequest) -> DistillationPublication:
    from . import distillation_service

    # The persistent scenario row serializes simultaneous handoffs and edits.
    # A deleted conversation is not a prerequisite for accessing retained work.
    state = distillation_service.scenario_state(db, scenario_id, write=True, lock=True, create=False)
    if state is None:
        raise HTTPException(422, "当前场景还没有可交接的阶段成果")
    if state.revision != payload.expected_revision:
        raise HTTPException(409, "场景阶段成果已变化，请保留当前决定并刷新后核对")
    document = DistillationDocument.model_validate({**state.document,
        "decision": payload.decision, "decision_reason": payload.decision_reason})
    if document.model_dump() != state.document:
        state.document = document.model_dump()
        state.revision += 1
        state.updated_at = datetime.now(timezone.utc)
        state.updated_by = permission_service.require_principal(db).user_id
        db.flush()
    existing = db.scalar(select(DistillationPublication).join(DataSource,
        DataSource.id == DistillationPublication.data_source_id).where(
        DistillationPublication.tenant_id == state.tenant_id,
        DistillationPublication.scenario_id == scenario_id,
        DistillationPublication.project_id.is_(None),
        DataSource.tenant_id == state.tenant_id,
        DataSource.config["distillation_scenario_state_id"].as_string() == state.id,
        DistillationPublication.project_revision == state.revision))
    if existing is not None:
        return existing
    scenario = db.scalar(select(BusinessScenario).where(
        BusinessScenario.id == scenario_id, BusinessScenario.tenant_id == state.tenant_id))
    return create_publication(db, tenant_id=state.tenant_id, scenario_id=scenario_id,
        project_id=None, revision=state.revision, name=scenario.name, document=document, scenario_state_id=state.id)
