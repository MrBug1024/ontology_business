from __future__ import annotations

import hashlib

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app import database
from app.database import Base
from app.models import (
    BusinessScenario,
    DataMapping,
    DataSource,
    OntologyEntity,
    OntologyProperty,
    OntologyRelation,
    RelationDataMapping,
    ScenarioModelDraftResource,
    Tenant,
    User,
)
from app.routers import scenarios
from app.services import scenario_model_draft_service


def _draft(
    *,
    tenant_id: str,
    scenario_id: str,
    kind: str,
    key: str,
    payload: dict,
) -> ScenarioModelDraftResource:
    return ScenarioModelDraftResource(
        tenant_id=tenant_id,
        scenario_id=scenario_id,
        proposal_id="proposal-edited-draft",
        task_id="ontology",
        resource_kind=kind,
        resource_key=key,
        resource_identity=hashlib.sha256(f"{kind}\0{key}".encode("utf-8")).hexdigest(),
        title=key,
        source_payload=payload,
        payload=payload,
        validation_issues=[],
        source_refs=[],
        draft_status="needs_validation",
        enabled=False,
        publishable=False,
        revision=1,
    )


def test_edited_entity_and_generated_dependants_are_excluded_from_stale_apply() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        tenant = Tenant(id="tenant-draft-apply", name="Draft apply")
        scenario = BusinessScenario(
            id="scenario-draft-apply",
            tenant_id=tenant.id,
            name="Draft apply",
        )
        db.add_all([tenant, scenario])
        db.commit()
        edited = _draft(
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            kind="entity",
            key="entity.edited",
            payload={"key": "entity.edited", "name": "Edited working copy"},
        )
        db.add(edited)
        db.commit()

        payload = {
            "entities": [
                {"key": "entity.edited", "name": "Stale proposal value"},
                {"key": "entity.safe", "name": "Safe value"},
            ],
            "relations": [{
                "key": "relation.depends_on_edited",
                "source": {"kind": "generated", "key": "entity.edited"},
                "target": {"kind": "generated", "key": "entity.safe"},
            }],
            "functions": [],
            "actions": [],
            "rules": [],
            "events": [],
            "workflows": [],
            "mappings": [],
            "relation_mappings": [],
            "changes": [
                {"change_id": "entity.edited", "operation": "add"},
                {"change_id": "entity.safe", "operation": "add"},
                {"change_id": "relation.depends_on_edited", "operation": "add"},
            ],
            "unresolved": [],
            "coverage": [{
                "source_ref": "source:p0001",
                "status": "modeled",
                "reason": "Model definitions",
                "change_keys": [
                    "entity.edited",
                    "entity.safe",
                    "relation.depends_on_edited",
                ],
            }],
        }

        selected, metadata = (
            scenario_model_draft_service.exclude_unvalidated_drafts_from_apply_payload(
                payload,
                [edited],
            )
        )

        assert [item["key"] for item in selected["entities"]] == ["entity.safe"]
        assert selected["relations"] == []
        assert [item["change_id"] for item in selected["changes"]] == ["entity.safe"]
        assert metadata["draft_preserved"] is True
        assert metadata["safe_change_count"] == 1
        assert metadata["excluded_resource_keys"] == [
            "entity.edited",
            "relation.depends_on_edited",
        ]


def test_edited_property_excludes_its_parent_entity_bundle() -> None:
    property_draft = _draft(
        tenant_id="tenant-property-draft",
        scenario_id="scenario-property-draft",
        kind="property",
        key="entity.project:property:name",
        payload={"entity_ref": "entity.project", "name": "name"},
    )
    selected, metadata = (
        scenario_model_draft_service.exclude_unvalidated_drafts_from_apply_payload(
            {
                "entities": [{"key": "entity.project", "name": "Project"}],
                "relations": [],
                "functions": [],
                "actions": [],
                "rules": [],
                "events": [],
                "workflows": [],
                "mappings": [],
                "relation_mappings": [],
                "changes": [{"change_id": "entity.project", "operation": "add"}],
                "unresolved": [],
                "coverage": [],
            },
            [property_draft],
        )
    )

    assert selected["entities"] == []
    assert selected["changes"] == []
    assert metadata["safe_change_count"] == 0
    assert metadata["excluded_resource_keys"] == ["entity.project"]


