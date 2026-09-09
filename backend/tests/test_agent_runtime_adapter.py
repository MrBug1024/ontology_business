from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Agent,
    BusinessScenario,
    CapabilityInvocation,
    Conversation,
    DatasetSchema,
    DatasetVersion,
    FunctionDefinition,
    LLMConfig,
    LogicalDataset,
    Message,
    OntologyRule,
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
    input_contract_validator,
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
    assert not hasattr(agent_engine, "AgentContext")
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
    assert "Receipt" in runtime._system_prompt()
    assert "不得把中间、待处理或不确定状态改写为最终结论" in runtime._system_prompt()
    assert "candidate_detected_pending_review" not in runtime._system_prompt()


def test_agent_presentation_preserves_markdown_stream_and_custom_prompt(db: Session, monkeypatch) -> None:
    *_, llm, _function, agent = _world(db, "presentation")
    agent.system_prompt = "Follow the caller's requested presentation format."
    runtime = agent_runtime_adapter.build_runtime_context(db, agent, llm)
    chunks = ["## Result\n\n", "**Ready**\n\n", "| Item | State |\n| --- | --- |\n| Draft | Ready |"]

    def chat_stream(_llm, messages, **_kwargs):
        prompt = messages[0]["content"]
        assert agent.system_prompt in prompt
        assert "不使用 Markdown" not in prompt
        assert "output" in prompt and "delivery.text" in prompt
        assert "调用方" in prompt and "附件" in prompt
        assert "执行失败表示未取得有效结果，不等于没有发现业务问题" in prompt
        assert "通用错误码不能证明故障根因已经查明" in prompt
        for chunk in chunks:
            yield {"type": "token", "content": chunk}

    monkeypatch.setattr(agent_runtime_adapter.llm_service, "chat_stream", chat_stream)
    events = list(runtime.run_agent([], "Show the result as Markdown."))
    assert [event["data"] for event in events if event["type"] == "token"] == chunks
    assert events[-1] == {"type": "done", "data": "".join(chunks)}


@pytest.mark.parametrize("chunks", [[], ["  ", "\n"]])
def test_empty_model_response_is_an_explicit_failure(db: Session, monkeypatch, chunks) -> None:
    *_, llm, _function, agent = _world(db, "empty-response")
    runtime = agent_runtime_adapter.build_runtime_context(db, agent, llm)
    monkeypatch.setattr(agent_runtime_adapter.llm_service, "chat_stream",
        lambda *_args, **_kwargs: iter({"type": "token", "content": chunk} for chunk in chunks))
    with pytest.raises(agent_runtime_adapter.AgentRuntimeAdapterError) as error:
        list(runtime.run_agent([], "Reply briefly."))
    assert error.value.code == "empty_model_response"
    from app.services.agent_turn_worker_service import _known_error
    assert _known_error(error.value) == ("empty_model_response", error.value.message, True)


def test_agent_turn_derives_stable_identity_per_capability_call(db: Session) -> None:
    _tenant, _user, _scenario, llm, function, agent = _world(
        db,
        "tool-idempotency",
    )
    runtime = agent_runtime_adapter.build_runtime_context(
        db,
        agent,
        llm,
        turn_input=agent_runtime_adapter.AgentTurnInput(
            idempotency_key="stable-agent-turn",
        ),
    )
    runtime.db.info["action_audit_context"] = {"agent_id": agent.id}
    try:
        first = json.loads(
            runtime.execute_tool(
                "invoke_capability",
                {
                    "kind": "function",
                    "key": function.id,
                    "inputs": {"amount": 8},
                },
            )
        )
        second = json.loads(
            runtime.execute_tool(
                "invoke_capability",
                {
                    "kind": "function",
                    "key": function.id,
                    "inputs": {"amount": 10},
                },
            )
        )
        replay = json.loads(
            runtime.execute_tool(
                "invoke_capability",
                {
                    "kind": "function",
                    "key": function.id,
                    "inputs": {"amount": 8},
                },
            )
        )
    finally:
        runtime.db.info.pop("action_audit_context", None)

    assert first["status"] == "succeeded"
    assert second["status"] == "succeeded"
    assert first["invocation_id"] != second["invocation_id"]
    assert replay["invocation_id"] == first["invocation_id"]
    invocations = db.scalars(
        select(CapabilityInvocation).where(
            CapabilityInvocation.scenario_id == runtime.scenario.id
        )
    ).all()
    assert len(invocations) == 2
    assert len({item.idempotency_key for item in invocations}) == 2
    assert all(str(item.idempotency_key).startswith("agent-tool:") for item in invocations)


