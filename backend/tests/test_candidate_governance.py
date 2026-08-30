from __future__ import annotations

import hashlib

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    BusinessScenario,
    OntologyAction,
    OntologyEntity,
    OntologyEvent,
    OntologyProperty,
    ScenarioModelDraftResource,
    Tenant,
    User,
)
from app.routers import scenarios
from app.schemas import ScenarioModelCandidateBatchPromotionRequest
from app.services import candidate_governance_service


def _draft(
    *,
    tenant_id: str,
    scenario_id: str,
    user_id: str,
    kind: str,
    key: str,
    payload: dict,
    source: str,
    status: str = "ready_for_review",
    revision: int = 0,
) -> ScenarioModelDraftResource:
    return ScenarioModelDraftResource(
        tenant_id=tenant_id,
        scenario_id=scenario_id,
        created_by_user_id=user_id,
        proposal_id=f"proposal-{source}-{key}"[:64],
        task_id="candidate-test",
        resource_kind=kind,
        resource_key=key,
        resource_identity=hashlib.sha256(
            f"{kind}\0{key}".encode("utf-8")
        ).hexdigest(),
        title=str(payload.get("name") or key),
        source_payload=payload,
        payload=payload,
        validation_issues=[],
        source_refs=[],
        materialization_source=source,
        draft_status=status,
        enabled=False,
        publishable=False,
        revision=revision,
    )


def _session() -> tuple[Session, Tenant, User, BusinessScenario]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)
    tenant = Tenant(id="tenant-candidate", name="Candidate governance")
    user = User(
        id="user-candidate",
        tenant_id=tenant.id,
        email="candidate@example.test",
        password_hash="x",
        status="active",
    )
    scenario = BusinessScenario(
        id="scenario-candidate",
        tenant_id=tenant.id,
        name="Candidate governance",
    )
    db.add_all([tenant, user, scenario])
    db.commit()
    db.info["tenant_id"] = tenant.id
    db.info["user_id"] = user.id
    return db, tenant, user, scenario


def test_origin_is_provenance_and_does_not_change_quality_state() -> None:
    payload = {
        "name": "Review event",
        "payload_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "enabled": False,
    }
    assistant = _draft(
        tenant_id="tenant",
        scenario_id="scenario",
        user_id="user",
        kind="event",
        key="event.reviewed",
        payload=payload,
        source="compiler_sidecar",
    )
    manual = _draft(
        tenant_id="tenant",
        scenario_id="scenario",
        user_id="user",
        kind="event",
        key="event.reviewed",
        payload=payload,
        source="manual",
    )

    assistant_quality = candidate_governance_service.governance_projection(
        assistant
    )
    manual_quality = candidate_governance_service.governance_projection(manual)

    assert assistant_quality["source_origin"] == "assistant"
    assert manual_quality["source_origin"] == "manual"
    assert assistant_quality["promotion_eligible"] is True
    assert manual_quality["promotion_eligible"] is True
    assert (
        assistant_quality["quality_fingerprint"]
        == manual_quality["quality_fingerprint"]
    )


def test_revalidate_and_promote_action_keeps_formal_action_inactive() -> None:
    db, tenant, user, scenario = _session()
    try:
        entity = OntologyEntity(
            id="entity-action-parent",
            scenario_id=scenario.id,
            name="Work item",
            api_name="work_item",
        )
        db.add(entity)
        db.commit()
        row = candidate_governance_service.create_manual_candidate(
            db,
            scenario,
            tenant_id=tenant.id,
            created_by_user_id=user.id,
            resource_kind="action",
            resource_key="action.review",
            title="Review",
            payload={
                "name": "Review",
                "entity_id": entity.id,
                "description": "Review one work item",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                "executor_type": "unbound",
                "executor_config": {},
                "enabled": True,
            },
        )
        db.commit()

        row, evaluation = candidate_governance_service.revalidate_candidate(
            db,
            scenario,
            tenant_id=tenant.id,
            created_by_user_id=user.id,
            draft_id=row.id,
            expected_revision=0,
        )
        assert evaluation.eligible is True
        assert row.draft_status == "ready_for_review"
        assert any(
            issue["code"] == "activation_deferred"
            and issue["blocking"] is False
            for issue in row.validation_issues
        )
        db.commit()

        rows, result = candidate_governance_service.promote_candidates(
            db,
            scenario,
            tenant_id=tenant.id,
            created_by_user_id=user.id,
            expected_revisions={row.id: 1},
        )
        db.commit()

        action = db.scalars(select(OntologyAction).where(
            OntologyAction.scenario_id == scenario.id,
            OntologyAction.name == "Review",
        )).one()
        assert action.enabled is False
        assert action.executor_type == "unbound"
        assert rows[0].draft_status == "resolved"
        assert rows[0].resolved_resource_id == action.id
        assert result["promoted"][0]["activation_status"] == "inactive"
    finally:
        db.close()


