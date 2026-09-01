from __future__ import annotations

import asyncio
import json
import subprocess
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import (
    ActionExecutionLog,
    Base,
    BusinessScenario,
    CapabilityInvocation,
    ConnectorBinding,
    FunctionDefinition,
    OntologyAction,
    OntologyEntity,
    OntologyRule,
    OntologyWorkflow,
    ScenarioCapabilityPort,
    Skill,
    Tenant,
    User,
    WorkflowRun,
)
from app.services import (
    function_runtime_service,
    mcp_service,
    operations_service,
    permission_service,
    runtime_definition_service,
    skill_service,
    workflow_service,
)
from app.services.capability_contracts import (
    Actor,
    BindingOverride,
    CapabilityRef,
    DataPort,
    Request,
    ResolvedDataHandle,
    RuntimeDataContext,
)
from app.services.capability_invoker import (
    CapabilityInvocationError,
    CapabilityInvoker,
    resolve_capability_contract,
    resolve_provider_binding,
)
from app.services.capability_provider_keys import BUILTIN_PROVIDER_KEYS
from app.services.capability_provider_keys import derive_provider_execution_key
from app.services.capability_registry import default_provider_registry
from app.services.deployment_service import build_resolved_deployment
from app.services.permission_service import PermissionDecision
from app.services.policies import PolicyViolation


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@dataclass(frozen=True)
class World:
    actor: Actor
    action: OntologyAction
    deployment: object
    function: FunctionDefinition
    rule: OntologyRule
    scenario: BusinessScenario
    workflow: OntologyWorkflow


def _runtime_copy(resource, scenario: BusinessScenario):
    fields = {
        column.key: getattr(resource, column.key)
        for column in resource.__table__.columns
    }
    fields["scenario"] = scenario
    return SimpleNamespace(**fields)


def _world(db: Session, key: str) -> World:
    tenant = Tenant(id=f"tenant-{key}", name=f"Tenant {key}")
    user = User(
        id=f"user-{key}",
        tenant_id=tenant.id,
        email=f"{key}@example.test",
        password_hash="test-only",
        status="active",
    )
    scenario = BusinessScenario(
        id=f"scenario-{key}",
        tenant_id=tenant.id,
        name=f"Scenario {key}",
    )
    entity = OntologyEntity(
        id=f"entity-{key}",
        scenario_id=scenario.id,
        name="Runtime object",
    )
    function = FunctionDefinition(
        id=f"function-{key}",
        scenario_id=scenario.id,
        name="Weighted score",
        input_schema={
            "type": "object",
            "properties": {"amount": {"type": "number"}},
            "required": ["amount"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"score": {"type": "number"}},
        },
        runtime_kind="weighted_score",
        runtime_config={"weights": {"amount": 0.5}, "bias": 2},
    )
    skill = Skill(
        id=f"skill-{key}",
        tenant_id=tenant.id,
        name=f"skill-{key}",
        path="managed/test-skill",
        enabled=True,
        meta={"idempotency_mode": "capability_execution_key_env"},
    )
    action = OntologyAction(
        id=f"action-{key}",
        scenario_id=scenario.id,
        entity_id=entity.id,
        name="Managed action",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        executor_type="skill",
        executor_config={"skill_id": skill.id},
        enabled=True,
        requires_confirmation=True,
        idempotency_required=True,
    )
    rule = OntologyRule(
        id=f"rule-{key}",
        scenario_id=scenario.id,
        name="Amount threshold",
        condition={"field": "amount", "op": ">", "value": 2},
        action_on_match="Send the record for review.",
        severity="warning",
        enabled=True,
    )
    workflow = OntologyWorkflow(
        id=f"workflow-{key}",
        scenario_id=scenario.id,
        name="Durable workflow",
        trigger_type="manual",
        trigger_config={
            "input_schema": {
                "type": "object",
                "properties": {"case_id": {"type": "string"}},
                "required": ["case_id"],
                "additionalProperties": False,
            }
        },
        nodes=[
            {"id": "start", "type": "start", "data": {"name": "Start"}},
            {"id": "end", "type": "end", "data": {"name": "End"}},
        ],
        edges=[{"id": "edge", "source": "start", "target": "end", "label": ""}],
        status="active",
        enabled=True,
    )
    db.add_all([tenant, user, scenario, entity, function, skill, action, rule, workflow])
    db.commit()
    permission_service.ensure_organization(db, tenant.id, owner_user_id=user.id)
    db.commit()
    db.info["tenant_id"] = tenant.id
    db.info["user_id"] = user.id

    frozen_function = _runtime_copy(function, scenario)
    frozen_action = _runtime_copy(action, scenario)
    frozen_action.entity = entity
    frozen_workflow = _runtime_copy(workflow, scenario)
    definition = runtime_definition_service.RuntimeDefinition(
        scenario=scenario,
        environment="dev",
        source="live",
        snapshot_id=None,
        release_id=None,
        definition_hash="d" * 64,
        scenario_name=scenario.name,
        entities={entity.id: entity},
        relations={},
        actions={action.id: frozen_action},
        functions={function.id: frozen_function},
        mappings={},
        relation_mappings={},
        rules={rule.id: _runtime_copy(rule, scenario)},
        events={},
        workflows={workflow.id: frozen_workflow},
        capability_ports={},
    )
    deployment = build_resolved_deployment(definition)
    actor = Actor(
        actor_type="user",
        principal_id=user.id,
        tenant_id=tenant.id,
    )
    return World(actor, action, deployment, function, rule, scenario, workflow)


