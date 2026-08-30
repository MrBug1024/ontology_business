from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import (
    BusinessScenario,
    DataMapping,
    DataSource,
    DatasetSchema,
    DatasetVersion,
    FunctionDefinition,
    LogicalDataset,
    OntologyEntity,
    OntologyEvent,
    ScenarioCapabilityPort,
    ScenarioDatasetBinding,
    ScenarioModelDraftResource,
    Tenant,
    User,
)
from app.routers import assistant
from app.services import (
    assistant_capability_modeling_service,
    candidate_governance_service,
    permission_service,
    release_service,
    scenario_model_compiler,
    scenario_model_draft_service,
)


def _object_schema(properties: dict | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": False,
    }


def _raw_model(ref: str) -> dict:
    return {
        "schema_version": scenario_model_compiler.SCHEMA_VERSION,
        "entities": [],
        "relations": [],
        "instances": [],
        "functions": [],
        "actions": [],
        "rules": [],
        "events": [],
        "workflows": [],
        "mappings": [],
        "relation_mappings": [],
        "conceptual_mappings": [],
        "unresolved": [],
        "coverage": [{
            "source_ref": ref,
            "status": "modeled",
            "reason": "The source is represented by the generated definition.",
            "change_keys": [],
        }],
    }


def _draft(
    *,
    tenant_id: str,
    scenario_id: str,
    user_id: str,
    kind: str,
    key: str,
    payload: dict,
) -> ScenarioModelDraftResource:
    return ScenarioModelDraftResource(
        tenant_id=tenant_id,
        scenario_id=scenario_id,
        created_by_user_id=user_id,
        proposal_id=f"proposal-{kind}-{key}"[:64],
        task_id="capabilities",
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
        materialization_source="compiler_sidecar",
        draft_status="ready_for_review",
        enabled=False,
        publishable=False,
        revision=0,
    )


@pytest.fixture()
def governed_scenario():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Session(engine, expire_on_commit=False)
    tenant = Tenant(id="tenant-capability-modeling", name="Capability modeling")
    user = User(
        id="user-capability-modeling",
        tenant_id=tenant.id,
        email="capability-modeling@example.test",
        password_hash="test-only",
        status="active",
    )
    scenario = BusinessScenario(
        id="scenario-capability-modeling",
        tenant_id=tenant.id,
        name="Generic knowledge work",
        namespace="generic_knowledge_work",
        status="draft",
    )
    db.add_all([tenant, user, scenario])
    db.commit()
    permission_service.ensure_organization(db, tenant.id, owner_user_id=user.id)
    db.commit()
    db.info["tenant_id"] = tenant.id
    db.info["user_id"] = user.id
    try:
        yield db, tenant, user, scenario
    finally:
        db.close()
        engine.dispose()


def test_historical_catalog_is_schema_evidence_not_runtime_binding(
    governed_scenario,
) -> None:
    db, _tenant, _user, scenario = governed_scenario
    bundle = scenario_model_compiler.build_source_bundle(
        "请编译附件",
        [{
            "id": "historical-sample",
            "filename": "historical-sample.xlsx",
            "text": "Records have a stable code and a descriptive title.",
        }],
    )
    ref = bundle["paragraphs"][0]["ref"]
    raw = _raw_model(ref)
    raw["entities"] = [{
        "key": "entity.record",
        "name": "Record",
        "properties": [
            {
                "name": "Code",
                "data_type": "string",
                "is_key": True,
                "is_title": True,
                "is_required": True,
            },
            {"name": "Title", "data_type": "string"},
        ],
        "evidence_refs": [ref],
        "confidence": 0.96,
    }]
    raw["coverage"][0]["change_keys"] = ["entity.record"]
    catalog = [{
        "data_source_id": "physical-source-must-not-survive",
        "data_source_name": "Historical storage",
        "type": "database",
        "tables": [{
            "name": "historical_records",
            "columns": [
                {"name": "code", "type": "varchar", "pk": True},
                {"name": "title", "type": "varchar", "pk": False},
            ],
        }],
    }]

    payload = scenario_model_compiler.normalize_scenario_model(
        db,
        scenario,
        raw,
        source_bundle=bundle,
        mapping_catalog=catalog,
        columns_by_table={},
    )

    sidecar = payload["capability_modeling"]
    assert sidecar["ports"] == []
    assert sidecar["zero_port_capability"] is True
    assert sidecar["data_roles"]
    assert {item["role"] for item in sidecar["data_roles"]} == {
        "modeling_evidence"
    }
    assert all(item["runtime_binding"] is False for item in sidecar["data_roles"])
    catalog_evidence = next(
        item
        for item in sidecar["data_roles"]
        if item["source_kind"] == "catalog_schema"
    )
    assert catalog_evidence["schema_evidence"][0]["relations"] == [{
        "name": "historical_records",
        "fields": [
            {"name": "code", "type": "varchar", "is_key": True},
            {"name": "title", "type": "varchar", "is_key": False},
        ],
    }]
    encoded = json.dumps(sidecar, ensure_ascii=False, sort_keys=True)
    assert "physical-source-must-not-survive" not in encoded
    assert "data_source_id" not in encoded
    scenario_model_compiler.preflight_scenario_model(
        db, scenario, payload, inspect_mappings=True
    )