def test_supplementary_attachment_does_not_block_zero_data_capability(db: Session) -> None:
    _tenant, _user, _scenario, llm, _function, agent = _world(
        db,
        "unsupported-attachment",
    )
    runtime = agent_runtime_adapter.build_runtime_context(
        db,
        agent,
        llm,
        turn_input=agent_runtime_adapter.AgentTurnInput(
            attachments=(
                agent_runtime_adapter.AgentAttachmentInput(
                    filename="records.csv",
                    dataset_version_id="version-unsupported-attachment",
                    expected_signature="a" * 64,
                ),
            ),
        ),
    )

    assert runtime.complete is True
    assert runtime.context_issues == []


def _attachment_runtime(
    *,
    attachments: tuple[agent_runtime_adapter.AgentAttachmentInput, ...],
    profiles: tuple[dict, ...],
) -> agent_runtime_adapter.CapabilityAgentRuntime:
    runtime = object.__new__(agent_runtime_adapter.CapabilityAgentRuntime)
    runtime.turn_input = agent_runtime_adapter.AgentTurnInput(attachments=attachments)
    runtime._attachment_observations_cache = tuple(
        input_contract_validator.ObservedInput(
            index=index,
            binding_kind=attachment.binding_kind,
            reference_id=attachment.reference_id,
            profile=profiles[index],
        )
        for index, attachment in enumerate(attachments)
    )
    return runtime


def _attachment_capability(
    *,
    cardinality: str = "one",
    binding_kinds: list[str] | None = None,
) -> dict:
    return {
        "data_ports": [
            {
                "port_key": "records",
                "direction": "input",
                "allow_override": True,
                "required": True,
                "cardinality": cardinality,
                "binding_kinds": binding_kinds or ["asset_version"],
                "schema_document": {
                    input_contract_validator.CONTENT_CONTRACT_KEY: {
                        "version": "tabular-content/v1",
                        "relations": [
                            {
                                "fields": [
                                    {
                                        "name": "record_id",
                                        "logical_types": ["string"],
                                        "required": True,
                                    }
                                ],
                                "minimum_data_rows": 1,
                                "allow_additional_fields": True,
                            }
                        ],
                        "allow_additional_relations": True,
                    }
                },
            }
        ]
    }


def _records_profile() -> dict:
    return {
        "category": "table",
        "tables": [
            {
                "name": "ignored-name",
                "sample_row_count": 1,
                "columns": [{"name": "record_id", "logical_type": "string"}],
            }
        ],
    }


def test_adapter_binds_matching_content_and_keeps_supplementary_attachment() -> None:
    attachments = (
        agent_runtime_adapter.AgentAttachmentInput(
            filename="notes.txt", asset_version_id="asset-notes"
        ),
        agent_runtime_adapter.AgentAttachmentInput(
            filename="renamed.csv", asset_version_id="asset-records"
        ),
    )
    runtime = _attachment_runtime(
        attachments=attachments,
        profiles=({"category": "document"}, _records_profile()),
    )

    overrides = runtime._attachment_overrides(_attachment_capability(), None)

    assert [(item.port_key, item.reference_id) for item in overrides] == [
        ("records", "asset-records")
    ]


def test_adapter_many_port_binds_each_compatible_attachment() -> None:
    attachments = (
        agent_runtime_adapter.AgentAttachmentInput(
            filename="first.csv", asset_version_id="asset-first"
        ),
        agent_runtime_adapter.AgentAttachmentInput(
            filename="second.csv", asset_version_id="asset-second"
        ),
    )
    runtime = _attachment_runtime(
        attachments=attachments,
        profiles=(_records_profile(), _records_profile()),
    )

    overrides = runtime._attachment_overrides(
        _attachment_capability(cardinality="many"), None
    )

    assert [(item.port_key, item.reference_id) for item in overrides] == [
        ("records", "asset-first"),
        ("records", "asset-second"),
    ]