def _with_managed_connector(db: Session, world: World, key: str) -> World:
    capability = getattr(world, key)
    port = ScenarioCapabilityPort(
        id=f"port-{key}",
        tenant_id=world.scenario.tenant_id,
        scenario_id=world.scenario.id,
        capability_kind=key,
        capability_key=capability.id,
        port_key="runtime_reference",
        name="Runtime reference",
        direction="input",
        role="reference",
        media_kind="connector",
        is_required=True,
        cardinality="one",
        binding_policy="per_invocation",
        status="active",
    )
    binding = ConnectorBinding(
        id=f"binding-{key}",
        tenant_id=world.scenario.tenant_id,
        scenario_id=world.scenario.id,
        environment="dev",
        binding_key=port.port_key,
        connector_kind="data_source",
        connector_id=f"connector-{key}",
        health_status="healthy",
        connector_signature="a" * 64,
    )
    db.add_all([port, binding])
    db.flush()

    definition = replace(
        world.deployment.definition,
        capability_ports={port.id: _runtime_copy(port, world.scenario)},
    )
    data_port = DataPort(
        key=port.port_key,
        modality=port.media_kind,
        schema=port.schema_document,
        required=True,
        binding_kinds=("connector_binding",),
        override_policy="managed-reference",
    )
    handle = ResolvedDataHandle(
        port_key=port.port_key,
        binding_kind="connector_binding",
        reference_id=binding.id,
        signature=binding.connector_signature,
    )
    return replace(
        world,
        deployment=build_resolved_deployment(
            definition,
            data_ports=(data_port,),
            bindings=(handle,),
        ),
    )


def _request(
    world: World,
    kind: str,
    resource_id: str,
    *,
    correlation_id: str,
    inputs: dict,
    mode: str = "execute",
    idempotency_key: str | None = None,
    confirmation: dict | None = None,
    provider_key: str | None = None,
) -> Request:
    managed_inputs = tuple(
        BindingOverride(
            port_key=handle.port_key,
            binding_kind=handle.binding_kind,
            reference_id=handle.reference_id,
            signature=handle.signature,
            version_id=handle.version_id,
        )
        for handle in world.deployment.data_context.handles
    )
    return Request(
        capability=CapabilityRef(
            kind=kind,
            resource_id=resource_id,
            provider_key=provider_key,
        ),
        inputs=inputs,
        binding_overrides=managed_inputs,
        mode=mode,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
        confirmation=confirmation or {},
        expected_definition_hash=world.deployment.definition_hash,
        expected_deployment_fingerprint=world.deployment.fingerprint,
    )


def _invoke(db: Session, world: World, request: Request):
    return CapabilityInvoker().invoke(
        db,
        world.deployment,
        world.actor,
        request,
        invocation_source="internal",
    )


def test_builtin_bindings_are_server_owned_and_registered(db: Session) -> None:
    world = _world(db, "binding")

    for kind, resource_id in (
        ("function", world.function.id),
        ("action", world.action.id),
        ("rule", world.rule.id),
        ("workflow", world.workflow.id),
    ):
        capability = CapabilityRef(kind=kind, resource_id=resource_id)
        assert resolve_provider_binding(world.deployment, capability) == (
            BUILTIN_PROVIDER_KEYS[kind]
        )
        assert BUILTIN_PROVIDER_KEYS[kind] in default_provider_registry

    execution_key = derive_provider_execution_key(
        _request(
            world,
            "action",
            world.action.id,
            correlation_id="execution-key",
            inputs={"value": 1},
            idempotency_key="caller-key",
        ),
        world.actor,
        world.deployment,
    )
    assert execution_key.startswith("cap:")
    assert execution_key.endswith(":caller-key")

    with pytest.raises(CapabilityInvocationError) as captured:
        resolve_provider_binding(
            world.deployment,
            CapabilityRef(
                kind="function",
                resource_id=world.function.id,
                provider_key="client.chosen-provider",
            ),
        )
    assert captured.value.code == "provider_binding_mismatch"