def test_assistant_action_cannot_promote_itself_active_or_drop_effect_gates() -> None:
    db, tenant, user, scenario = _session()
    try:
        entity = OntologyEntity(
            id="entity-assistant-action-parent",
            scenario_id=scenario.id,
            name="Work item",
            api_name="assistant_work_item",
        )
        candidate = _draft(
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            user_id=user.id,
            kind="action",
            key="action.assistant_requested_activation",
            payload={
                "name": "Apply reviewed change",
                "entity_id": entity.id,
                "description": "Apply a previously reviewed state change",
                "input_schema": {
                    "type": "object",
                    "properties": {"change_id": {"type": "string"}},
                    "required": ["change_id"],
                    "additionalProperties": False,
                },
                "executor_type": "unbound",
                "executor_config": {},
                "enabled": True,
                "requires_confirmation": True,
                "idempotency_required": True,
            },
            source="compiler_sidecar",
        )
        db.add_all([entity, candidate])
        db.commit()

        rows, result = candidate_governance_service.promote_candidates(
            db,
            scenario,
            tenant_id=tenant.id,
            created_by_user_id=user.id,
            expected_revisions={candidate.id: 0},
        )
        db.commit()

        action = db.scalars(
            select(OntologyAction).where(
                OntologyAction.scenario_id == scenario.id,
                OntologyAction.name == "Apply reviewed change",
            )
        ).one()
        assert rows[0].draft_status == "resolved"
        assert result["promoted"][0]["source_origin"] == "assistant"
        assert result["promoted"][0]["activation_status"] == "inactive"
        assert action.enabled is False
        assert action.executor_type == "unbound"
        assert action.requires_confirmation is True
        assert action.idempotency_required is True
    finally:
        db.close()


def test_single_property_candidate_promotes_through_formal_parent_preflight() -> None:
    db, tenant, user, scenario = _session()
    try:
        entity = OntologyEntity(
            id="entity-property-parent",
            scenario_id=scenario.id,
            name="Work item",
            api_name="work_item",
        )
        identifier = OntologyProperty(
            id="property-identifier",
            entity=entity,
            name="Identifier",
            api_name="identifier",
            data_type="string",
            is_key=True,
            is_title=True,
            is_required=True,
        )
        row = _draft(
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            user_id=user.id,
            kind="property",
            key="work_item:property:priority",
            payload={
                "entity_ref": entity.id,
                "name": "Priority",
                "api_name": "priority",
                "data_type": "integer",
                "is_required": False,
            },
            source="manual",
        )
        db.add_all([entity, identifier, row])
        db.commit()

        rows, _result = candidate_governance_service.promote_candidates(
            db,
            scenario,
            tenant_id=tenant.id,
            created_by_user_id=user.id,
            expected_revisions={row.id: 0},
        )
        db.commit()

        priority = db.scalars(select(OntologyProperty).where(
            OntologyProperty.entity_id == entity.id,
            OntologyProperty.api_name == "priority",
        )).one()
        assert rows[0].resolved_resource_id == priority.id
        assert rows[0].draft_status == "resolved"
    finally:
        db.close()