def test_adapter_auto_and_explicit_mapping_accept_many_relation_bundle() -> None:
    attachment = agent_runtime_adapter.AgentAttachmentInput(
        filename="受管数据包",
        dataset_version_id="dataset-bundle",
    )
    profile = _records_profile()
    profile["tables"].append(
        {
            **profile["tables"][0],
            "name": "another-ignored-name",
        }
    )
    runtime = _attachment_runtime(
        attachments=(attachment,),
        profiles=(profile,),
    )
    capability = _attachment_capability(
        cardinality="many",
        binding_kinds=["dataset_version"],
    )

    automatic = runtime._attachment_overrides(capability, None)
    explicit = runtime._attachment_overrides(
        capability,
        [{"attachment_index": 0, "port_key": "records"}],
    )

    assert [(item.port_key, item.reference_id) for item in automatic] == [
        ("records", "dataset-bundle")
    ]
    assert explicit == automatic


def test_attachment_port_without_kind_restriction_accepts_managed_asset() -> None:
    attachment = agent_runtime_adapter.AgentAttachmentInput(
        filename="notes.txt", asset_version_id="asset-notes"
    )
    runtime = _attachment_runtime(
        attachments=(attachment,),
        profiles=({"category": "document"},),
    )
    capability = {
        "data_ports": [
            {
                "port_key": "supplement",
                "direction": "input",
                "allow_override": True,
                "required": True,
                "cardinality": "one",
                "binding_kinds": [],
                "schema_document": {},
            }
        ]
    }

    overrides = runtime._attachment_overrides(capability, None)

    assert [(item.port_key, item.reference_id) for item in overrides] == [
        ("supplement", "asset-notes")
    ]


def test_supplementary_attachment_does_not_satisfy_connector_only_port() -> None:
    attachment = agent_runtime_adapter.AgentAttachmentInput(
        filename="notes.txt", asset_version_id="asset-notes"
    )
    runtime = _attachment_runtime(
        attachments=(attachment,),
        profiles=({"category": "document"},),
    )
    capability = {
        "data_ports": [
            {
                "port_key": "warehouse",
                "direction": "input",
                "allow_override": True,
                "required": True,
                "cardinality": "one",
                "binding_kinds": ["connector_binding"],
                "schema_document": {},
            }
        ]
    }

    assert runtime._capability_accepts_attachments(capability) is True
    assert runtime._attachment_overrides(capability, None) == ()


def test_agent_catalog_and_invocation_include_selected_dynamic_rule(db: Session) -> None:
    _tenant, _user, scenario, llm, _function, agent = _world(db, "dynamic-rule")
    rule = OntologyRule(
        id="rule-dynamic-rule",
        scenario_id=scenario.id,
        name="Generic threshold rule",
        condition={"field": "amount", "op": ">", "value": 2},
        severity="warning",
        enabled=True,
    )
    scope = agent_capability_service.explicit_empty_scope()
    scope["rules"] = {
        "mode": "explicit",
        "selected_ids": [rule.id],
    }
    agent.capability_scope = scope
    db.add(rule)
    db.commit()

    runtime = agent_runtime_adapter.build_runtime_context(db, agent, llm)

    assert [
        (item["kind"], item["key"])
        for item in runtime.public_catalog()
    ] == [("rule", rule.id)]
    runtime.db.info["action_audit_context"] = {"agent_id": runtime.agent.id}
    try:
        receipt = json.loads(runtime.execute_tool(
            "invoke_capability",
            {
                "kind": "rule",
                "key": rule.id,
                "inputs": {"record": {"amount": 3}},
            },
        ))
    finally:
        runtime.db.info.pop("action_audit_context", None)

    assert receipt["status"] == "succeeded"
    assert receipt["output"]["matched"] is True
    assert receipt["output"]["side_effects_executed"] is False


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
    # Non-durable/MCP-style calls retain their existing self-commit behavior.
    db.rollback()
    db.expire_all()
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