def test_rule_uses_unified_invoker_and_never_executes_side_effects(db: Session) -> None:
    world = _world(db, "rule-invocation")

    contract = resolve_capability_contract(
        db,
        world.deployment,
        CapabilityRef(kind="rule", resource_id=world.rule.id),
    )
    assert contract["input_schema"] == {
        "type": "object",
        "properties": {
            "record": {
                "type": "object",
                "properties": {"amount": {}},
                "required": ["amount"],
                "additionalProperties": False,
            }
        },
        "required": ["record"],
        "additionalProperties": False,
    }
    assert contract["side_effect"] is False
    assert contract["requires_confirmation"] is False

    matched = _invoke(
        db,
        world,
        _request(
            world,
            "rule",
            world.rule.id,
            correlation_id="rule-matched",
            inputs={"record": {"amount": 3}},
        ),
    )
    assert matched.status == "succeeded", (matched.error_code, matched.error_message)
    assert matched.output["matched"] is True
    assert matched.output["action_on_match"] == "Send the record for review."
    assert matched.output["side_effects_executed"] is False

    not_matched = _invoke(
        db,
        world,
        _request(
            world,
            "rule",
            world.rule.id,
            correlation_id="rule-boundary",
            inputs={"record": {"amount": 2}},
        ),
    )
    assert not_matched.status == "succeeded"
    assert not_matched.output["matched"] is False
    assert not_matched.output["action_on_match"] == ""
    assert not_matched.output["side_effects_executed"] is False


def test_contract_discovery_does_not_require_runtime_readiness(db: Session) -> None:
    world = _world(db, "contract-discovery")
    frozen_action = world.deployment.definition.actions[world.action.id]
    frozen_action.enabled = False
    frozen_action.requires_confirmation = False
    frozen_action.idempotency_required = False

    contract = resolve_capability_contract(
        db,
        world.deployment,
        CapabilityRef(kind="action", resource_id=world.action.id),
    )

    assert contract["side_effect"] is True
    assert contract["requires_confirmation"] is True
    assert contract["idempotency_required"] is True
    assert contract["input_schema"]["required"] == ["value"]
    assert contract["contract_hash"]
    assert "provider_key" not in contract

    receipt = _invoke(
        db,
        world,
        _request(
            world,
            "action",
            world.action.id,
            correlation_id="unready-action-preview",
            inputs={"value": 1},
            mode="preview",
            idempotency_key="unready-action-key",
        ),
    )
    assert receipt.status == "failed"


def test_function_provider_uses_frozen_runtime_definition_and_preview_is_safe(
    db: Session,
) -> None:
    world = _world(db, "function")
    world.function.runtime_config = {"weights": {"amount": 100}, "bias": 0}
    db.flush()

    preview = _invoke(
        db,
        world,
        _request(
            world,
            "function",
            world.function.id,
            correlation_id="function-preview",
            inputs={"amount": 10},
            mode="preview",
        ),
    )
    executed = _invoke(
        db,
        world,
        _request(
            world,
            "function",
            world.function.id,
            correlation_id="function-execute",
            inputs={"amount": 10},
        ),
    )

    assert preview.status == "succeeded"
    assert preview.output["side_effects_skipped"] is True
    assert executed.output == {"score": 7.0}


@pytest.mark.parametrize(
    ("kind", "mode", "inputs"),
    (
        ("function", "execute", {"amount": 10}),
        ("function", "preview", {"amount": 10}),
        ("action", "preview", {"value": 3}),
        ("workflow", "preview", {"case_id": "case-1"}),
    ),
)
def test_builtin_providers_fail_closed_before_ignoring_managed_runtime_inputs(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    mode: str,
    inputs: dict,
) -> None:
    world = _with_managed_connector(db, _world(db, f"managed-{kind}"), kind)
    calls: list[str] = []
    monkeypatch.setattr(
        function_runtime_service,
        "execute_function",
        lambda *_args, **_kwargs: calls.append("function") or {"score": 999},
    )
    monkeypatch.setattr(
        workflow_service,
        "preview_action",
        lambda *_args, **_kwargs: calls.append("action") or {},
    )
    monkeypatch.setattr(
        operations_service,
        "enqueue_workflow_run",
        lambda *_args, **_kwargs: calls.append("workflow"),
    )
    resource = getattr(world, kind)

    receipt = _invoke(
        db,
        world,
        _request(
            world,
            kind,
            resource.id,
            correlation_id=f"managed-{kind}",
            inputs=inputs,
            mode=mode,
            idempotency_key=(f"managed-{kind}-key" if kind != "function" else None),
        ),
    )
    invocation = db.get(CapabilityInvocation, receipt.invocation_id)

    assert receipt.status == "failed"
    assert receipt.error_code == "provider_execution_failed"
    assert receipt.output == {}
    assert receipt.confirmation == {}
    assert receipt.data_context_fingerprint == world.deployment.data_context.fingerprint
    assert receipt.data_context_fingerprint != RuntimeDataContext().fingerprint
    assert invocation.result_document["output"] == {}
    assert len(invocation.request_document["managed_inputs"]) == 1
    assert calls == []
    assert db.scalar(select(func.count(ActionExecutionLog.id))) == 0
    assert db.scalar(select(func.count(WorkflowRun.id))) == 0