def test_draft_resource_startup_migration_is_idempotent() -> None:
    migration_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(migration_engine)
    original_engine = database.engine
    database.engine = migration_engine
    try:
        database._migrate_scenario_model_draft_resources()
        database._migrate_scenario_model_draft_resources()
        inspector = inspect(migration_engine)
        columns = {
            column["name"]
            for column in inspector.get_columns("scenario_model_draft_resources")
        }
        assert {"payload", "validation_issues", "enabled", "publishable"} <= columns
        unique_names = {
            item.get("name")
            for item in inspector.get_unique_constraints(
                "scenario_model_draft_resources"
            )
        }
        index_names = {
            item.get("name")
            for item in inspector.get_indexes("scenario_model_draft_resources")
        }
        assert "uq_scenario_model_draft_resource_identity" in (
            unique_names | index_names
        )
    finally:
        database.engine = original_engine
        migration_engine.dispose()


def test_property_resolve_accepts_parent_entity_and_stores_child_identity() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        tenant = Tenant(id="tenant-property-resolve", name="Property resolve")
        user = User(
            id="user-property-resolve",
            tenant_id=tenant.id,
            email="property-resolve@example.test",
            password_hash="x",
        )
        scenario = BusinessScenario(
            id="scenario-property-resolve",
            tenant_id=tenant.id,
            name="Property resolve",
        )
        entity = OntologyEntity(
            id="entity-property-resolve",
            scenario_id=scenario.id,
            name="Project",
        )
        prop = OntologyProperty(
            id="property-resolved-id",
            entity_id=entity.id,
            name="Project name",
            api_name="project_name",
        )
        draft = _draft(
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            kind="property",
            key="entity.project:property:project_name",
            payload={"name": "Project name", "api_name": "project_name"},
        )
        draft.created_by_user_id = user.id
        db.add_all([tenant, user, scenario, entity, prop, draft])
        db.commit()

        resolved_id = scenarios._resolved_formal_resource_id(
            db,
            scenario_id=scenario.id,
            draft=draft,
            resource_id=entity.id,
        )

        assert resolved_id == prop.id
        draft = scenario_model_draft_service.resolve_draft_atomic(
            db,
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            draft_id=draft.id,
            created_by_user_id=user.id,
            expected_revision=1,
            resolved_resource_id=resolved_id,
        )
        assert draft.draft_status == "resolved"
        assert draft.resolved_resource_id == prop.id


def test_entity_conceptual_mapping_resolve_uses_immutable_kind_and_identity() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        tenant = Tenant(id="tenant-concept-entity", name="Concept entity")
        scenario = BusinessScenario(
            id="scene-concept-entity",
            tenant_id=tenant.id,
            name="Concept entity",
        )
        other_scenario = BusinessScenario(
            id="scene-concept-other",
            tenant_id=tenant.id,
            name="Concept other",
        )
        source = DataSource(
            id="source-concept-entity",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            name="Entity source",
            type="sqlite",
            config={},
        )
        other_source = DataSource(
            id="source-concept-other",
            tenant_id=tenant.id,
            scenario_id=other_scenario.id,
            name="Other source",
            type="sqlite",
            config={},
        )
        entity = OntologyEntity(
            id="entity-concept-project",
            scenario_id=scenario.id,
            name="Project",
            api_name="entity_project",
        )
        wrong_entity = OntologyEntity(
            id="entity-concept-other",
            scenario_id=scenario.id,
            name="Other",
            api_name="entity_other",
        )
        cross_scene_entity = OntologyEntity(
            id="entity-concept-cross",
            scenario_id=other_scenario.id,
            name="Project",
            api_name="entity_project",
        )
        mapping = DataMapping(
            id="mapping-concept-project",
            scenario_id=scenario.id,
            entity_id=entity.id,
            data_source_id=source.id,
            table_name="projects",
        )
        wrong_mapping = DataMapping(
            id="mapping-concept-other",
            scenario_id=scenario.id,
            entity_id=wrong_entity.id,
            data_source_id=source.id,
            table_name="other",
        )
        cross_scene_mapping = DataMapping(
            id="mapping-concept-cross",
            scenario_id=other_scenario.id,
            entity_id=cross_scene_entity.id,
            data_source_id=other_source.id,
            table_name="projects",
        )
        draft = _draft(
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            kind="conceptual_mapping",
            key="conceptual_mapping.project",
            payload={
                "mapping_kind": "object",
                "entity_ref": "entity.project",
            },
        )
        # Working edits cannot turn an immutable object-mapping draft into a
        # relation-mapping draft, but corrected identity fields remain usable.
        draft.payload = {
            "mapping_kind": "relation",
            "entity_ref": "entity.project",
        }
        db.add_all([
            tenant,
            scenario,
            other_scenario,
            source,
            other_source,
            entity,
            wrong_entity,
            cross_scene_entity,
            mapping,
            wrong_mapping,
            cross_scene_mapping,
            draft,
        ])
        db.commit()

        assert scenarios._resolved_formal_resource_id(
            db,
            scenario_id=scenario.id,
            draft=draft,
            resource_id=mapping.id,
        ) == mapping.id
        assert scenarios._resolved_formal_resource_id(
            db,
            scenario_id=scenario.id,
            draft=draft,
            resource_id=wrong_mapping.id,
        ) == ""
        assert scenarios._resolved_formal_resource_id(
            db,
            scenario_id=scenario.id,
            draft=draft,
            resource_id=cross_scene_mapping.id,
        ) == ""


