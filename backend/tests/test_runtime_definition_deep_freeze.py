from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models import (
    Base,
    BusinessScenario,
    ConnectorBinding,
    FunctionDefinition,
    OntologyAction,
    OntologyEntity,
    OntologyProperty,
    OntologySnapshot,
    OntologyWorkflow,
    ScenarioCapabilityPort,
    Tenant,
)
from app.services import release_service, runtime_definition_service
from app.services.capability_contracts import DataPort
from app.services.deployment_service import build_resolved_deployment


@pytest.fixture
def runtime_world() -> tuple[Session, dict[str, object]]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine, expire_on_commit=False)
    tenant = Tenant(id="tenant-deep-freeze", name="Deep freeze tenant")
    scenario = BusinessScenario(
        id="scenario-deep-freeze",
        tenant_id=tenant.id,
        name="Definition A",
    )
    entity = OntologyEntity(
        id="entity-deep-freeze",
        scenario_id=scenario.id,
        name="Runtime object",
    )
    prop = OntologyProperty(
        id="property-deep-freeze",
        entity_id=entity.id,
        name="Value",
        is_key=True,
        is_title=True,
        is_required=True,
        constraints={"min_length": 0},
    )
    function = FunctionDefinition(
        id="function-deep-freeze",
        scenario_id=scenario.id,
        name="Runtime function",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "number"}},
        },
        output_schema={"type": "object"},
        runtime_kind="weighted_score",
        runtime_config={"weights": {"value": 1}},
    )
    action = OntologyAction(
        id="action-deep-freeze",
        scenario_id=scenario.id,
        entity_id=entity.id,
        name="Runtime action",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "number"}},
        },
        executor_type="script",
        executor_config={"options": {"timeout": 10}},
        enabled=True,
        requires_confirmation=True,
        idempotency_required=True,
    )
    workflow = OntologyWorkflow(
        id="workflow-deep-freeze",
        scenario_id=scenario.id,
        name="Runtime workflow",
        trigger_type="manual",
        trigger_config={},
        steps=[],
        nodes=[
            {"id": "start", "type": "start", "data": {"label": "A"}},
            {"id": "end", "type": "end", "data": {}},
        ],
        edges=[{"id": "edge", "source": "start", "target": "end"}],
        status="active",
        enabled=True,
    )
    port = ScenarioCapabilityPort(
        id="port-deep-freeze",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        capability_kind="function",
        capability_key=function.id,
        port_key="runtime_input",
        name="Runtime input",
        direction="input",
        role="invocation_input",
        media_kind="dataset",
        schema_document={
            "type": "object",
            "properties": {"record_id": {"type": "string"}},
        },
        is_required=True,
        binding_policy="per_invocation",
        status="active",
        config={"allow_override": True, "limits": {"rows": 100}},
    )
    db.add_all(
        [tenant, scenario, entity, prop, function, action, workflow, port]
    )
    db.commit()
    world: dict[str, object] = {
        "scenario": scenario,
        "entity": entity,
        "function": function,
        "action": action,
        "workflow": workflow,
        "port": port,
    }
    try:
        yield db, world
    finally:
        db.close()
        engine.dispose()


def _deployment(definition, signature: str):
    port = definition.capability_ports["port-deep-freeze"]
    contract = DataPort(
        key=port.port_key,
        modality=port.media_kind,
        schema=port.schema_document,
        required=port.is_required,
        binding_kinds=("connector_binding",),
        override_policy="managed-reference",
    )
    binding = ConnectorBinding(
        tenant_id=definition.scenario.tenant_id,
        scenario_id=definition.scenario.id,
        environment=definition.environment,
        binding_key=port.port_key,
        connector_kind="connector_binding",
        connector_id="managed-reference-a",
        connector_signature=signature,
    )
    return build_resolved_deployment(
        definition,
        data_ports=(contract,),
        bindings=(binding,),
    ), binding