def test_incomplete_turn_contract_is_rejected_before_model_call(db: Session) -> None:
    _tenant, _user, _scenario, _llm, _function, agent = _world(
        db,
        "incomplete-turn-contract",
    )
    runtime = Mock()
    runtime.complete = False
    runtime.context_issues = [
        {
            "code": "attachments_not_supported",
            "binding_kinds": ["dataset_version"],
            "count": 1,
        }
    ]
    runtime.build_tools.return_value = []

    with (
        patch.object(agents_router, "_authorization_context", return_value=runtime),
        patch.object(agents_router, "_agent_readiness_missing", return_value=[]),
        patch.object(
            agent_engine,
            "run_agent",
            side_effect=AssertionError("LLM must not receive an incomplete runtime"),
        ),
    ):
        with pytest.raises(agent_runtime_adapter.AgentRuntimeAdapterError) as blocked:
            agents_router.invoke_agent_once(
                agent.id,
                message="Use the uploaded table.",
                conversation_id=None,
                db=db,
            )

    assert blocked.value.code == "runtime_input_contract_unsatisfied"
    assert blocked.value.message == "上传内容未满足所选能力的基础输入契约"


def test_large_capability_receipt_is_bounded_before_model_and_message_use(
    db: Session,
) -> None:
    _tenant, _user, _scenario, llm, function, agent = _world(
        db,
        "bounded-receipt",
    )
    runtime = agent_runtime_adapter.build_runtime_context(db, agent, llm)
    sentinel = "RAW_RESULT_MUST_NOT_REACH_MODEL"
    document = {
        "invocation_id": "invocation-bounded-receipt",
        "status": "succeeded",
        "capability": {"kind": "function", "key": function.id},
        "definition_hash": "a" * 64,
        "deployment_fingerprint": "b" * 64,
        "data_context_fingerprint": "c" * 64,
        "output": {
            "rows": [
                {"physical_column": sentinel + ("x" * 9_000)},
                {"physical_column": sentinel + ("y" * 9_000)},
            ]
        },
        "audit_ref": {"invocation_id": "invocation-bounded-receipt"},
        "confirmation": None,
        "error": None,
    }

    with (
        patch.object(
            agent_runtime_adapter.capability_application_service,
            "invoke",
            return_value=object(),
        ),
        patch.object(
            agent_runtime_adapter.capability_application_service,
            "receipt_document",
            return_value=document,
        ),
    ):
        raw_result = runtime.execute_tool(
            "invoke_capability",
            {
                "kind": "function",
                "key": function.id,
                "inputs": {"amount": 8},
            },
        )

    projected = json.loads(raw_result)
    assert len(raw_result.encode("utf-8")) <= (
        agent_runtime_adapter._MAX_MODEL_RECEIPT_BYTES
    )
    assert projected["contract"] == (
        "agent-capability-receipt-model-view/v1"
    )
    assert projected["invocation_id"] == document["invocation_id"]
    assert projected["status"] == "succeeded"
    assert projected["capability"] == document["capability"]
    assert projected["result_omitted"] is True
    assert len(projected["receipt_hash"]) == 64
    assert len(projected["result_hash"]) == 64
    assert projected["result_outline"] == {
        "root_type": "object",
        "node_type_counts": {
            "array": 1,
            "object": 3,
            "string": 2,
        },
        "object_field_count": 3,
        "array_item_count": 2,
        "max_depth": 3,
    }
    assert "output" not in projected
    assert "audit_ref" not in projected
    assert "physical_column" not in raw_result
    assert sentinel not in raw_result
    # The application-service receipt passed into the adapter remains intact;
    # only its model/message view is projected.
    assert document["output"]["rows"][0]["physical_column"].startswith(sentinel)