def test_relation_conceptual_mapping_resolve_validates_relation_identity() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        tenant = Tenant(id="tenant-concept-relation", name="Concept relation")
        scenario = BusinessScenario(
            id="scene-concept-relation",
            tenant_id=tenant.id,
            name="Concept relation",
        )
        source = DataSource(
            id="source-concept-relation",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            name="Relation source",
            type="sqlite",
            config={},
        )
        project = OntologyEntity(
            id="entity-relation-project",
            scenario_id=scenario.id,
            name="Project",
            api_name="entity_project",
        )
        owner = OntologyEntity(
            id="entity-relation-owner",
            scenario_id=scenario.id,
            name="Owner",
            api_name="entity_owner",
        )
        relation = OntologyRelation(
            id="relation-project-owner",
            scenario_id=scenario.id,
            name="Project owner",
            api_name="relation_project_owner",
            source_entity_id=project.id,
            target_entity_id=owner.id,
        )
        wrong_relation = OntologyRelation(
            id="relation-project-reviewer",
            scenario_id=scenario.id,
            name="Project reviewer",
            api_name="relation_project_reviewer",
            source_entity_id=project.id,
            target_entity_id=owner.id,
        )
        source_mapping = DataMapping(
            id="mapping-relation-project",
            scenario_id=scenario.id,
            entity_id=project.id,
            data_source_id=source.id,
            table_name="projects",
        )
        target_mapping = DataMapping(
            id="mapping-relation-owner",
            scenario_id=scenario.id,
            entity_id=owner.id,
            data_source_id=source.id,
            table_name="owners",
        )
        relation_mapping = RelationDataMapping(
            id="relmap-project-owner",
            scenario_id=scenario.id,
            relation_id=relation.id,
            source_mapping_id=source_mapping.id,
            target_mapping_id=target_mapping.id,
            mode="source_fk",
            data_source_id=source.id,
            table_name="projects",
            foreign_key_column="owner_id",
        )
        wrong_relation_mapping = RelationDataMapping(
            id="relmap-project-reviewer",
            scenario_id=scenario.id,
            relation_id=wrong_relation.id,
            source_mapping_id=source_mapping.id,
            target_mapping_id=target_mapping.id,
            mode="source_fk",
            data_source_id=source.id,
            table_name="projects",
            foreign_key_column="reviewer_id",
        )
        draft = _draft(
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            kind="conceptual_mapping",
            key="conceptual_mapping.project_owner",
            payload={
                "mapping_kind": "relation",
                "relation_ref": "relation.project_owner",
                "mode": "source_fk",
            },
        )
        draft.payload = {
            "mapping_kind": "object",
            "relation_ref": "relation.project_owner",
            "mode": "source_fk",
        }
        db.add_all([
            tenant,
            scenario,
            source,
            project,
            owner,
            relation,
            wrong_relation,
            source_mapping,
            target_mapping,
            relation_mapping,
            wrong_relation_mapping,
            draft,
        ])
        db.commit()

        assert scenarios._resolved_formal_resource_id(
            db,
            scenario_id=scenario.id,
            draft=draft,
            resource_id=relation_mapping.id,
        ) == relation_mapping.id
        assert scenarios._resolved_formal_resource_id(
            db,
            scenario_id=scenario.id,
            draft=draft,
            resource_id=wrong_relation_mapping.id,
        ) == ""
        assert scenarios._resolved_formal_resource_id(
            db,
            scenario_id=scenario.id,
            draft=draft,
            resource_id=source_mapping.id,
        ) == ""

        draft.payload = {**draft.payload, "mode": "target_fk"}
        assert scenarios._resolved_formal_resource_id(
            db,
            scenario_id=scenario.id,
            draft=draft,
            resource_id=relation_mapping.id,
        ) == ""