def test_live_resolve_deep_freezes_orm_json_and_deployment_binding(
    runtime_world,
) -> None:
    db, world = runtime_world
    scenario = world["scenario"]
    action = world["action"]
    entity = world["entity"]
    function = world["function"]
    workflow = world["workflow"]
    port = world["port"]

    definition_a = runtime_definition_service.resolve_active(
        db,
        scenario,
        environment="dev",
    )
    deployment_a, binding = _deployment(definition_a, "a" * 64)
    definition_hash_a = definition_a.definition_hash
    fingerprint_a = deployment_a.fingerprint

    with pytest.raises(TypeError):
        definition_a.actions[action.id].executor_config["options"]["timeout"] = 99
    with pytest.raises(TypeError):
        definition_a.actions[action.id].input_schema["properties"]["value"] = {}
    with pytest.raises(TypeError):
        definition_a.workflows[workflow.id].nodes.append({"id": "late"})
    with pytest.raises(TypeError):
        definition_a.capability_ports[port.id].config["limits"]["rows"] = 0
    with pytest.raises(TypeError):
        definition_a.actions[action.id] = object()
    with pytest.raises(TypeError):
        vars(definition_a.actions[action.id])["name"] = "late"
    with pytest.raises(TypeError):
        definition_a.scenario.name = "late"
    with pytest.raises(TypeError):
        definition_a.entities[entity.id].properties[0].constraints["min_length"] = 9
    mutable_schema = copy.deepcopy(definition_a.actions[action.id].input_schema)
    mutable_schema["properties"]["value"]["type"] = "string"
    assert (
        definition_a.actions[action.id].input_schema["properties"]["value"]["type"]
        == "number"
    )

    scenario.name = "Definition B"
    entity.properties[0].constraints["min_length"] = 1
    action.executor_config["options"]["timeout"] = 20
    action.input_schema["properties"]["value"]["type"] = "integer"
    function.input_schema["properties"]["value"]["type"] = "integer"
    workflow.nodes[0]["data"]["label"] = "B"
    port.schema_document["properties"]["record_id"]["type"] = "integer"
    port.config["limits"]["rows"] = 200
    for row, field in (
        (entity.properties[0], "constraints"),
        (action, "executor_config"),
        (action, "input_schema"),
        (function, "input_schema"),
        (workflow, "nodes"),
        (port, "schema_document"),
        (port, "config"),
    ):
        flag_modified(row, field)
    binding.signature = "b" * 64
    db.commit()

    assert definition_a.definition_hash == definition_hash_a
    assert deployment_a.fingerprint == fingerprint_a
    assert deployment_a.definition is definition_a
    assert deployment_a.data_context.handles[0].signature == "a" * 64
    assert definition_a.scenario.name == "Definition A"
    assert definition_a.entities[entity.id].properties[0].constraints["min_length"] == 0
    assert definition_a.actions[action.id].executor_config["options"]["timeout"] == 10
    assert (
        definition_a.actions[action.id].input_schema["properties"]["value"]["type"]
        == "number"
    )
    assert (
        definition_a.functions[function.id].input_schema["properties"]["value"]["type"]
        == "number"
    )
    assert definition_a.workflows[workflow.id].nodes[0]["data"]["label"] == "A"
    assert (
        definition_a.capability_ports[port.id]
        .schema_document["properties"]["record_id"]["type"]
        == "string"
    )
    assert definition_a.capability_ports[port.id].config["limits"]["rows"] == 100

    definition_b = runtime_definition_service.resolve_active(
        db,
        scenario,
        environment="dev",
    )
    deployment_b, _ = _deployment(definition_b, binding.signature)
    assert definition_b.definition_hash != definition_hash_a
    assert deployment_b.fingerprint != fingerprint_a
    assert definition_b.scenario.name == "Definition B"
    assert definition_b.entities[entity.id].properties[0].constraints["min_length"] == 1
    assert definition_b.actions[action.id].executor_config["options"]["timeout"] == 20
    assert (
        definition_b.functions[function.id].input_schema["properties"]["value"]["type"]
        == "integer"
    )
    assert definition_b.workflows[workflow.id].nodes[0]["data"]["label"] == "B"
    assert definition_b.capability_ports[port.id].config["limits"]["rows"] == 200
    assert deployment_b.data_context.handles[0].signature == "b" * 64


