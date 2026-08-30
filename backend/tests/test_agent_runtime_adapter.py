from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Agent,
    BusinessScenario,
    CapabilityInvocation,
    DatasetSchema,
    DatasetVersion,
    FunctionDefinition,
    LLMConfig,
    LogicalDataset,
    Message,
    RunInputBinding,
    ScenarioCapabilityPort,
    Tenant,
    User,
)
from app.services import (
    agent_capability_service,
    agent_engine,
    agent_runtime_adapter,
    permission_service,
)
from app.routers import agents as agents_router
from app.schemas import ChatRequest


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


def _world(db: Session, key: str = "generic"):
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
        status="active",
    )
    llm = LLMConfig(
        id=f"llm-{key}",
        tenant_id=tenant.id,
        name="Validation model",
        model="test-model",
        capabilities=["chat", "tool"],
        enabled=True,
    )
    function = FunctionDefinition(
        id=f"function-{key}",
        scenario_id=scenario.id,
        name="Generic score",
        description="Compute a deterministic score from structured input.",
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
    scope = agent_capability_service.explicit_empty_scope()
    scope["functions"] = {
        "mode": "explicit",
        "selected_ids": [function.id],
    }
    agent = Agent(
        id=f"agent-{key}",
        tenant_id=tenant.id,
        name="Validation Agent",
        scenario_id=scenario.id,
        llm_config_id=llm.id,
        data_source_ids=["unused-fixed-source"],
        capability_scope=scope,
        runtime_binding_mode="capability_only",
    )
    db.add_all([tenant, user, scenario, llm, function, agent])
    db.commit()
    permission_service.ensure_organization(db, tenant.id, owner_user_id=user.id)
    db.commit()
    db.info["tenant_id"] = tenant.id
    db.info["user_id"] = user.id
    return tenant, user, scenario, llm, function, agent


def _invoke(runtime: agent_runtime_adapter.CapabilityAgentRuntime, function_id: str):
    runtime.db.info["action_audit_context"] = {"agent_id": runtime.agent.id}
    try:
        raw = runtime.execute_tool(
            "invoke_capability",
            {"kind": "function", "key": function_id, "inputs": {}},
        )
    finally:
        runtime.db.info.pop("action_audit_context", None)
    document = json.loads(raw)
    assert document.get("error") is None, document
    return document


def test_zero_data_capability_agent_uses_kernel_without_legacy_context(db: Session) -> None:
    _tenant, _user, _scenario, llm, function, agent = _world(db, "zero-data")
    agent.data_source_ids = ["fixed-source-that-does-not-exist"]
    turn_input = agent_runtime_adapter.AgentTurnInput(
        structured_inputs={"amount": 8},
        target_kind="function",
        target_key=function.id,
    )

    # capability_only must not construct the fixed DataSource/DataMapping
    # context even when stale fixed ids remain on the compatibility row.
    with patch.object(
        agent_engine,
        "AgentContext",
        side_effect=AssertionError("legacy context must not be read"),
    ):
        runtime = agent_runtime_adapter.build_runtime_context(
            db,
            agent,
            llm,
            turn_input=turn_input,
        )

    assert isinstance(runtime, agent_runtime_adapter.CapabilityAgentRuntime)
    assert runtime.runtime_data_context.handles == ()
    receipt = _invoke(runtime, function.id)
    assert receipt["status"] == "succeeded"
    assert receipt["output"]["score"] == 6
    assert runtime.input_snapshot()["structured_inputs"]["outline"]["fields"] == {
        "amount": {"type": "integer"}
    }
    assert '"amount": 8' not in json.dumps(
        runtime.input_snapshot(), ensure_ascii=False, sort_keys=True
    )
    invocation = db.get(CapabilityInvocation, receipt["invocation_id"])
    assert invocation is not None
    assert invocation.invocation_source == "agent"
    assert invocation.request_document["structured_inputs"]["outline"]["fields"] == {
        "amount": {"type": "integer"}
    }


def test_same_agent_can_pin_two_data_versions_without_configuration_change(
    db: Session,
) -> None:
    tenant, user, scenario, llm, function, agent = _world(db, "changing-data")
    dataset = LogicalDataset(
        id="dataset-changing-data",
        tenant_id=tenant.id,
        key="changing-data",
        name="Current invocation records",
    )
    schema = DatasetSchema(
        id="schema-changing-data",
        tenant_id=tenant.id,
        dataset_id=dataset.id,
        schema_version=1,
        schema_hash="a" * 64,
        compatibility="none",
    )
    version_a = DatasetVersion(
        id="version-changing-data-a",
        tenant_id=tenant.id,
        dataset_id=dataset.id,
        schema_id=schema.id,
        version_number=1,
        status="ready",
        content_hash="b" * 64,
    )
    version_b = DatasetVersion(
        id="version-changing-data-b",
        tenant_id=tenant.id,
        dataset_id=dataset.id,
        schema_id=schema.id,
        version_number=2,
        status="ready",
        content_hash="c" * 64,
    )
    port = ScenarioCapabilityPort(
        id="port-changing-data",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        capability_kind="function",
        capability_key=function.id,
        port_key="records",
        name="Invocation records",
        direction="input",
        role="invocation_input",
        media_kind="dataset",
        dataset_id=dataset.id,
        dataset_schema_id=schema.id,
        schema_document={"type": "array"},
        is_required=True,
        cardinality="many",
        binding_policy="per_invocation",
        status="active",
        config={},
    )
    db.add(dataset)
    db.flush()
    db.add(schema)
    db.flush()
    db.add_all([version_a, version_b, port])
    db.commit()

    catalog = agents_router.get_agent_runtime_capabilities(agent.id, db)
    assert len(catalog) == 1
    assert set(catalog[0]) == {
        "kind",
        "key",
        "name",
        "description",
        "input_schema",
        "output_schema",
        "side_effect",
        "requires_confirmation",
        "idempotency_required",
        "data_ports",
        "readiness",
        "definition_hash",
        "deployment_fingerprint",
    }
    assert catalog[0]["data_ports"] == [
        {
            "port_key": "records",
            "name": "Invocation records",
            "description": "",
            "direction": "input",
            "role": "invocation_input",
            "media_kind": "dataset",
            "schema_document": {"type": "array"},
            "schema_signature": "a" * 64,
            "required": True,
            "cardinality": "many",
            "binding_policy": "per_invocation",
            "binding_kinds": ["dataset_head", "dataset_version"],
            "allow_override": True,
        }
    ]
    catalog_document = json.dumps(catalog, ensure_ascii=False, sort_keys=True)
    for forbidden in (
        "data_source_id",
        "connector_id",
        "connector_kind",
        "provider_key",
        "storage_backend",
        "bucket_name",
        "object_key",
    ):
        assert forbidden not in catalog_document

    original_agent_configuration = {
        "data_source_ids": list(agent.data_source_ids),
        "capability_scope": json.loads(json.dumps(agent.capability_scope)),
        "runtime_binding_mode": agent.runtime_binding_mode,
    }
    receipts: list[dict] = []
    evidence: list[list[dict]] = []
    definitions: list[str] = []
    for version in (version_a, version_b):
        payload = ChatRequest.model_validate(
            {
                "message": "Use this invocation's governed records.",
                "inputs": {"amount": 8},
                "managed_inputs": [
                    {
                        "port_key": "records",
                        "dataset_version_id": version.id,
                        "expected_signature": version.content_hash,
                    }
                ],
                "capability": {"kind": "function", "key": function.id},
            }
        )
        runtime = agent_runtime_adapter.build_runtime_context(
            db,
            agent,
            llm,
            turn_input=agents_router._agent_turn_input(payload),
        )
        assert isinstance(runtime, agent_runtime_adapter.CapabilityAgentRuntime)
        runtime.db.info["action_audit_context"] = {"agent_id": runtime.agent.id}
        try:
            receipt = json.loads(
                runtime.execute_tool(
                    "invoke_capability",
                    {"kind": "function", "key": function.id, "inputs": {}},
                )
            )
        finally:
            runtime.db.info.pop("action_audit_context", None)
        # The built-in structured function has no materializer for managed
        # handles. Receiving and rejecting this owned context proves it was not
        # silently dropped at the Agent boundary.
        assert receipt["error"]["code"] == "provider_execution_failed"
        receipts.append(receipt)
        evidence.append(runtime.evidence_snapshot())
        definitions.append(runtime.deployment.definition_hash)
        db.commit()

    assert definitions[0] == definitions[1]
    assert receipts[0]["invocation_id"] != receipts[1]["invocation_id"]
    assert (
        receipts[0]["data_context_fingerprint"]
        != receipts[1]["data_context_fingerprint"]
    )
    assert any(
        item.get("resolved_version_id") == version_a.id for item in evidence[0]
    )
    assert any(
        item.get("resolved_version_id") == version_b.id for item in evidence[1]
    )
    assert original_agent_configuration == {
        "data_source_ids": list(agent.data_source_ids),
        "capability_scope": agent.capability_scope,
        "runtime_binding_mode": agent.runtime_binding_mode,
    }
    audit_rows = db.scalars(
        select(RunInputBinding).order_by(RunInputBinding.created_at)
    ).all()
    assert [row.resolved_dataset_version_id for row in audit_rows] == [
        version_a.id,
        version_b.id,
    ]
    assert [row.invocation_id for row in audit_rows] == [
        receipt["invocation_id"] for receipt in receipts
    ]
    for row in audit_rows:
        assert row.invocation.agent_id == agent.id
        assert row.invocation.requested_by_user_id == user.id
        assert row.invocation.principal_type == "agent"
        assert row.invocation.principal_id == agent.id


def test_non_browser_turn_persists_safe_snapshot_and_evidence(db: Session) -> None:
    _tenant, _user, _scenario, _llm, function, agent = _world(db, "messages")
    calls = 0

    def fake_chat_stream(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                "type": "tool_calls",
                "tool_calls": [
                    {
                        "id": "call-generic-capability",
                        "function": {
                            "name": "invoke_capability",
                            "arguments": {
                                "kind": "function",
                                "key": function.id,
                                "inputs": {},
                            },
                        },
                    }
                ],
            }
            return
        yield {"type": "token", "content": "The governed capability completed."}

    with patch.object(agent_runtime_adapter.llm_service, "chat_stream", fake_chat_stream):
        result = agents_router.invoke_agent_once(
            agent.id,
            message="Analyze the supplied structured request.",
            conversation_id=None,
            db=db,
            inputs={"amount": 8},
            capability={"kind": "function", "key": function.id},
        )

    assert result["answer"] == "The governed capability completed."
    assert result["evidence_refs"][0]["kind"] == "capability_invocation"
    messages = db.scalars(
        select(Message)
        .where(Message.conversation_id == result["conversation_id"])
        .order_by(Message.created_at, Message.id)
    ).all()
    assert [message.role for message in messages] == ["user", "assistant"]
    for message in messages:
        assert message.input_snapshot["structured_inputs"]["outline"]["fields"] == {
            "amount": {"type": "integer"}
        }
        assert '"amount": 8' not in json.dumps(
            message.input_snapshot, ensure_ascii=False, sort_keys=True
        )
        assert message.evidence_refs == result["evidence_refs"]


def test_shadow_executes_legacy_and_records_comparison(db: Session) -> None:
    _tenant, _user, _scenario, llm, _function, agent = _world(db, "shadow")
    agent.runtime_binding_mode = "shadow"
    agent.data_source_ids = []
    db.commit()

    context = agent_runtime_adapter.build_runtime_context(db, agent, llm)
    snapshot = agent_runtime_adapter.input_snapshot(context)
    assert isinstance(context, agent_engine.AgentContext)
    assert snapshot["runtime"]["configured_mode"] == "shadow"
    assert snapshot["runtime"]["selected_path"] == "legacy"
    assert snapshot["runtime"]["shadow"]["executed_path"] == "legacy"
    assert snapshot["runtime"]["capability_context"]["resolved"] is True


def test_prefer_capability_fallback_is_explicit_and_auditable(db: Session) -> None:
    _tenant, _user, _scenario, llm, _function, agent = _world(db, "fallback")
    agent.runtime_binding_mode = "prefer_capability"
    agent.data_source_ids = []
    scope = agent_capability_service.explicit_empty_scope()
    scope["functions"] = {
        "mode": "explicit",
        "selected_ids": ["missing-function"],
    }
    agent.capability_scope = scope
    db.commit()

    context = agent_runtime_adapter.build_runtime_context(db, agent, llm)
    snapshot = agent_runtime_adapter.input_snapshot(context)
    assert isinstance(context, agent_engine.AgentContext)
    assert snapshot["runtime"]["selected_path"] == "legacy"
    assert snapshot["runtime"]["fallback"] == {
        "used": True,
        "code": "capability_context_incomplete",
        "message": "Capability context is incomplete; legacy execution was selected",
    }
    assert snapshot["runtime"]["capability_context"]["issues"][0]["code"] == (
        "selected_capability_unavailable"
    )


def test_explicit_legacy_mode_constructs_the_unchanged_context(db: Session) -> None:
    _tenant, _user, _scenario, llm, _function, agent = _world(db, "legacy")
    agent.runtime_binding_mode = "legacy"
    agent.data_source_ids = []
    db.commit()
    sentinel = object()
    with patch.object(agent_engine, "AgentContext", return_value=sentinel) as factory:
        result = agent_runtime_adapter.build_runtime_context(db, agent, llm)
    assert result is sentinel
    factory.assert_called_once_with(db, agent, llm, environment="dev")