def test_historical_sample_builds_model_and_explicit_fixture_without_contract_lock_in(
    governed_scenario,
    monkeypatch,
) -> None:
    db, tenant, user, scenario = governed_scenario
    source = DataSource(
        id="historical-modeling-source",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        name="Historical modeling source",
        type="postgres",
        config={},
    )
    dataset = LogicalDataset(
        id="historical-modeling-dataset",
        tenant_id=tenant.id,
        key="historical.modeling.records",
        name="Historical modeling records",
    )
    schema = DatasetSchema(
        id="historical-modeling-schema",
        tenant_id=tenant.id,
        dataset_id=dataset.id,
        schema_version=1,
        schema_hash="7" * 64,
        compatibility="none",
        schema_document={
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["code"],
            },
        },
    )
    version = DatasetVersion(
        id="historical-modeling-version",
        tenant_id=tenant.id,
        dataset_id=dataset.id,
        schema_id=schema.id,
        version_number=1,
        status="ready",
        record_count=2,
        content_hash="8" * 64,
    )
    db.add_all([source, dataset, schema, version])
    db.commit()

    bundle = scenario_model_compiler.build_source_bundle(
        "Model the historical sample and an invocation contract.",
        [{
            "id": "historical-modeling-workbook",
            "filename": "historical-modeling.xlsx",
            "text": "Each historical record has a stable code and a descriptive title.",
        }],
    )
    ref = bundle["paragraphs"][0]["ref"]
    all_modeling_refs = [paragraph["ref"] for paragraph in bundle["paragraphs"]]
    raw = _raw_model(ref)
    raw["entities"] = [{
        "key": "entity.historical_record",
        "name": "Historical record",
        "properties": [
            {
                "name": "Code",
                "data_type": "string",
                "is_key": True,
                "is_title": True,
                "is_required": True,
            },
            {"name": "Title", "data_type": "string"},
        ],
        "evidence_refs": [ref],
        "confidence": 0.97,
    }]
    raw["functions"] = [{
        "key": "function.analyze_current_records",
        "name": "Analyze current records",
        "input_schema": _object_schema({"records": {"type": "array"}}),
        "output_schema": _object_schema({"summary": {"type": "object"}}),
        "managed_data_ports": [{
            "port_key": "records.current",
            "name": "Current governed records",
            "direction": "input",
            "role": "invocation_input",
            "media_kind": "dataset",
            "schema_document": {
                "type": "array",
                "items": {"type": "object"},
            },
            "binding_policy": "per_invocation",
            "binding_kinds": ["dataset_version", "dataset_head"],
            "evidence_kind": "versioned_data",
            "evidence_refs": all_modeling_refs,
            "confidence": 0.94,
        }],
        "evidence_refs": all_modeling_refs,
        "confidence": 0.94,
    }]
    raw["mappings"] = [{
        "key": "mapping.historical_record",
        "entity_ref": "entity.historical_record",
        "data_source_ref": source.id,
        "table_name": "historical_records",
        "column_map": {"Code": "code", "Title": "title"},
        "evidence_refs": [ref],
        "confidence": 0.96,
    }]
    raw["coverage"][0]["change_keys"] = [
        "entity.historical_record",
        "function.analyze_current_records",
        "mapping.historical_record",
    ]
    for paragraph in bundle["paragraphs"]:
        if paragraph["ref"] != ref:
            raw["coverage"].append({
                "source_ref": paragraph["ref"],
                "status": "modeled",
                "reason": "The request is represented by the invocation contract.",
                "change_keys": ["function.analyze_current_records"],
            })
    catalog = [{
        "data_source_id": source.id,
        "data_source_name": source.name,
        "type": source.type,
        "tables": [{
            "name": "historical_records",
            "columns": [
                {"name": "code", "type": "varchar", "pk": True},
                {"name": "title", "type": "varchar", "pk": False},
            ],
        }],
    }]
    monkeypatch.setattr(
        scenario_model_compiler.datasource_service,
        "list_tables",
        lambda _source: catalog[0]["tables"],
    )
    payload = scenario_model_compiler.normalize_scenario_model(
        db,
        scenario,
        raw,
        source_bundle=bundle,
        mapping_catalog=catalog,
        columns_by_table={(source.id, "historical_records"): {"code", "title"}},
    )
    assert not [
        item for item in payload["unresolved"] if item.get("blocking", True)
    ], payload["unresolved"]
    scenario_model_compiler.apply_scenario_model(db, scenario, payload)
    summary = scenario_model_draft_service.materialize_draft_resources(
        db,
        scenario,
        {
            "kind": "scenario_model",
            "proposal_id": "proposal-historical-modeling",
            "payload": payload,
        },
        created_by_user_id=user.id,
    )
    function = db.scalars(select(FunctionDefinition).where(
        FunctionDefinition.scenario_id == scenario.id,
    )).one()
    function_draft = db.scalars(select(ScenarioModelDraftResource).where(
        ScenarioModelDraftResource.scenario_id == scenario.id,
        ScenarioModelDraftResource.resource_kind == "function",
        ScenarioModelDraftResource.resource_key == "function.analyze_current_records",
    )).one()
    function_draft.draft_status = "resolved"
    function_draft.resolved_resource_id = function.id
    db.flush()
    port_rows = list(db.scalars(select(ScenarioModelDraftResource).where(
        ScenarioModelDraftResource.scenario_id == scenario.id,
        ScenarioModelDraftResource.resource_kind == "capability_port",
    )).all())
    candidate_governance_service.promote_candidates(
        db,
        scenario,
        tenant_id=tenant.id,
        created_by_user_id=user.id,
        expected_revisions={row.id: row.revision for row in port_rows},
    )
    fixture = ScenarioDatasetBinding(
        id="historical-modeling-fixture",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        dataset_id=dataset.id,
        binding_key="historical.records.fixture",
        role="test_fixture",
        environment="dev",
        binding_mode="pinned",
        dataset_version_id=version.id,
        is_required=False,
        status="active",
        config={"classification_source": "expert_review"},
    )
    db.add(fixture)
    for port in db.scalars(select(ScenarioCapabilityPort).where(
        ScenarioCapabilityPort.scenario_id == scenario.id,
    )).all():
        port.status = "active"
    db.commit()

    assert summary["resource_count"] >= 5
    assert db.scalars(select(OntologyEntity)).one().name == "Historical record"
    assert db.scalars(select(DataMapping)).one().table_name == "historical_records"
    assert len(db.scalars(select(ScenarioCapabilityPort)).all()) == 1
    assert db.scalars(select(ScenarioDatasetBinding)).one().role == "test_fixture"
    contract = release_service.capture_snapshot_content(db, scenario)["capability_ports"]
    encoded_contract = json.dumps(contract, ensure_ascii=False, sort_keys=True)
    assert version.id not in encoded_contract
    assert source.id not in encoded_contract
    assert "data_source_id" not in encoded_contract


