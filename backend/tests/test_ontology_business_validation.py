"""2026-09-09 business semantics regressions, derived from diagnostic experiments.

PostgreSQL cases opt in with RUN_ONTOLOGY_BUSINESS_POSTGRESQL_TESTS=1. All
database writes use a newly created database, migrated to the actual head and
removed afterward. Historical observations remain in the dated validation report.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
import pytest
import pyarrow as pa
import pyarrow.parquet as parquet
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import (
    BusinessScenario, OntologyEntity, OntologyInstance, OntologyProperty,
    OntologyRelation, RelationInstance, OntologyRule, OntologyWorkflow,
    Tenant, User,
)
from app.routers import scenarios
from app.schemas import EntityIn, InstanceIn, PropertyIn, RelationInstanceIn
from app.services import (
    business_query_service, dataset_query_service, permission_service, runtime_definition_service,
    scenario_model_compiler, scenario_model_evaluator, workflow_service,
)
from app.services.capability_contracts import Actor, CapabilityRef, Request
from app.services.capability_invoker import (
    CapabilityInvocationError, CapabilityInvoker, resolve_capability_contract,
)
from app.services.deployment_service import build_resolved_deployment
from tests.access_postgresql import isolated_access_database
from tests.release_fixtures import enable_current_release


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def isolated_engine():
    if os.environ.get("RUN_ONTOLOGY_BUSINESS_POSTGRESQL_TESTS") != "1":
        pytest.skip("Explicit opt-in required for a self-created PostgreSQL database")
    with isolated_access_database() as (runtime_url, _admin):
        engine = create_engine(runtime_url, hide_parameters=True)
        try:
            yield engine
        finally:
            engine.dispose()


@pytest.fixture
def world(isolated_engine):
    with Session(isolated_engine, expire_on_commit=False) as db:
        tenant = Tenant(id=uuid4().hex, name="Ontology verification")
        db.add(tenant)
        db.flush()
        user = User(id=uuid4().hex, tenant_id=tenant.id,
                    email=f"{uuid4().hex}@example.test", password_hash="synthetic",
                    status="active")
        scenario = BusinessScenario(id=uuid4().hex, tenant_id=tenant.id,
                                    name="Synthetic case review", namespace="verification")
        db.add_all([user, scenario])
        db.flush()
        entity = OntologyEntity(id=uuid4().hex, scenario_id=scenario.id,
                                name="Case", api_name="case", state_property="status",
                                description="One independently identified request for review.")
        db.add(entity)
        db.flush()
        properties = [
            OntologyProperty(entity_id=entity.id, name="case_id", api_name="case_id",
                             data_type="string", is_key=True, is_title=True, is_required=True),
            OntologyProperty(entity_id=entity.id, name="amount", api_name="amount",
                             data_type="number", is_required=True, constraints={"minimum": 0}),
            OntologyProperty(entity_id=entity.id, name="status", api_name="status",
                             data_type="string", is_enum=True,
                             enum_values=["draft", "approved", "closed"]),
        ]
        rule = OntologyRule(id=uuid4().hex, scenario_id=scenario.id, entity_id=entity.id,
                            name="Review threshold", enabled=True, severity="warning",
                            condition={"field": "amount", "op": ">", "value": 10},
                            action_on_match="Request review")
        db.add_all([*properties, rule])
        db.commit()
        permission_service.ensure_organization(db, tenant.id, owner_user_id=user.id)
        db.commit()
        db.info.update(tenant_id=tenant.id, user_id=user.id)
        yield SimpleNamespace(db=db, scenario=scenario, entity=entity, rule=rule,
                              amount=properties[1], status=properties[2],
                              actor=Actor(actor_type="user", principal_id=user.id,
                                          tenant_id=tenant.id))


def _definition(world):
    world.db.expire_all()
    return runtime_definition_service.resolve_authoring(world.db, world.scenario)


def _invoke_rule(world, record, definition=None):
    deployment = build_resolved_deployment(definition or _definition(world))
    request = Request(capability=CapabilityRef(kind="rule", resource_id=world.rule.id),
                      inputs={"record": record}, correlation_id=uuid4().hex,
                      expected_definition_hash=deployment.definition_hash,
                      expected_deployment_fingerprint=deployment.fingerprint)
    return CapabilityInvoker().invoke(world.db, deployment, world.actor, request,
                                      invocation_source="internal")


def _create_case(world, key="case-a", amount=15, status="draft"):
    return scenarios.create_instance(world.scenario.id, InstanceIn(
        entity_id=world.entity.id, name=key,
        attributes={"case_id": key, "amount": amount, "status": status}), db=world.db)


def test_rule_mutation_changes_business_decision_and_old_release_stays_fixed(world):
    release = enable_current_release(world.db, world.scenario)
    world.db.commit()
    published = runtime_definition_service.resolve_active(
        world.db, world.scenario, release_id=release.id)
    before = _invoke_rule(world, {"amount": 15}, published)
    assert before.status == "succeeded", (before.error_code, before.error_message)
    assert before.output["matched"] is True
    assert before.output["action_on_match"] == "Request review"

    world.rule.condition = {"field": "amount", "op": ">", "value": 20}
    world.db.commit()
    changed = _invoke_rule(world, {"amount": 15})
    assert changed.status == "succeeded", (changed.error_code, changed.error_message)
    assert changed.output["matched"] is False
    assert changed.output["action_on_match"] == ""
    reread = runtime_definition_service.resolve_active(
        world.db, world.scenario, release_id=release.id)
    old_result = _invoke_rule(world, {"amount": 15}, reread)
    assert old_result.status == "succeeded"
    assert old_result.output["matched"] is True
    assert old_result.output["side_effects_executed"] is False


def test_property_type_mutation_changes_rule_input_admissibility(world):
    world.amount.data_type = "integer"
    world.db.commit()
    with pytest.raises(CapabilityInvocationError) as rejected:
        _invoke_rule(world, {"amount": 15.5})
    assert rejected.value.code == "input_schema_invalid"
    world.amount.data_type = "number"
    world.db.commit()
    changed = _invoke_rule(world, {"amount": 15.5})
    assert changed.status == "succeeded"
    assert changed.output["matched"] is True


def test_numeric_constraint_is_shared_by_instance_and_object_bound_rule(world):
    with pytest.raises(HTTPException) as rejected:
        _create_case(world, amount=-1)
    assert rejected.value.status_code == 400
    assert "amount" in rejected.value.detail
    definition = _definition(world)
    contract = resolve_capability_contract(world.db, build_resolved_deployment(definition),
                                          CapabilityRef(kind="rule", resource_id=world.rule.id))
    schema = contract["input_schema"]["properties"]["record"]["properties"]["amount"]
    assert schema == {"type": "number", "minimum": 0}
    with pytest.raises(CapabilityInvocationError) as rejected:
        _invoke_rule(world, {"amount": -1}, definition)
    assert rejected.value.code == "input_schema_invalid"
    world.rule.input_validation = "record"
    world.db.commit()
    assert _invoke_rule(world, {"amount": -1}).status == "succeeded"


def test_business_key_prevents_duplicate_manual_objects(world):
    first = _create_case(world, key="same-business-key")
    with pytest.raises(HTTPException) as rejected:
        _create_case(world, key="same-business-key")
    assert rejected.value.status_code == 409
    count = world.db.scalar(select(func.count()).select_from(OntologyInstance).where(
        OntologyInstance.entity_id == world.entity.id,
        OntologyInstance.attributes["case_id"].as_string() == "same-business-key"))
    assert count == 1
    assert world.db.get(OntologyInstance, first.id) is not None


def test_unlinked_object_remains_explicitly_incomplete_until_required_relation_exists(world):
    relation = OntologyRelation(scenario_id=world.scenario.id, name="requires reviewer",
                                api_name="requires_reviewer", source_entity_id=world.entity.id,
                                target_entity_id=world.entity.id, relation_type="N:1",
                                constraints={"source_min_cardinality": 1})
    world.db.add(relation)
    world.db.commit()
    created = _create_case(world)
    assert world.db.get(OntologyInstance, created.id) is not None
    assert world.db.scalar(select(func.count()).select_from(RelationInstance).where(
        RelationInstance.source_instance_id == created.id)) == 0
    assert created.integrity.status == "incomplete"
    reviewer = _create_case(world, key="reviewer")
    scenarios.create_relation_instance(world.scenario.id, RelationInstanceIn(
        relation_id=relation.id, source_instance_id=created.id, target_instance_id=reviewer.id), db=world.db)
    world.db.expire_all()
    from app.services.ontology_instance_contract_service import integrity
    assert integrity(world.db, world.db.get(OntologyInstance, created.id)).status == "valid"


def test_explicit_state_policy_rejects_invalid_initial_state_and_transition(world):
    world.entity.state_policy = {"enabled": True, "initial_states": ["draft"],
        "transitions": [{"from_state": "draft", "to_state": "approved"}]}
    world.db.commit()
    with pytest.raises(HTTPException) as rejected:
        _create_case(world, status="closed")
    assert rejected.value.status_code == 409
    created = _create_case(world)
    updated = scenarios.update_instance(created.id, InstanceIn(
        entity_id=world.entity.id, name="case-a",
        attributes={"case_id": "case-a", "amount": 15, "status": "approved"}), db=world.db)
    assert updated.state == "approved"
    with pytest.raises(HTTPException) as rejected:
        scenarios.update_instance(created.id, InstanceIn(entity_id=world.entity.id, name="case-a",
            attributes={"case_id": "case-a", "amount": 15, "status": "draft"}), db=world.db)
    assert rejected.value.status_code == 409


def test_relation_axiom_mutation_changes_accepted_business_links(world):
    relation = OntologyRelation(scenario_id=world.scenario.id, name="precedes",
        api_name="precedes", source_entity_id=world.entity.id,
        target_entity_id=world.entity.id, relation_type="N:M", constraints={"acyclic": True})
    world.db.add(relation)
    world.db.commit()
    first = _create_case(world, key="first")
    second = _create_case(world, key="second")
    scenarios.create_relation_instance(world.scenario.id, RelationInstanceIn(
        relation_id=relation.id, source_instance_id=first.id,
        target_instance_id=second.id), db=world.db)
    reverse = RelationInstanceIn(relation_id=relation.id, source_instance_id=second.id,
                                 target_instance_id=first.id)
    with pytest.raises(HTTPException) as rejected:
        scenarios.create_relation_instance(world.scenario.id, reverse, db=world.db)
    assert rejected.value.status_code == 409
    relation.constraints = {}
    world.db.commit()
    accepted = scenarios.create_relation_instance(world.scenario.id, reverse, db=world.db)
    assert accepted.source_instance_id == second.id


def _query_context(world):
    definition = _definition(world)
    source = SimpleNamespace(id="synthetic-source", type="dataset", config={},
        tenant_id=world.scenario.tenant_id, scenario_id=world.scenario.id)
    mapping = SimpleNamespace(id="synthetic-mapping", entity_id=world.entity.id,
        data_source_id=source.id, table_name="case_rows",
        column_map={"case_id": "record_key", "amount": "quantity", "status": "state"},
        transform_rules={})
    return dict(definition=replace(definition, mappings={mapping.id: mapping}),
                mappings=[mapping], data_sources=[source])


def test_semantic_query_executes_parquet_and_property_type_controls_filter(world, tmp_path, monkeypatch):
    path = tmp_path / "synthetic.parquet"
    parquet.write_table(pa.table({"record_key": ["case-a", "case-b"],
                                 "quantity": [5, 25], "state": ["draft", "draft"],
                                 "parent_key": [None, "case-a"]}), path)
    content = path.read_bytes()
    fragment = dataset_query_service.DatasetFragmentSpec(id="synthetic-fragment",
        bucket_name="synthetic", object_key="synthetic.parquet", version_id="synthetic-v1",
        content_sha256=hashlib.sha256(content).hexdigest(), byte_size=len(content), ordinal=0)
    catalog = dataset_query_service.DatasetCatalog(dataset_id="synthetic-dataset",
        dataset_version_id="synthetic-v1", relations=(dataset_query_service.DatasetRelationSpec(
            id="synthetic-table", relation_key="case_rows", ordinal=0, row_count=2,
            fragments=(fragment,), expected_columns=("record_key", "quantity", "state", "parent_key")),))
    # Catalog loading and object retrieval are isolated. SQL validation,
    # parameter binding, real Parquet/DuckDB execution and projection run intact.
    monkeypatch.setattr(dataset_query_service, "_load_catalog", lambda _source: catalog)
    monkeypatch.setattr(dataset_query_service, "_acquire_cached_fragment",
                        lambda _fragment: SimpleNamespace(path=path, release=lambda: None))
    args = {"base_entity": "Case", "base_properties": ["case_id", "amount"],
            "base_filters": [{"property": "amount", "op": "gt", "value": 10.5}]}
    world.amount.data_type = "integer"
    world.db.commit()
    with pytest.raises(business_query_service.BusinessQueryError, match="amount"):
        business_query_service.query_business_data(world.db, args=args, **_query_context(world))
    world.amount.data_type = "number"
    world.db.commit()
    result = business_query_service.query_business_data(world.db, args=args, **_query_context(world))
    assert result["records"] == [{"case_id": "case-b", "amount": 25}]
    assert result["columns"] == ["case_id", "amount"]
    relation = OntologyRelation(scenario_id=world.scenario.id, name="has parent", api_name="has_parent",
        source_entity_id=world.entity.id, target_entity_id=world.entity.id, relation_type="N:1")
    world.db.add(relation)
    world.db.commit()
    context = _query_context(world)
    mapping = context["mappings"][0]
    relationship = SimpleNamespace(id="synthetic-parent-mapping", relation_id=relation.id,
        source_mapping_id=mapping.id, target_mapping_id=mapping.id, data_source_id=mapping.data_source_id,
        status="ready", mode="source_fk", foreign_key_column="parent_key")
    context["definition"] = replace(context["definition"], relation_mappings={relationship.id: relationship})
    roles = {"base_entity": {"entity_name": "Case", "role": "child"}, "base_properties": ["case_id"],
        "related_entities": [{"entity_name": "Case", "role": "parent", "relation_id": relation.id,
                              "direction": "outgoing", "properties": ["case_id"]}]}
    result = business_query_service.query_business_data(world.db, args=roles, **context)
    assert result["records"] == [{"case_id": "case-b", "parent.case_id": "case-a"}]
    roles["base_entity"]["role"] = "parent"
    roles["related_entities"][0].update(role="child", direction="incoming")
    result = business_query_service.query_business_data(world.db, args=roles, **context)
    assert result["records"] == [{"case_id": "case-a", "child.case_id": "case-b"}]


def test_semantic_query_requires_distinct_roles_and_uses_governed_self_relation(world):
    relation = OntologyRelation(scenario_id=world.scenario.id, name="depends on",
        api_name="depends_on", source_entity_id=world.entity.id,
        target_entity_id=world.entity.id, relation_type="N:M")
    world.db.add(relation)
    world.db.commit()
    args = {"base_entity": "Case", "base_properties": ["case_id"],
            "related_entities": [{"entity_name": "Case", "properties": ["case_id"]}]}
    with pytest.raises(business_query_service.BusinessQueryError, match="角色不能重复"):
        business_query_service.prepare_query(world.db, args=args, **_query_context(world))
    context = _query_context(world)
    mapping = context["mappings"][0]
    relation_mapping = SimpleNamespace(id="synthetic-relation-map", relation_id=relation.id,
        source_mapping_id=mapping.id, target_mapping_id=mapping.id,
        data_source_id=mapping.data_source_id, status="ready", mode="source_fk", foreign_key_column="parent_key")
    context["definition"] = replace(context["definition"], relation_mappings={relation_mapping.id: relation_mapping})
    args["base_entity"] = {"entity_name": "Case", "role": "child"}
    args["related_entities"][0].update(role="parent", relation_id=relation.id, direction="outgoing")
    plan = business_query_service.prepare_query(world.db, args=args, **context)
    assert '"b"."parent_key" = "r0"."record_key"' in plan.sql
    assert plan.output_labels == ("case_id", "parent.case_id")
    args["sort"] = [{"entity_name": "Case", "property": "case_id", "direction": "asc"}]
    with pytest.raises(business_query_service.BusinessQueryError, match="多个查询角色"):
        business_query_service.prepare_query(world.db, args=args, **context)
    args["sort"][0]["role"] = "parent"
    assert 'ORDER BY "r0"."record_key" ASC' in business_query_service.prepare_query(world.db, args=args, **context).sql


def _load_reference_objects(world, reference):
    scenario = BusinessScenario(id=uuid4().hex, tenant_id=world.actor.tenant_id,
                                name=reference["name"], description=reference["description"])
    world.db.add(scenario)
    world.db.flush()
    entities = {}
    for item in reference["objects"]:
        entity = OntologyEntity(scenario_id=scenario.id, name=item["name"],
                                api_name=item["api_name"], description=item["description"])
        world.db.add(entity)
        world.db.flush()
        entities[item["api_name"]] = entity
        for prop in item["properties"]:
            values = PropertyIn.model_validate(prop).model_dump()
            world.db.add(OntologyProperty(entity_id=entity.id, **values))
    for index, item in enumerate(reference["relations"]):
        world.db.add(OntologyRelation(scenario_id=scenario.id, name=item["name"],
            api_name=f"reference_relation_{index}", description=item["description"],
            source_entity_id=entities[item["source"]].id,
            target_entity_id=entities[item["target"]].id, relation_type=item["cardinality"]))
    world.db.commit()
    world.scenario = scenario
    return entities


def test_evaluator_counts_relation_constraints_and_reports_description_changes(world):
    fixture = json.loads((Path(__file__).parent / "fixtures/construction_v1.json").read_text(
        encoding="utf-8"))
    normalized = scenario_model_compiler.normalize_scenario_model(
        world.db, world.scenario, fixture["gold_model"],
        source_bundle=scenario_model_compiler.build_source_bundle("", fixture["documents"]))
    changed = deepcopy(normalized)
    assert changed["entities"] and changed["relations"]
    changed["entities"][0]["description"] = "This means the opposite business concept."
    changed["relations"][0]["constraints"] = {"source_max_cardinality": 1}
    assert changed != normalized
    report = scenario_model_evaluator.evaluate_scenario_model(changed, normalized)
    assert report["metrics"]["micro"]["f1"] < 1.0
    assert report["metrics"]["constraint"]["f1"] < 1.0
    assert report["business_meaning_verified"] is False
    assert report["description_differences"]


@pytest.mark.parametrize("flow_index", [0, 1])
def test_legacy_reference_flows_keep_behavior_until_explicit_contract_is_declared(
    world, monkeypatch, flow_index,
):
    reference = json.loads((ROOT / "examples/project-lifecycle-collaboration/references/"
                           "scenario-definition.json").read_text(encoding="utf-8"))
    entities = _load_reference_objects(world, reference)
    assert len(entities) == 8
    assert len(world.scenario.relations) == 6
    flow = reference["analysis_flows"][flow_index]
    workflow = OntologyWorkflow(id=uuid4().hex, scenario_id=world.scenario.id,
        name=flow["name"], description=flow["description"], nodes=deepcopy(flow["nodes"]),
        edges=deepcopy(flow["edges"]), trigger_type="manual", enabled=True, status="active")
    world.db.add(workflow)
    world.db.commit()
    captured = []

    def fake_chat(_config, messages, **_kwargs):
        captured.append(deepcopy(messages))
        return {"content": '{"unrelated": true}'}

    # Only the LLM configuration/transport is stubbed; the executor and its
    # readiness, permission, rendering, parsing and audit persistence are real.
    monkeypatch.setattr(workflow_service, "_resolve_llm", lambda *_args: object())
    monkeypatch.setattr(workflow_service.llm_service, "chat", fake_chat)
    context = json.loads((ROOT / "examples/project-lifecycle-collaboration/fixtures/"
                         "kickoff-context.json").read_text(encoding="utf-8"))
    params = {"context": context}
    first = workflow_service.execute_workflow(world.db, workflow, params,
                                              runtime_definition=_definition(world))
    assert first["status"] == "success", first["error"]
    first_messages = deepcopy(captured)
    assert len(first_messages) == 2
    captured.clear()
    entity = entities["project_context"]
    entity.description = "Changed definition: this object is an invoice."
    revision = next(prop for prop in entity.properties if prop.api_name == "context_revision")
    revision.data_type = "string"
    revision.constraints = {"min_length": 1000}
    world.scenario.relations[0].description = "Changed relationship business meaning."
    world.db.commit()
    second = workflow_service.execute_workflow(world.db, workflow, params,
                                               runtime_definition=_definition(world))
    assert second["status"] == "success", second["error"]
    assert captured == first_messages
    assert second["steps"] == first["steps"]
    llm_steps = [step for step in second["steps"] if step.get("type") == "llm"]
    assert len(llm_steps) == 2
    assert all(step["result"]["parsed"] == {"unrelated": True} for step in llm_steps)


def test_unknown_first_class_semantics_are_rejected_by_model_dto():
    with pytest.raises(ValidationError):
        PropertyIn.model_validate({"name": "amount", "data_type": "number", "unit": "USD"})
    with pytest.raises(ValidationError):
        EntityIn.model_validate({"name": "Employee", "subclass_of": "Person"})


def test_workflow_contract_reads_pinned_ontology_and_rejects_wrong_business_output(world, monkeypatch):
    from app.services.policies import PolicyViolation
    workflow = OntologyWorkflow(scenario_id=world.scenario.id, name="Typed analysis",
        nodes=[{"id": "start", "type": "start"}, {"id": "analyze", "type": "llm",
                "data": {"prompt": "{{params.case}}", "system": "Return the business result"}},
               {"id": "end", "type": "end"}],
        edges=[{"source": "start", "target": "analyze"}, {"source": "analyze", "target": "end"}],
        trigger_type="manual", status="active", enabled=True, trigger_config={"ontology_contract": {
            "version": 1, "entity_ids": [world.entity.id], "input_bindings": [
                {"path": "case", "entity_id": world.entity.id}], "output_node_id": "analyze",
            "output_schema": {"type": "object", "properties": {"approved": {"type": "boolean"}},
                              "required": ["approved"], "additionalProperties": False}}})
    world.db.add(workflow)
    world.db.commit()
    captured = []
    response = {"content": '{"unrelated": true}'}
    monkeypatch.setattr(workflow_service, "_resolve_llm", lambda *_args: object())
    def chat(_config, messages, **_kwargs):
        captured.append(deepcopy(messages))
        return response
    monkeypatch.setattr(workflow_service.llm_service, "chat", chat)
    definition = _definition(world)
    with pytest.raises(PolicyViolation):
        workflow_service.execute_workflow(world.db, workflow, {"case": {"amount": -1}}, runtime_definition=definition)
    params = {"case": {"case_id": "case-a", "amount": 12, "status": "draft"}}
    invalid = workflow_service.execute_workflow(world.db, workflow, params, runtime_definition=definition)
    assert invalid["status"] == "failed"
    assert "业务输出" in invalid["error"]
    assert world.entity.description in json.dumps(captured[0], ensure_ascii=False)
    response["content"] = '{"approved": true}'
    valid = workflow_service.execute_workflow(world.db, workflow, params, runtime_definition=definition)
    assert valid["status"] == "success", valid["error"]
    assert next(step for step in valid["steps"] if step["node"] == "analyze")["contract_validation"] == "passed"
    world.entity.description = "An updated business meaning"
    world.db.commit()
    workflow_service.execute_workflow(world.db, workflow, params, runtime_definition=_definition(world))
    assert "An updated business meaning" in json.dumps(captured[-1])
    assert "An updated business meaning" not in json.dumps(captured[0])
    from app.services.workflow_ontology_contract import llm_messages
    intermediate = llm_messages(workflow, definition, "Analyze evidence", "Current input",
                                db=world.db, node_id="intermediate")
    assert '"output_contract"' not in intermediate[1]["content"]


def test_original_editor_read_dto_roundtrip_preserves_state_policy(world):
    world.entity.state_policy = {"enabled": True, "initial_states": ["draft"], "transitions": []}
    world.db.commit()
    payload = EntityIn.model_validate(scenarios._entity_out(world.db, world.entity).model_dump(mode="json"))
    updated = scenarios.update_entity(world.entity.id, payload, db=world.db)
    assert updated.state_policy.enabled is True
    omitted = payload.model_dump(exclude={"state_policy"})
    updated = scenarios.update_entity(world.entity.id, EntityIn.model_validate(omitted), db=world.db)
    assert updated.state_policy.enabled is True


def test_manual_identity_uniqueness_is_enforced_across_concurrent_transactions(world):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy.exc import IntegrityError
    barrier = Barrier(2)
    engine, entity_id, scenario_id = world.db.get_bind(), world.entity.id, world.scenario.id
    def create():
        with Session(engine) as session:
            session.add(OntologyInstance(entity_id=entity_id, scenario_id=scenario_id, name="Concurrent case",
                source="manual", attributes={"case_id": "concurrent-key", "amount": 12}, state="draft"))
            barrier.wait(timeout=10)
            try:
                session.commit()
                return "created"
            except IntegrityError as exc:
                session.rollback()
                return exc.orig.diag.constraint_name
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create) for _ in range(2)]
        assert sorted(future.result(timeout=20) for future in futures) == ["created", "uq_instances_business_key"]


def test_equivalent_numeric_business_keys_cannot_create_distinct_objects(world):
    key = next(prop for prop in world.entity.properties if prop.is_key)
    key.data_type = "number"
    world.db.commit()
    def create(value):
        return scenarios.create_instance(world.scenario.id, InstanceIn(entity_id=world.entity.id,
            name="Numeric identity", attributes={"case_id": value, "amount": 12}), db=world.db)
    create(1)
    with pytest.raises(HTTPException) as rejected:
        create(1.0)
    assert rejected.value.status_code == 409
    # This is an unsupported-field diagnostic, not proof that users cannot
    # represent currency or categories as explicit properties/relationships.