def test_batch_blocker_is_structured_and_persists_no_partial_definition() -> None:
    db, tenant, user, scenario = _session()
    try:
        event = _draft(
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            user_id=user.id,
            kind="event",
            key="event.valid",
            payload={
                "name": "Valid event",
                "payload_schema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                "enabled": False,
            },
            source="compiler_sidecar",
        )
        blocked_action = _draft(
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            user_id=user.id,
            kind="action",
            key="action.blocked",
            payload={
                "name": "Blocked action",
                "entity_id": "missing-entity",
                "executor_type": "unbound",
                "executor_config": {},
                "enabled": False,
            },
            source="manual",
        )
        db.add_all([event, blocked_action])
        db.commit()

        with pytest.raises(
            candidate_governance_service.CandidatePromotionBlocked
        ) as exc_info:
            candidate_governance_service.promote_candidates(
                db,
                scenario,
                tenant_id=tenant.id,
                created_by_user_id=user.id,
                expected_revisions={event.id: 0, blocked_action.id: 0},
            )

        assert exc_info.value.blockers
        assert exc_info.value.blockers[0]["code"] == "formal_preflight_failed"
        assert exc_info.value.blockers[0]["blocking"] is True
        assert db.scalars(select(OntologyEvent)).all() == []
        assert db.scalars(select(OntologyAction)).all() == []
        assert db.get(ScenarioModelDraftResource, event.id).draft_status == "ready_for_review"
        assert db.get(ScenarioModelDraftResource, blocked_action.id).draft_status == "ready_for_review"
    finally:
        db.close()


def test_batch_api_returns_machine_readable_blockers() -> None:
    db, tenant, user, scenario = _session()
    try:
        blocked = _draft(
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            user_id=user.id,
            kind="action",
            key="action.api_blocked",
            payload={
                "name": "API blocked action",
                "entity_id": "missing-entity",
                "executor_type": "unbound",
                "executor_config": {},
                "enabled": False,
            },
            source="compiler_sidecar",
        )
        db.add(blocked)
        db.commit()

        with pytest.raises(HTTPException) as exc_info:
            scenarios._promote_scenario_model_candidates(
                scenario,
                ScenarioModelCandidateBatchPromotionRequest(items=[{
                    "draft_id": blocked.id,
                    "expected_revision": 0,
                }]),
                db,
            )

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["code"] == "candidate_promotion_blocked"
        assert exc_info.value.detail["blockers"][0]["code"] == "formal_preflight_failed"
        assert db.scalars(select(OntologyAction)).all() == []
    finally:
        db.close()


def test_atomic_batch_resolves_generated_dependency_across_ai_and_manual_rows() -> None:
    db, tenant, user, scenario = _session()
    try:
        entity = _draft(
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            user_id=user.id,
            kind="entity",
            key="entity.work_item",
            payload={
                "name": "Work item",
                "api_name": "work_item",
                "is_abstract": False,
                "properties": [{
                    "name": "Identifier",
                    "api_name": "identifier",
                    "data_type": "string",
                    "is_key": True,
                    "is_title": True,
                    "is_required": True,
                    "_operation": "add",
                }],
            },
            source="compiler_sidecar",
        )
        action = _draft(
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            user_id=user.id,
            kind="action",
            key="action.review_work_item",
            payload={
                "name": "Review work item",
                "entity_id": "entity.work_item",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                "executor_type": "unbound",
                "executor_config": {},
                "enabled": False,
            },
            source="manual",
        )
        db.add_all([entity, action])
        db.commit()

        rows, result = candidate_governance_service.promote_candidates(
            db,
            scenario,
            tenant_id=tenant.id,
            created_by_user_id=user.id,
            expected_revisions={entity.id: 0, action.id: 0},
        )
        db.commit()

        formal_entity = db.scalars(select(OntologyEntity).where(
            OntologyEntity.scenario_id == scenario.id,
            OntologyEntity.api_name == "work_item",
        )).one()
        formal_action = db.scalars(select(OntologyAction).where(
            OntologyAction.scenario_id == scenario.id,
            OntologyAction.name == "Review work item",
        )).one()
        assert formal_action.entity_id == formal_entity.id
        assert formal_action.enabled is False
        assert {row.draft_status for row in rows} == {"resolved"}
        assert {item["source_origin"] for item in result["promoted"]} == {
            "assistant", "manual"
        }
        assert result["atomic"] is True
    finally:
        db.close()