def test_side_effecting_builtin_entry_points_reject_context_before_execution(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _with_managed_connector(
        db,
        _world(db, "managed-side-effects"),
        "action",
    )
    calls: list[str] = []
    monkeypatch.setattr(
        workflow_service,
        "execute_action",
        lambda *_args, **_kwargs: calls.append("action-execute"),
    )
    monkeypatch.setattr(
        workflow_service,
        "recover_action_execution",
        lambda *_args, **_kwargs: calls.append("action-recover"),
    )
    monkeypatch.setattr(
        operations_service,
        "enqueue_workflow_run",
        lambda *_args, **_kwargs: calls.append("workflow-enqueue"),
    )
    action_request = _request(
        world,
        "action",
        world.action.id,
        correlation_id="managed-action-confirm",
        inputs={"value": 3},
        mode="confirm",
        idempotency_key="managed-action-confirm-key",
        confirmation={"preview_invocation_id": "not-consulted"},
    )
    workflow_request = _request(
        world,
        "workflow",
        world.workflow.id,
        correlation_id="managed-workflow-confirm",
        inputs={"case_id": "case-1"},
        mode="confirm",
        idempotency_key="managed-workflow-confirm-key",
        confirmation={"preview_invocation_id": "not-consulted"},
    )
    action_provider = default_provider_registry.resolve(
        BUILTIN_PROVIDER_KEYS["action"]
    ).bind_invocation(db)
    workflow_provider = default_provider_registry.resolve(
        BUILTIN_PROVIDER_KEYS["workflow"]
    ).bind_invocation(db)

    for operation in (
        lambda: action_provider.invoke(
            action_request,
            world.actor,
            world.deployment,
            world.deployment.data_context,
        ),
        lambda: action_provider.recover(
            action_request,
            world.actor,
            world.deployment,
            world.deployment.data_context,
        ),
        lambda: workflow_provider.invoke(
            workflow_request,
            world.actor,
            world.deployment,
            world.deployment.data_context,
        ),
    ):
        with pytest.raises(
            ValueError,
            match="does not support managed runtime inputs",
        ):
            operation()

    assert calls == []
    assert db.scalar(select(func.count(ActionExecutionLog.id))) == 0
    assert db.scalar(select(func.count(WorkflowRun.id))) == 0


def test_legacy_flat_action_schema_is_exposed_as_valid_json_schema(
    db: Session,
) -> None:
    world = _world(db, "legacy-schema")
    frozen_action = world.deployment.definition.actions[world.action.id]
    frozen_action.input_schema = {
        "value": {"type": "integer", "required": True},
    }
    normalized = workflow_service.normalize_parameter_schema(
        frozen_action.input_schema
    )

    Draft202012Validator.check_schema(normalized)
    assert normalized["required"] == ["value"]
    assert "required" not in normalized["properties"]["value"]

    with pytest.raises(CapabilityInvocationError) as captured:
        _invoke(
            db,
            world,
            _request(
                world,
                "action",
                world.action.id,
                correlation_id="legacy-schema-missing",
                inputs={},
                mode="preview",
                idempotency_key="legacy-schema-key",
            ),
        )
    assert captured.value.code == "input_schema_invalid"


def test_action_provider_previews_without_execution_and_confirms_once(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(db, "action")
    dispatches: list[dict] = []

    def dispatch(_db, _action, params, **_kwargs):
        dispatches.append(dict(params))
        return {"accepted": True}, []

    monkeypatch.setattr(workflow_service, "_dispatch_executor", dispatch)
    preview_request = _request(
        world,
        "action",
        world.action.id,
        correlation_id="action-confirmation",
        inputs={"value": 3},
        mode="preview",
        idempotency_key="action-key",
    )
    preview = _invoke(db, world, preview_request)

    assert preview.status == "awaiting_confirmation"
    assert preview.output["side_effects_skipped"] is True
    assert dispatches == []
    assert db.scalar(select(func.count(WorkflowRun.id))) == 0

    confirm_request = _request(
        world,
        "action",
        world.action.id,
        correlation_id="action-confirmation",
        inputs={"value": 3},
        mode="confirm",
        idempotency_key="action-key",
        confirmation=dict(preview.confirmation),
    )
    confirmed = _invoke(db, world, confirm_request)
    replay = _invoke(db, world, confirm_request)

    assert confirmed.status == "succeeded"
    assert replay.audit_ref["replayed"] is True
    assert dispatches == [{"value": 3}]
    execute_log = db.scalar(
        select(ActionExecutionLog).where(ActionExecutionLog.mode == "execute")
    )
    assert execute_log.idempotency_key.startswith("dev:cap:")
    assert execute_log.idempotency_key.endswith(":action-key")
    assert execute_log.parent_action_log_id == preview.output["preview_log_id"]
    assert execute_log.input_params["contract"] == (
        "capability-structured-input-audit/v1"
    )


def test_workflow_provider_only_enqueues_durable_run_and_reuses_dedupe(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(db, "workflow")
    executed = []
    monkeypatch.setattr(
        workflow_service,
        "execute_workflow",
        lambda *_args, **_kwargs: executed.append(True),
    )
    preview_request = _request(
        world,
        "workflow",
        world.workflow.id,
        correlation_id="workflow-confirmation",
        inputs={"case_id": "case-1"},
        mode="preview",
        idempotency_key="workflow-key",
    )
    preview = _invoke(db, world, preview_request)
    assert preview.status == "awaiting_confirmation"
    assert executed == []
    assert db.scalar(select(func.count(WorkflowRun.id))) == 0

    confirm_request = _request(
        world,
        "workflow",
        world.workflow.id,
        correlation_id="workflow-confirmation",
        inputs={"case_id": "case-1"},
        mode="confirm",
        idempotency_key="workflow-key",
        confirmation=dict(preview.confirmation),
    )
    confirmed = _invoke(db, world, confirm_request)
    replay = _invoke(db, world, confirm_request)
    run = db.get(WorkflowRun, confirmed.output["workflow_run_id"])

    assert confirmed.status == "succeeded"
    assert replay.audit_ref["replayed"] is True
    assert run.status == "queued"
    assert run.dedupe_key.startswith("dev:cap:")
    assert run.dedupe_key.endswith(":workflow-key")
    assert run.definition_hash == world.deployment.definition_hash
    assert executed == []
    assert db.scalar(select(func.count(WorkflowRun.id))) == 1


def test_downstream_idempotency_is_scoped_to_deployment_and_principal(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(db, "idempotency-scope")
    dispatches: list[dict] = []
    monkeypatch.setattr(
        workflow_service,
        "_dispatch_executor",
        lambda _db, _action, params, **_kwargs: (
            dispatches.append(dict(params)) or {"accepted": True},
            [],
        ),
    )

    def confirmed_action(target: World, correlation_id: str) -> None:
        preview = _invoke(
            db,
            target,
            _request(
                target,
                "action",
                target.action.id,
                correlation_id=correlation_id,
                inputs={"value": 8},
                mode="preview",
                idempotency_key="shared-caller-key",
            ),
        )
        receipt = _invoke(
            db,
            target,
            _request(
                target,
                "action",
                target.action.id,
                correlation_id=correlation_id,
                inputs={"value": 8},
                mode="confirm",
                idempotency_key="shared-caller-key",
                confirmation=dict(preview.confirmation),
            ),
        )
        assert receipt.status == "succeeded"

    confirmed_action(world, "definition-a")
    definition_b = replace(
        world.deployment.definition,
        definition_hash="e" * 64,
    )
    world_b = replace(
        world,
        deployment=build_resolved_deployment(definition_b),
    )
    confirmed_action(world_b, "definition-b")

    keys = db.scalars(
        select(ActionExecutionLog.idempotency_key)
        .where(ActionExecutionLog.mode == "execute")
        .order_by(ActionExecutionLog.definition_hash)
    ).all()
    assert len(keys) == 2
    assert len(set(keys)) == 2
    assert all(key.endswith(":shared-caller-key") for key in keys)
    assert dispatches == [{"value": 8}, {"value": 8}]


def test_action_provider_does_not_persist_raw_structured_inputs(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(db, "input-audit")
    frozen_action = world.deployment.definition.actions[world.action.id]
    frozen_action.input_schema = {
        "type": "object",
        "properties": {"private_note": {"type": "string"}},
        "required": ["private_note"],
        "additionalProperties": False,
    }
    raw_value = "do-not-persist-this-value"
    monkeypatch.setattr(
        workflow_service,
        "_dispatch_executor",
        lambda *_args, **_kwargs: ({"accepted": True}, []),
    )
    preview = _invoke(
        db,
        world,
        _request(
            world,
            "action",
            world.action.id,
            correlation_id="input-audit",
            inputs={"private_note": raw_value},
            mode="preview",
            idempotency_key="input-audit-key",
        ),
    )
    _invoke(
        db,
        world,
        _request(
            world,
            "action",
            world.action.id,
            correlation_id="input-audit",
            inputs={"private_note": raw_value},
            mode="confirm",
            idempotency_key="input-audit-key",
            confirmation=dict(preview.confirmation),
        ),
    )

    logs = db.scalars(
        select(ActionExecutionLog).where(
            ActionExecutionLog.target_id == world.action.id
        )
    ).all()
    serialized = json.dumps(
        [
            {"input_params": log.input_params, "result": log.result}
            for log in logs
        ],
        sort_keys=True,
    )
    assert raw_value not in serialized
    assert len(logs) == 2
    assert all(log.input_params.get("input_hash") for log in logs)


def test_provider_permissions_fail_closed_and_legacy_preview_still_commits(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(db, "permission")
    denied = PermissionDecision(False, "denied", "viewer")
    monkeypatch.setattr(permission_service, "check_workflow", lambda *_args: denied)
    denied_receipt = _invoke(
        db,
        world,
        _request(
            world,
            "workflow",
            world.workflow.id,
            correlation_id="denied-preview",
            inputs={"case_id": "case-2"},
            mode="preview",
            idempotency_key="denied-key",
        ),
    )
    assert denied_receipt.status == "failed"
    assert db.scalar(select(func.count(WorkflowRun.id))) == 0

    monkeypatch.undo()
    with patch.object(db, "commit", wraps=db.commit) as commit:
        workflow_service.preview_action(
            db,
            world.deployment.definition.actions[world.action.id],
            {"value": 4},
            runtime_environment="dev",
            runtime_definition=world.deployment.definition,
        )
    commit.assert_called_once()


def test_action_confirmation_recovers_success_committed_before_invoker_finalize(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(db, "recover-success")
    dispatches: list[dict] = []
    monkeypatch.setattr(
        workflow_service,
        "_dispatch_executor",
        lambda _db, _action, params, **_kwargs: (
            dispatches.append(dict(params)) or {"accepted": True},
            [],
        ),
    )
    preview = _invoke(
        db,
        world,
        _request(
            world,
            "action",
            world.action.id,
            correlation_id="recover-success",
            inputs={"value": 11},
            mode="preview",
            idempotency_key="recover-success-key",
        ),
    )
    confirm_request = _request(
        world,
        "action",
        world.action.id,
        correlation_id="recover-success",
        inputs={"value": 11},
        mode="confirm",
        idempotency_key="recover-success-key",
        confirmation=dict(preview.confirmation),
    )
    execute_action = workflow_service.execute_action

    def commit_then_stop(*args, **kwargs):
        execute_action(*args, **kwargs)
        raise SystemExit("simulated process stop after Action commit")

    monkeypatch.setattr(workflow_service, "execute_action", commit_then_stop)
    with pytest.raises(SystemExit):
        _invoke(db, world, confirm_request)

    invocation = db.get(CapabilityInvocation, preview.invocation_id)
    child = db.scalar(
        select(ActionExecutionLog).where(ActionExecutionLog.mode == "execute")
    )
    assert invocation.status == "running"
    assert child.status == "success"

    monkeypatch.setattr(workflow_service, "execute_action", execute_action)
    recovered = _invoke(db, world, confirm_request)

    assert recovered.status == "succeeded"
    assert recovered.output["action_execution_log_id"] == child.id
    assert recovered.audit_ref["replayed"] is True
    assert dispatches == [{"value": 11}]
    assert db.scalar(select(func.count(ActionExecutionLog.id)).where(
        ActionExecutionLog.mode == "execute"
    )) == 1


def test_action_confirmation_running_outcome_is_indeterminate_without_redispatch(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(db, "recover-running")
    dispatches = 0

    def stop_after_claim(*_args, **_kwargs):
        nonlocal dispatches
        dispatches += 1
        raise SystemExit("simulated process stop after durable claim")

    monkeypatch.setattr(workflow_service, "_dispatch_executor", stop_after_claim)
    preview = _invoke(
        db,
        world,
        _request(
            world,
            "action",
            world.action.id,
            correlation_id="recover-running",
            inputs={"value": 12},
            mode="preview",
            idempotency_key="recover-running-key",
        ),
    )
    confirm_request = _request(
        world,
        "action",
        world.action.id,
        correlation_id="recover-running",
        inputs={"value": 12},
        mode="confirm",
        idempotency_key="recover-running-key",
        confirmation=dict(preview.confirmation),
    )
    with pytest.raises(SystemExit):
        _invoke(db, world, confirm_request)

    first = _invoke(db, world, confirm_request)
    second = _invoke(db, world, confirm_request)
    child = db.scalar(
        select(ActionExecutionLog).where(ActionExecutionLog.mode == "execute")
    )

    assert first.status == second.status == "running"
    assert first.error_code == "execution_outcome_indeterminate"
    assert first.audit_ref["replayed"] is True
    assert child.status == "running"
    assert dispatches == 1


def test_http_capability_propagates_final_action_execution_key(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(db, "http-key")
    action = world.deployment.definition.actions[world.action.id]
    action.executor_type = "http"
    action.executor_config = {
        "idempotency_mode": "header",
        "method": "POST",
        "url": "https://example.test/capability",
    }
    observed: list[str | None] = []
    real_exec_http = workflow_service._exec_http

    def fake_http(_cfg, _params, *, execution_key=None, require_idempotency=False):
        assert require_idempotency is True
        observed.append(execution_key)
        return {"accepted": True}

    monkeypatch.setattr(workflow_service, "_exec_http", fake_http)
    preview = _invoke(
        db,
        world,
        _request(
            world,
            "action",
            world.action.id,
            correlation_id="http-key",
            inputs={"value": 13},
            mode="preview",
            idempotency_key="http-caller-key",
        ),
    )
    receipt = _invoke(
        db,
        world,
        _request(
            world,
            "action",
            world.action.id,
            correlation_id="http-key",
            inputs={"value": 13},
            mode="confirm",
            idempotency_key="http-caller-key",
            confirmation=dict(preview.confirmation),
        ),
    )
    execution = db.get(ActionExecutionLog, receipt.output["action_execution_log_id"])
    assert observed == [execution.idempotency_key]

    captured_headers: dict[str, str] = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"ok": true}'

    class Opener:
        def open(self, request, timeout):
            assert timeout == 30
            captured_headers.update(dict(request.header_items()))
            return Response()

    monkeypatch.setattr(workflow_service, "_assert_public_http_target", lambda _url: None)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_args: Opener())
    real_exec_http(
        action.executor_config,
        {"body": {"value": 13}},
        execution_key=execution.idempotency_key,
        require_idempotency=True,
    )
    assert next(
        value
        for key, value in captured_headers.items()
        if key.lower() == "idempotency-key"
    ) == execution.idempotency_key


def test_mcp_and_skill_execution_key_carriers_fail_closed_without_attestation(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(db, "executor-carriers")
    execution_key = "dev:cap:server-derived-key"
    mcp_calls: list[str | None] = []
    mcp = SimpleNamespace(id="mcp-managed")
    monkeypatch.setattr(
        mcp_service,
        "call_tool",
        lambda _mcp, _name, _params, *, execution_key=None: (
            mcp_calls.append(execution_key) or {"status": "success"}
        ),
    )
    workflow_service._exec_mcp(
        db,
        {"tool_name": "process", "idempotency_mode": "mcp_meta"},
        {"value": 1},
        mcp=mcp,
        execution_key=execution_key,
        require_idempotency=True,
    )
    with pytest.raises(PolicyViolation):
        workflow_service._exec_mcp(
            db,
            {"tool_name": "process"},
            {"value": 1},
            mcp=mcp,
            execution_key=execution_key,
            require_idempotency=True,
        )
    assert mcp_calls == [execution_key]

    skill = db.get(Skill, world.action.executor_config["skill_id"])
    skill_calls: list[str | None] = []
    real_execute_skill = skill_service.execute_skill
    monkeypatch.setattr(
        skill_service,
        "execute_skill",
        lambda _skill, _args, timeout, *, execution_key=None: (
            skill_calls.append(execution_key)
            or {"status": "success", "stdout": "", "stderr": "", "exit_code": 0}
        ),
    )
    workflow_service._exec_skill(
        db,
        {"skill_id": skill.id},
        {"args": []},
        execution_key=execution_key,
        require_idempotency=True,
    )
    skill.meta = {}
    with pytest.raises(PolicyViolation):
        workflow_service._exec_skill(
            db,
            {"skill_id": skill.id},
            {"args": []},
            execution_key=execution_key,
            require_idempotency=True,
        )
    assert skill_calls == [execution_key]

    captured_environment: dict[str, str] = {}
    monkeypatch.setattr(skill_service, "_find_entry", lambda _path: "entry.py")

    def fake_run(*_args, **kwargs):
        captured_environment.update(kwargs["env"])
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(skill_service.subprocess, "run", fake_run)
    real_execute_skill(skill, [], execution_key=execution_key)
    assert captured_environment["CAPABILITY_EXECUTION_KEY"] == execution_key


def test_mcp_protocol_metadata_carries_server_execution_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp
    from mcp.client import stdio as stdio_module

    captured: dict[str, object] = {}

    class Session:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def initialize(self):
            return None

        async def call_tool(self, _name, _arguments, *, meta=None):
            captured["meta"] = meta
            return SimpleNamespace(content=[], isError=False)

    @asynccontextmanager
    async def stdio_client(_params):
        yield "read", "write"

    monkeypatch.setattr(mcp, "ClientSession", Session)
    monkeypatch.setattr(stdio_module, "stdio_client", stdio_client)
    monkeypatch.setattr(
        mcp_service,
        "get_settings",
        lambda: SimpleNamespace(allow_mcp_stdio=True),
    )
    execution_key = "dev:cap:mcp-protocol-key"
    result = asyncio.run(
        mcp_service._call_tool_async(
            SimpleNamespace(
                transport="stdio",
                command="managed-tool",
                args=[],
                env={},
            ),
            "process",
            {"value": 1},
            execution_key=execution_key,
        )
    )
    assert result["status"] == "success"
    assert captured["meta"] == {
        "com.ontology-platform/capability-execution-key": execution_key
    }


def test_capability_action_fails_before_unattested_external_executor(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _world(db, "unattested-executor")
    skill = db.get(Skill, world.action.executor_config["skill_id"])
    skill.meta = {}
    external_calls: list[bool] = []
    monkeypatch.setattr(
        skill_service,
        "execute_skill",
        lambda *_args, **_kwargs: external_calls.append(True),
    )
    preview = _invoke(
        db,
        world,
        _request(
            world,
            "action",
            world.action.id,
            correlation_id="unattested-executor",
            inputs={"value": 15},
            mode="preview",
            idempotency_key="unattested-key",
        ),
    )
    failed = _invoke(
        db,
        world,
        _request(
            world,
            "action",
            world.action.id,
            correlation_id="unattested-executor",
            inputs={"value": 15},
            mode="confirm",
            idempotency_key="unattested-key",
            confirmation=dict(preview.confirmation),
        ),
    )
    assert failed.status == "failed"
    assert external_calls == []
    assert db.scalar(
        select(func.count(ActionExecutionLog.id)).where(
            ActionExecutionLog.mode == "execute"
        )
    ) == 0

    action = world.deployment.definition.actions[world.action.id]
    action.executor_type = "script"
    action.executor_config = {"script": "result = True"}
    with pytest.raises(PolicyViolation):
        workflow_service._dispatch_executor(
            db,
            action,
            {"value": 15},
            execution_key="dev:cap:server-derived-key",
            external_idempotency_required=True,
        )


def test_non_user_actor_requires_server_bound_client_identity(
    db: Session,
) -> None:
    world = _world(db, "client-identity")
    user_id = str(db.info["user_id"])
    actor = Actor(
        actor_type="external_api",
        principal_id="client-key-1",
        tenant_id=world.scenario.tenant_id,
        user_id=user_id,
        client_id="client-key-1",
    )
    request = _request(
        world,
        "action",
        world.action.id,
        correlation_id="client-identity-valid",
        inputs={"value": 14},
        mode="preview",
        idempotency_key="client-valid-key",
    )
    receipt = CapabilityInvoker().invoke(
        db,
        world.deployment,
        actor,
        request,
        invocation_source="rest",
    )
    preview_log = db.get(ActionExecutionLog, receipt.output["preview_log_id"])
    assert receipt.status == "awaiting_confirmation"
    assert preview_log.actor_type == "external_api"
    assert preview_log.permission_decision["capability_principal"]["type"] == (
        "external_api"
    )

    invalid_actor = Actor(
        actor_type="external_api",
        principal_id="client-key-2",
        tenant_id=world.scenario.tenant_id,
        user_id=user_id,
        client_id="different-client-key",
    )
    failed = CapabilityInvoker().invoke(
        db,
        world.deployment,
        invalid_actor,
        _request(
            world,
            "action",
            world.action.id,
            correlation_id="client-identity-invalid",
            inputs={"value": 14},
            mode="preview",
            idempotency_key="client-invalid-key",
        ),
        invocation_source="rest",
    )
    assert failed.status == "failed"