def test_large_receipt_summary_is_bound_to_the_definition_and_reauthorized_for_replay(db: Session) -> None:
    _tenant, _user, _scenario, llm, function, agent = _world(db, "summary-replay")
    function.output_schema = {"type": "object", "properties": {
        "total": {"type": "integer"},
        "state": {"type": "string", "enum": ["pending_review"]},
        "records": {"type": "array"},
    }}
    db.commit()
    runtime = agent_runtime_adapter.build_runtime_context(db, agent, llm)
    capability = runtime.public_catalog()[0]
    document = {
        "invocation_id": "invocation-summary-replay", "status": "succeeded",
        "capability": {"kind": "function", "key": function.id},
        "definition_hash": capability["definition_hash"],
        "deployment_fingerprint": capability["deployment_fingerprint"],
        "data_context_fingerprint": "c" * 64,
        "output": {"total": 107, "state": "pending_review", "records": [{"private": "x" * 20_000}]},
        "audit_ref": {}, "confirmation": None, "error": None,
    }
    with patch.object(agent_runtime_adapter.capability_application_service, "invoke", return_value=object()), patch.object(
        agent_runtime_adapter.capability_application_service, "receipt_document", return_value=document,
    ):
        projected = json.loads(runtime.execute_tool("invoke_capability", {
            "kind": "function", "key": function.id, "inputs": {"amount": 8},
        }))
    assert projected["output_summary"] == {"state": "pending_review", "total": 107}
    assert "private" not in json.dumps(projected)
    assert len(json.dumps(projected).encode("utf-8")) <= agent_runtime_adapter._MAX_MODEL_RECEIPT_BYTES
    legacy_projection = agent_runtime_adapter._model_receipt_projection(document)
    with patch.object(agent_runtime_adapter.capability_application_service, "get_receipt", return_value=document):
        for stored in (document, projected, legacy_projection):
            assert runtime.authorize_historic_tool_result("invoke_capability", {}, stored)
            replayed = runtime.model_historic_tool_result("invoke_capability", {}, stored)
            assert json.loads(replayed) == projected
        tampered = {**projected, "output_summary": {"total": 108}}
        assert not runtime.authorize_historic_tool_result("invoke_capability", {}, tampered)
        assert runtime.model_historic_tool_result("invoke_capability", {}, tampered) is None
    assert "output_summary" not in runtime._model_receipt({**document, "definition_hash": "0" * 64})


def test_historic_large_receipt_replay_uses_the_same_bounded_projection(
    db: Session,
) -> None:
    _tenant, user, _scenario, llm, function, agent = _world(
        db,
        "bounded-history",
    )
    runtime = agent_runtime_adapter.build_runtime_context(db, agent, llm)
    sentinel = "HISTORIC_RAW_RESULT_MUST_NOT_REACH_MODEL"
    document = {
        "invocation_id": "invocation-bounded-history",
        "status": "succeeded",
        "capability": {"kind": "function", "key": function.id},
        "definition_hash": "d" * 64,
        "deployment_fingerprint": "e" * 64,
        "data_context_fingerprint": "f" * 64,
        "output": {"rows": [{"private_value": sentinel + ("z" * 12_000)}]},
        "audit_ref": {"invocation_id": "invocation-bounded-history"},
        "confirmation": None,
        "error": None,
    }
    conversation = Conversation(
        id="conversation-bounded-history",
        agent_id=agent.id,
        created_by_user_id=user.id,
        title="Bounded history",
    )
    message = Message(
        id="message-bounded-history",
        conversation_id=conversation.id,
        role="assistant",
        content="The governed capability completed.",
        tool_calls=[
            {
                "id": "call-bounded-history",
                "name": "invoke_capability",
                "arguments": {
                    "kind": "function",
                    "key": function.id,
                    "inputs": {"amount": 8},
                },
            }
        ],
        # Simulate a legacy pre-boundary row. Authorization may accept the
        # canonical record, but replay must replace it with today's projection.
        tool_results=[
            {
                "id": "call-bounded-history",
                "name": "invoke_capability",
                "result": json.dumps(document, ensure_ascii=False, sort_keys=True),
            }
        ],
    )
    db.add_all([conversation, message])
    db.commit()

    with patch.object(
        agent_runtime_adapter.capability_application_service,
        "get_receipt",
        return_value=document,
    ):
        history = agents_router._model_history(
            db,
            conversation.id,
            agent,
            runtime,
        )

    tool_messages = [item for item in history if item.get("role") == "tool"]
    assert len(tool_messages) == 1
    projected = json.loads(tool_messages[0]["content"])
    assert projected == agent_runtime_adapter._model_receipt_projection(document)
    assert len(tool_messages[0]["content"].encode("utf-8")) <= (
        agent_runtime_adapter._MAX_MODEL_RECEIPT_BYTES
    )
    assert sentinel not in json.dumps(history, ensure_ascii=False)
    assert "private_value" not in json.dumps(history, ensure_ascii=False)