def test_text_only_function_remains_typed_input_without_managed_ports(
    governed_scenario,
) -> None:
    db, _tenant, _user, scenario = governed_scenario
    bundle = scenario_model_compiler.build_source_bundle(
        "Each invocation accepts a fragment of text and returns a structured result.",
        [],
    )
    ref = bundle["paragraphs"][0]["ref"]
    raw = _raw_model(ref)
    raw["functions"] = [{
        "key": "function.analyze_fragment",
        "name": "Analyze fragment",
        "description": "Analyze the supplied fragment without retaining it.",
        "input_schema": _object_schema({
            "fragment": {"type": "string"},
        }),
        "output_schema": _object_schema({
            "result": {"type": "object"},
        }),
        "evidence_refs": [ref],
        "confidence": 0.91,
    }]
    raw["coverage"][0]["change_keys"] = ["function.analyze_fragment"]

    payload = scenario_model_compiler.normalize_scenario_model(
        db,
        scenario,
        raw,
        source_bundle=bundle,
        mapping_catalog=[],
        columns_by_table={},
    )

    sidecar = payload["capability_modeling"]
    assert sidecar["zero_port_capability"] is True
    assert sidecar["ports"] == []
    port_candidates = [
        item
        for item in payload["draft_candidates"]
        if item["resource_kind"] == "capability_port"
    ]
    assert port_candidates == []
    assert not [item for item in payload["unresolved"] if item.get("blocking")]
    scenario_model_compiler.preflight_scenario_model(
        db, scenario, payload, inspect_mappings=True
    )