def test_capability_port_owner_is_frozen_and_participates_in_live_hash(
    runtime_world,
) -> None:
    db, world = runtime_world
    scenario = world["scenario"]
    function = world["function"]
    workflow = world["workflow"]
    port = world["port"]

    definition_a = runtime_definition_service.resolve_active(
        db, scenario, environment="dev"
    )
    assert definition_a.capability_ports[port.id].capability_kind == "function"
    assert definition_a.capability_ports[port.id].capability_key == function.id

    port.capability_kind = "workflow"
    port.capability_key = workflow.id
    db.flush()
    definition_b = runtime_definition_service.resolve_active(
        db, scenario, environment="dev"
    )

    assert definition_b.definition_hash != definition_a.definition_hash
    assert definition_a.capability_ports[port.id].capability_kind == "function"
    assert definition_a.capability_ports[port.id].capability_key == function.id
    assert definition_b.capability_ports[port.id].capability_kind == "workflow"
    assert definition_b.capability_ports[port.id].capability_key == workflow.id


def test_release_resolve_deep_freezes_snapshot_content_and_new_resolve_reads_b(
    runtime_world,
) -> None:
    db, world = runtime_world
    scenario = world["scenario"]
    action = world["action"]
    function = world["function"]
    workflow = world["workflow"]
    port = world["port"]
    content_a = release_service.capture_snapshot_content(db, scenario)
    snapshot_a = OntologySnapshot(
        id="snapshot-deep-freeze-a",
        tenant_id=scenario.tenant_id,
        scenario_id=scenario.id,
        kind="merge",
        content=content_a,
        content_hash=release_service.snapshot_hash(content_a),
    )
    definition_a = runtime_definition_service._from_snapshot(
        scenario,
        "staging",
        snapshot_a,
        release=SimpleNamespace(id="release-deep-freeze-a"),
    )
    deployment_a, _ = _deployment(definition_a, "c" * 64)
    definition_hash_a = definition_a.definition_hash
    fingerprint_a = deployment_a.fingerprint

    action_a = next(item for item in snapshot_a.content["actions"] if item["id"] == action.id)
    workflow_a = next(
        item for item in snapshot_a.content["workflows"] if item["id"] == workflow.id
    )
    port_a = next(
        item
        for item in snapshot_a.content["capability_ports"]
        if item["port_key"] == port.port_key
    )
    action_a["executor_config"]["options"]["timeout"] = 30
    action_a["input_schema"]["properties"]["value"]["type"] = "integer"
    workflow_a["nodes"][0]["data"]["label"] = "C"
    port_a["config"]["limits"]["rows"] = 300
    port_a["capability_kind"] = "workflow"
    port_a["capability_key"] = workflow.id

    assert definition_a.definition_hash == definition_hash_a
    assert deployment_a.fingerprint == fingerprint_a
    assert definition_a.actions[action.id].executor_config["options"]["timeout"] == 10
    assert definition_a.workflows[workflow.id].nodes[0]["data"]["label"] == "A"
    assert definition_a.capability_ports[port.id].config["limits"]["rows"] == 100
    assert definition_a.capability_ports[port.id].capability_kind == "function"
    assert definition_a.capability_ports[port.id].capability_key == function.id
    with pytest.raises(TypeError):
        definition_a.workflows[workflow.id].nodes[0]["data"]["label"] = "late"

    content_b = copy.deepcopy(snapshot_a.content)
    snapshot_b = OntologySnapshot(
        id="snapshot-deep-freeze-b",
        tenant_id=scenario.tenant_id,
        scenario_id=scenario.id,
        kind="merge",
        content=content_b,
        content_hash=release_service.snapshot_hash(content_b),
    )
    definition_b = runtime_definition_service._from_snapshot(
        scenario,
        "staging",
        snapshot_b,
        release=SimpleNamespace(id="release-deep-freeze-b"),
    )
    deployment_b, _ = _deployment(definition_b, "c" * 64)
    assert definition_b.definition_hash != definition_hash_a
    assert deployment_b.fingerprint != fingerprint_a
    assert definition_b.actions[action.id].executor_config["options"]["timeout"] == 30
    assert (
        definition_b.actions[action.id].input_schema["properties"]["value"]["type"]
        == "integer"
    )
    assert definition_b.workflows[workflow.id].nodes[0]["data"]["label"] == "C"
    assert definition_b.capability_ports[port.id].config["limits"]["rows"] == 300
    assert definition_b.capability_ports[port.id].capability_kind == "workflow"
    assert definition_b.capability_ports[port.id].capability_key == workflow.id