def test_model_history_character_budget_keeps_tool_exchange_atomic() -> None:
    tool_group = [
        {
            "role": "assistant",
            "content": "calling",
            "tool_calls": [{"id": "call-1", "type": "function"}],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "x" * 500,
        },
    ]
    newest_group = [{"role": "user", "content": "latest"}]
    budget = len(json.dumps(newest_group, ensure_ascii=False, separators=(",", ":")))
    budget += len(
        json.dumps([tool_group[0]], ensure_ascii=False, separators=(",", ":"))
    )

    history = agents_router._bounded_model_history(
        [tool_group, newest_group],
        maximum_characters=budget,
    )

    assert history == newest_group
    assert len(
        json.dumps(history, ensure_ascii=False, separators=(",", ":"))
    ) <= budget


def test_model_history_queries_only_the_newest_persisted_records(db: Session) -> None:
    _tenant, user, _scenario, _llm, _function, agent = _world(
        db,
        "recent-history",
    )
    conversation = Conversation(
        id="conversation-recent-history",
        agent_id=agent.id,
        created_by_user_id=user.id,
        title="Recent history",
    )
    messages = [
        Message(
            id=f"recent-history-{index:02d}",
            conversation_id=conversation.id,
            role="user",
            content=f"message-{index:02d}",
        )
        for index in range(agents_router._MODEL_HISTORY_MAX_RECORDS + 6)
    ]
    db.add_all([conversation, *messages])
    db.commit()

    history = agents_router._model_history(
        db,
        conversation.id,
        agent,
        Mock(),
    )

    assert len(history) == agents_router._MODEL_HISTORY_MAX_RECORDS
    assert history[0]["content"] == "message-06"
    assert history[-1]["content"] == "message-29"


def test_capability_loop_runs_boundary_before_every_llm_round(db: Session) -> None:
    _tenant, _user, _scenario, llm, _function, agent = _world(
        db,
        "llm-boundary",
    )
    runtime = agent_runtime_adapter.build_runtime_context(db, agent, llm)
    boundary_calls: list[bool] = []
    llm_transaction_states: list[bool] = []
    model_calls = 0

    def before_llm_call() -> None:
        boundary_calls.append(db.in_transaction())
        db.commit()

    def fake_chat_stream(*_args, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        llm_transaction_states.append(db.in_transaction())
        if model_calls == 1:
            yield {
                "type": "tool_calls",
                "tool_calls": [
                    {
                        "id": "list-capabilities",
                        "function": {
                            "name": "list_available_capabilities",
                            "arguments": {},
                        },
                    }
                ],
            }
            return
        yield {"type": "token", "content": "done"}

    with patch.object(
        agent_runtime_adapter.llm_service,
        "chat_stream",
        fake_chat_stream,
    ):
        events = list(
            agent_engine.run_agent(
                db,
                agent,
                llm,
                [],
                "List the governed capabilities.",
                "Capability scenario",
                "",
                runtime_context=runtime,
                before_llm_call=before_llm_call,
            )
        )

    assert model_calls == 2
    assert len(boundary_calls) == model_calls
    assert llm_transaction_states == [False, False]
    assert events[-1] == {"type": "done", "data": "done"}


@pytest.mark.parametrize("mode", ("legacy", "shadow", "prefer_capability"))
def test_historical_modes_fail_closed_before_runtime_construction(
    db: Session,
    mode: str,
) -> None:
    _tenant, _user, _scenario, llm, _function, agent = _world(db, mode)
    agent.runtime_binding_mode = mode
    agent.data_source_ids = []
    db.commit()
    assert not hasattr(agent_engine, "AgentContext")
    with patch.object(
        agent_runtime_adapter,
        "CapabilityAgentRuntime",
        side_effect=AssertionError("capability runtime must not be probed"),
    ):
        with pytest.raises(agent_runtime_adapter.AgentRuntimeAdapterError) as blocked:
            agent_runtime_adapter.build_runtime_context(db, agent, llm)

    assert blocked.value.code == "historical_runtime_disabled"
    assert blocked.value.message == (
        "Historical Agent runtime modes are disabled; migrate the Agent to "
        "capability_only"
    )