def test_function_action_and_workflow_share_explicit_managed_port_rules() -> None:
    ref = "source:managed-port-contract"
    resources = [
        (
            "functions",
            "function",
            "function.inspect_records",
            {
                "port_key": "records.current",
                "name": "Current records",
                "direction": "input",
                "role": "invocation_input",
                "media_kind": "dataset",
                "schema_document": {"type": "array", "items": {"type": "object"}},
                "binding_kinds": ["dataset_version", "dataset_head"],
                "evidence_kind": "versioned_data",
                "evidence_refs": [ref],
            },
        ),
        (
            "actions",
            "action",
            "action.review_document",
            {
                "port_key": "review.document",
                "name": "Review document",
                "direction": "input",
                "role": "invocation_input",
                "media_kind": "document",
                "schema_document": {"type": "object"},
                "binding_kinds": ["asset_version"],
                "evidence_kind": "document_attachment",
                "evidence_refs": [ref],
            },
        ),
        (
            "workflows",
            "workflow",
            "workflow.refresh_reference",
            {
                "port_key": "reference.live",
                "name": "Live reference connector",
                "direction": "input",
                "role": "reference",
                "media_kind": "connector",
                "schema_document": {"type": "object"},
                "binding_kinds": ["connector_binding"],
                "evidence_kind": "reference",
                "evidence_refs": [ref],
            },
        ),
    ]
    sections: dict[str, list[dict]] = {
        key: [] for key in (
            "entities", "relations", "functions", "actions", "rules", "events", "workflows"
        )
    }
    for section, kind, key, declaration in resources:
        ports = assistant_capability_modeling_service.normalize_managed_data_port_declarations(
            [declaration],
            resource_kind=kind,
            resource_key=key,
            resource_evidence_refs=[ref],
            resource_confidence=0.9,
        )
        sections[section].append({
            "key": key,
            "name": key,
            "input_schema": _object_schema({"ordinary": {"type": "string"}}),
            "output_schema": _object_schema({"result": {"type": "string"}}),
            "evidence_refs": [ref],
            "confidence": 0.9,
            "managed_data_ports": ports,
        })

    sidecar = assistant_capability_modeling_service.build_capability_modeling_sidecar(
        normalized_sections=sections,
        source_bundle={"documents": [], "paragraphs": []},
    )

    assert len(sidecar["ports"]) == 3
    assert {item["port"]["media_kind"] for item in sidecar["ports"]} == {
        "dataset", "document", "connector"
    }
    assert {
        item["port"]["config"]["contract_source"]["resource_kind"]
        for item in sidecar["ports"]
    } == {"function", "action", "workflow"}
    assert all(item["port"]["media_kind"] != "structured" for item in sidecar["ports"])

    with pytest.raises(ValueError, match="不属于该能力的证据"):
        assistant_capability_modeling_service.normalize_managed_data_port_declarations(
            [{**resources[0][3], "evidence_refs": ["source:other"]}],
            resource_kind="function",
            resource_key="function.inspect_records",
            resource_evidence_refs=[ref],
            resource_confidence=0.9,
        )


def test_no_data_source_and_no_interaction_contract_allows_zero_ports(
    governed_scenario,
) -> None:
    db, _tenant, _user, scenario = governed_scenario
    bundle = scenario_model_compiler.build_source_bundle(
        "A note has a stable identifier and a title.",
        [],
    )
    ref = bundle["paragraphs"][0]["ref"]
    raw = _raw_model(ref)
    raw["entities"] = [{
        "key": "entity.note",
        "name": "Note",
        "properties": [{
            "name": "Identifier",
            "data_type": "string",
            "is_key": True,
            "is_title": True,
            "is_required": True,
        }],
        "evidence_refs": [ref],
        "confidence": 0.94,
    }]
    raw["coverage"][0]["change_keys"] = ["entity.note"]

    payload = scenario_model_compiler.normalize_scenario_model(
        db,
        scenario,
        raw,
        source_bundle=bundle,
        mapping_catalog=[],
        columns_by_table={},
    )

    assert payload["capability_modeling"]["zero_port_capability"] is True
    assert payload["capability_modeling"]["ports"] == []
    assert not [item for item in payload["unresolved"] if item.get("blocking")]
    scenario_model_compiler.preflight_scenario_model(
        db, scenario, payload, inspect_mappings=True
    )


def test_staged_capability_merge_keeps_ports_and_prior_modeling_evidence(
    governed_scenario,
) -> None:
    db, _tenant, _user, scenario = governed_scenario
    bundle = scenario_model_compiler.build_source_bundle(
        "A note has an identifier. Each invocation accepts text and returns a result.",
        [],
    )
    ref = bundle["paragraphs"][0]["ref"]
    ontology_raw = _raw_model(ref)
    ontology_raw["entities"] = [{
        "key": "entity.note",
        "name": "Note",
        "properties": [{
            "name": "Identifier",
            "data_type": "string",
            "is_key": True,
            "is_title": True,
            "is_required": True,
        }],
        "evidence_refs": [ref],
        "confidence": 0.9,
    }]
    ontology_raw["coverage"][0]["change_keys"] = ["entity.note"]
    ontology = scenario_model_compiler.normalize_scenario_model(
        db,
        scenario,
        ontology_raw,
        source_bundle=bundle,
        mapping_catalog=[],
        columns_by_table={},
    )

    capability_raw = _raw_model(ref)
    capability_raw["functions"] = [{
        "key": "function.process_text",
        "name": "Process text",
        "input_schema": _object_schema({"text": {"type": "string"}}),
        "output_schema": _object_schema({"result": {"type": "object"}}),
        "evidence_refs": [ref],
        "confidence": 0.92,
    }]
    capability_raw["coverage"][0]["change_keys"] = ["function.process_text"]
    capability = scenario_model_compiler.normalize_scenario_model(
        db,
        scenario,
        capability_raw,
        source_bundle=bundle,
        mapping_catalog=[],
        columns_by_table={},
    )

    merged = assistant._merge_staged_compilation_payload(
        ontology,
        capability,
        task_id="capabilities",
    )

    assert merged["capability_modeling"]["ports"] == []
    document_role = next(
        item
        for item in merged["capability_modeling"]["data_roles"]
        if item["source_kind"] == "user_request"
    )
    assert {
        item["resource_kind"] for item in document_role["schema_evidence"]
    } == {"entity", "function"}
    assert {
        item["resource_kind"] for item in document_role["semantic_evidence"]
    } >= {"entity", "function"}
    assert {
        item["resource_kind"]
        for item in merged["draft_candidates"]
    } >= {"entity", "function"}
    assert "capability_port" not in {
        item["resource_kind"]
        for item in merged["draft_candidates"]
    }


def test_generated_port_candidates_promote_only_to_unbound_draft_ports(
    governed_scenario,
) -> None:
    db, tenant, user, scenario = governed_scenario
    bundle = scenario_model_compiler.build_source_bundle(
        "Each request supplies structured content and receives a structured response.",
        [],
    )
    ref = bundle["paragraphs"][0]["ref"]
    raw = _raw_model(ref)
    raw["functions"] = [{
        "key": "function.transform_content",
        "name": "Transform content",
        "input_schema": _object_schema({"content": {"type": "string"}}),
        "output_schema": _object_schema({"transformed": {"type": "string"}}),
        "managed_data_ports": [{
            "port_key": "content.reference",
            "name": "Versioned reference content",
            "direction": "input",
            "role": "reference",
            "media_kind": "document",
            "schema_document": {"type": "object"},
            "binding_policy": "scenario_default",
            "binding_kinds": ["asset_version"],
            "evidence_kind": "reference",
            "evidence_refs": [ref],
            "confidence": 0.89,
        }],
        "evidence_refs": [ref],
        "confidence": 0.89,
    }]
    raw["coverage"][0]["change_keys"] = ["function.transform_content"]
    payload = scenario_model_compiler.normalize_scenario_model(
        db,
        scenario,
        raw,
        source_bundle=bundle,
        mapping_catalog=[],
        columns_by_table={},
    )
    summary = scenario_model_draft_service.materialize_draft_resources(
        db,
        scenario,
        {
            "kind": "scenario_model",
            "proposal_id": "proposal-capability-modeling",
            "payload": payload,
        },
        created_by_user_id=user.id,
    )
    db.commit()
    candidate_rows = list(db.scalars(select(ScenarioModelDraftResource).where(
        ScenarioModelDraftResource.scenario_id == scenario.id,
        ScenarioModelDraftResource.resource_kind.in_({"function", "capability_port"}),
    )).all())
    port_rows = [row for row in candidate_rows if row.resource_kind == "capability_port"]
    function_rows = [row for row in candidate_rows if row.resource_kind == "function"]
    assert summary["resource_count"] >= 2
    assert len(port_rows) == 1
    assert len(function_rows) == 1
    evaluation = candidate_governance_service.evaluate_candidates(
        db, scenario, candidate_rows
    )
    assert evaluation.eligible, json.dumps(
        evaluation.blockers, ensure_ascii=False, sort_keys=True
    )

    promoted_rows, result = candidate_governance_service.promote_candidates(
        db,
        scenario,
        tenant_id=tenant.id,
        created_by_user_id=user.id,
        expected_revisions={row.id: 0 for row in candidate_rows},
    )
    db.commit()

    formal_ports = list(db.scalars(select(ScenarioCapabilityPort).where(
        ScenarioCapabilityPort.scenario_id == scenario.id,
    )).all())
    assert len(formal_ports) == 1
    assert {port.status for port in formal_ports} == {"draft"}
    assert all(port.dataset_id is None for port in formal_ports)
    assert all(port.dataset_schema_id is None for port in formal_ports)
    assert {row.draft_status for row in promoted_rows} == {"resolved"}
    assert {item["activation_status"] for item in result["promoted"]} == {
        "inactive", "not_applicable",
    }
    assert result["counts"] == {
        "functions_added": 1,
        "capability_ports_added": 1,
    }
    assert len(db.scalars(select(FunctionDefinition)).all()) == 1
    snapshot = release_service.capture_snapshot_content(db, scenario)
    assert snapshot["capability_ports"] == []


def test_physical_binding_blocker_prevents_mixed_batch_partial_writes(
    governed_scenario,
) -> None:
    db, tenant, user, scenario = governed_scenario
    event = _draft(
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        user_id=user.id,
        kind="event",
        key="event.completed",
        payload={
            "name": "Completed",
            "payload_schema": _object_schema(),
            "enabled": False,
        },
    )
    unsafe_port = _draft(
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        user_id=user.id,
        kind="capability_port",
        key="unsafe.input",
        payload={
            "port_key": "unsafe.input",
            "name": "Unsafe input",
            "direction": "input",
            "role": "invocation_input",
            "media_kind": "dataset",
            "schema_document": _object_schema(),
            "binding_policy": "per_invocation",
            "status": "draft",
            "config": {"data_source_id": None},
            "dataset_version_id": "historical-version-must-not-bind",
        },
    )
    db.add_all([event, unsafe_port])
    db.commit()

    with pytest.raises(
        candidate_governance_service.CandidatePromotionBlocked
    ) as exc_info:
        candidate_governance_service.promote_candidates(
            db,
            scenario,
            tenant_id=tenant.id,
            created_by_user_id=user.id,
            expected_revisions={event.id: 0, unsafe_port.id: 0},
        )

    assert exc_info.value.blockers[0]["code"] == "capability_port_contract_invalid"
    assert "config.data_source_id" in exc_info.value.blockers[0]["message"]
    assert "dataset_version_id" in exc_info.value.blockers[0]["message"]
    assert db.scalars(select(OntologyEvent)).all() == []
    assert db.scalars(select(ScenarioCapabilityPort)).all() == []
    assert db.get(ScenarioModelDraftResource, event.id).draft_status == "ready_for_review"
    assert db.get(ScenarioModelDraftResource, unsafe_port.id).draft_status == "ready_for_review"
