from __future__ import annotations

import copy
import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Agent,
    BusinessScenario,
    CapabilityInvocation,
    Conversation,
    FunctionDefinition,
    LLMConfig,
    LogicalDataset,
    Message,
    OntologyAction,
    OntologyEntity,
    PlatformMigrationCheckpoint,
    DatasetSchema,
    DatasetVersion,
    ScenarioCapabilityPort,
    Tenant,
    User,
)
from app.services import (
    agent_capability_service,
    agent_migration_service,
    agent_runtime_adapter,
    permission_service,
)
from app.services.capability_contracts import DataBindingOverride


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine, expire_on_commit=False)
    db.info["test_engine"] = engine
    return db


def _world(db: Session, key: str):
    tenant = Tenant(id=f"tenant-{key}", name=f"Tenant {key}")
    user = User(
        id=f"user-{key}",
        tenant_id=tenant.id,
        email=f"{key}@example.test",
        password_hash="test-only",
        status="active",
    )
    scenario = BusinessScenario(
        id=f"scenario-{key}", tenant_id=tenant.id, name=f"Scenario {key}"
    )
    llm = LLMConfig(
        id=f"llm-{key}",
        tenant_id=tenant.id,
        name="Validation model",
        model="test-model",
        capabilities=["chat", "tool"],
        enabled=True,
    )
    agent = Agent(
        id=f"agent-{key}",
        tenant_id=tenant.id,
        name="Validation Agent",
        scenario_id=scenario.id,
        llm_config_id=llm.id,
        data_source_ids=[],
        capability_scope=agent_capability_service.explicit_empty_scope(),
        runtime_binding_mode="legacy",
    )
    db.add_all([tenant, user, scenario, llm, agent])
    db.commit()
    permission_service.ensure_organization(db, tenant.id, owner_user_id=user.id)
    db.commit()
    db.info["tenant_id"] = tenant.id
    db.info["user_id"] = user.id
    return tenant, user, scenario, agent


def _probe(key: str, *, matching: bool = True) -> agent_migration_service.ServerShadowProbe:
    schema_a = "a" * 64
    result_a = "c" * 64
    return agent_migration_service.ServerShadowProbe(
        observation_id=key,
        legacy_schema_hash=schema_a,
        capability_schema_hash=schema_a if matching else "b" * 64,
        legacy_result_hash=result_a,
        capability_result_hash=result_a if matching else "d" * 64,
        legacy_row_count=12,
        capability_row_count=12 if matching else 11,
    )


def _shadow_function_world(db: Session, key: str):
    tenant, user, scenario, agent = _world(db, key)
    llm = db.get(LLMConfig, agent.llm_config_id)
    function = FunctionDefinition(
        id=f"function-{key}",
        scenario_id=scenario.id,
        name="Deterministic score",
        description="Compute a governed score.",
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
    scope["functions"] = {"mode": "explicit", "selected_ids": [function.id]}
    agent.capability_scope = scope
    agent.runtime_binding_mode = "shadow"
    agent.data_source_ids = []
    db.add(function)
    db.commit()
    return tenant, user, scenario, llm, function, agent


def _shadow_message(
    db: Session,
    *,
    key: str,
    user: User,
    agent: Agent,
    llm: LLMConfig,
    capability_kind: str,
    capability_key: str,
    inputs: dict,
    legacy_result: object,
    raw_marker: str = "",
    legacy_tool_name: str = "run_function",
    legacy_arguments: dict | None = None,
    legacy_result_name: str | None = None,
    binding_overrides: tuple[DataBindingOverride, ...] = (),
) -> Message:
    turn_input = agent_runtime_adapter.AgentTurnInput(
        structured_inputs=inputs,
        binding_overrides=binding_overrides,
        target_kind=capability_kind,
        target_key=capability_key,
    )
    context = agent_runtime_adapter.build_runtime_context(
        db,
        agent,
        llm,
        turn_input=turn_input,
    )
    snapshot = agent_runtime_adapter.input_snapshot(context)
    if legacy_arguments is None:
        legacy_arguments = {
            "function_id": capability_key,
            "params": inputs,
        }
    persisted_result = legacy_result
    if legacy_tool_name == "run_function" and isinstance(legacy_result, dict):
        persisted_result = {
            **legacy_result,
            "definition_hash": snapshot["runtime"]["legacy_context"][
                "definition_hash"
            ],
        }
    conversation = Conversation(
        id=f"conversation-{key}",
        agent_id=agent.id,
        created_by_user_id=user.id,
    )
    result_id = f"legacy-result-{key}"
    message = Message(
        id=f"message-{key}",
        conversation_id=conversation.id,
        role="assistant",
        content=f"Persisted legacy answer {raw_marker}",
        tool_calls=[
            {
                "id": result_id,
                "type": "function",
                "function": {
                    "name": legacy_tool_name,
                    "arguments": json.dumps(legacy_arguments, sort_keys=True),
                },
            }
        ],
        tool_results=[
            {
                "id": result_id,
                "name": legacy_result_name or legacy_tool_name,
                "result": json.dumps(persisted_result, sort_keys=True),
                "private_marker": raw_marker,
            }
        ],
        stream_finalized=True,
        input_snapshot=snapshot,
    )
    db.add_all([conversation, message])
    db.commit()
    return message


def _close(db: Session) -> None:
    engine = db.info["test_engine"]
    db.close()
    engine.dispose()


def test_gate_refuses_then_allows_ordered_cutover_and_one_click_rollback() -> None:
    db = _db()
    try:
        _tenant, user, _scenario, llm, function, agent = _shadow_function_world(
            db, "cutover"
        )
        agent.runtime_binding_mode = "legacy"
        db.commit()
        first = agent_migration_service.change_agent_mode(
            db,
            agent.id,
            target_mode="shadow",
            reason="Begin shadow validation",
            idempotency_key="to-shadow",
        )
        db.commit()
        assert first["runtime_binding_mode"] == "shadow"
        with pytest.raises(agent_migration_service.AgentMigrationError) as blocked:
            agent_migration_service.change_agent_mode(
                db,
                agent.id,
                target_mode="prefer_capability",
                reason="Try early cutover",
                idempotency_key="blocked-cutover",
            )
        assert blocked.value.code == "migration_gate_failed"

        status = agent_migration_service.record_server_shadow_probe(
            db, agent.id, _probe("probe-1")
        )
        assert status["gate"]["passed"] is False
        status = agent_migration_service.record_server_shadow_probe(
            db, agent.id, _probe("probe-2")
        )
        assert status["gate"]["passed"] is False
        assert status["gate"]["metrics"] == {
            "total_observations": 2,
            "eligible_observations": 0,
            "evaluated_observations": 0,
            "schema_matches": 0,
            "row_matches": 0,
            "result_matches": 0,
        }

        for index in (1, 2):
            message = _shadow_message(
                db,
                key=f"cutover-{index}",
                user=user,
                agent=agent,
                llm=llm,
                capability_kind="function",
                capability_key=function.id,
                inputs={"amount": 8},
                legacy_result={"score": 6.0},
            )
            status = agent_migration_service.execute_server_shadow_validation(
                db,
                agent.id,
                source_message_id=message.id,
                legacy_tool_result_id=message.tool_results[0]["id"],
                capability_kind="function",
                capability_key=function.id,
                inputs={"amount": 8},
            )
            db.commit()
        assert status["gate"]["passed"] is True
        assert status["gate"]["metrics"]["eligible_observations"] == 2

        preferred = agent_migration_service.change_agent_mode(
            db,
            agent.id,
            target_mode="prefer_capability",
            reason="Shadow gate passed",
            idempotency_key="to-prefer",
        )
        assert preferred["runtime_binding_mode"] == "prefer_capability"
        capability = agent_migration_service.change_agent_mode(
            db,
            agent.id,
            target_mode="capability_only",
            reason="Complete controlled cutover",
            idempotency_key="to-capability",
        )
        db.commit()
        assert capability["runtime_binding_mode"] == "capability_only"

        rolled_back = agent_migration_service.rollback_agent_to_legacy(
            db,
            agent.id,
            reason="Observed downstream regression",
            idempotency_key="rollback-once",
        )
        db.commit()
        assert rolled_back["runtime_binding_mode"] == "legacy"
        assert rolled_back["events"][0]["event"] == "rollback"
        assert rolled_back["events"][0]["reason"] == "Observed downstream regression"
        assert "raw" not in json.dumps(rolled_back, sort_keys=True).lower()
    finally:
        _close(db)


def test_mode_events_and_shadow_probes_are_idempotent() -> None:
    db = _db()
    try:
        _tenant, _user, _scenario, agent = _world(db, "idempotent")
        agent_migration_service.change_agent_mode(
            db,
            agent.id,
            target_mode="shadow",
            reason="Validate",
            idempotency_key="same-transition",
        )
        db.commit()
        event_count = db.scalar(
            select(func.count()).select_from(PlatformMigrationCheckpoint)
        )
        replay = agent_migration_service.change_agent_mode(
            db,
            agent.id,
            target_mode="shadow",
            reason="Validate",
            idempotency_key="same-transition",
        )
        assert replay["runtime_binding_mode"] == "shadow"
        assert db.scalar(select(func.count()).select_from(PlatformMigrationCheckpoint)) == event_count
        agent_migration_service.record_server_shadow_probe(db, agent.id, _probe("same-probe"))
        db.commit()
        probe_count = db.scalar(select(func.count()).select_from(PlatformMigrationCheckpoint))
        agent_migration_service.record_server_shadow_probe(db, agent.id, _probe("same-probe"))
        assert db.scalar(select(func.count()).select_from(PlatformMigrationCheckpoint)) == probe_count
        with pytest.raises(agent_migration_service.AgentMigrationError) as conflict:
            agent_migration_service.record_server_shadow_probe(
                db, agent.id, _probe("same-probe", matching=False)
            )
        assert conflict.value.code == "migration_idempotency_conflict"
    finally:
        _close(db)


def test_manual_probe_cannot_become_gate_evidence_by_flipping_checkpoint_flag() -> None:
    db = _db()
    try:
        _tenant, _user, _scenario, agent = _world(db, "manual-probe")
        agent.runtime_binding_mode = "shadow"
        db.commit()
        status = agent_migration_service.record_server_shadow_probe(
            db,
            agent.id,
            _probe("manual-probe"),
        )
        db.commit()
        assert status["gate"]["metrics"]["eligible_observations"] == 0

        checkpoint = db.scalar(
            select(PlatformMigrationCheckpoint).where(
                PlatformMigrationCheckpoint.stage == "shadow_metric"
            )
        )
        payload = json.loads(json.dumps(checkpoint.payload))
        payload["gate_eligible"] = True
        payload["source"]["kind"] = "server_shadow_validation"
        checkpoint.payload = payload
        db.commit()

        gate = agent_migration_service.evaluate_migration_gate(db, agent.id)
        assert gate["passed"] is False
        assert gate["metrics"]["eligible_observations"] == 0
        assert "shadow_evidence_unverified" in {
            reason["code"] for reason in gate["reasons"]
        }
    finally:
        _close(db)


def test_server_shadow_executor_runs_real_capability_twice_and_passes_gate() -> None:
    db = _db()
    try:
        _tenant, user, _scenario, llm, function, agent = _shadow_function_world(
            db, "executor"
        )
        marker = "RAW-SHADOW-MARKER-DO-NOT-LEAK"
        messages = [
            _shadow_message(
                db,
                key=f"executor-{index}",
                user=user,
                agent=agent,
                llm=llm,
                capability_kind="function",
                capability_key=function.id,
                inputs={"amount": 8},
                legacy_result={"score": 6.0},
                raw_marker=marker,
            )
            for index in (1, 2)
        ]
        receipts = []
        for message in messages:
            receipts.append(
                agent_migration_service.execute_server_shadow_validation(
                    db,
                    agent.id,
                    source_message_id=message.id,
                    legacy_tool_result_id=message.tool_results[0]["id"],
                    capability_kind="function",
                    capability_key=function.id,
                    inputs={"amount": 8},
                )
            )
            db.commit()

        assert receipts[0]["gate"]["passed"] is False
        assert receipts[1]["gate"]["passed"] is True
        assert receipts[0]["capability_invocation_id"] != receipts[1][
            "capability_invocation_id"
        ]
        replay = agent_migration_service.execute_server_shadow_validation(
            db,
            agent.id,
            source_message_id=messages[0].id,
            legacy_tool_result_id=messages[0].tool_results[0]["id"],
            capability_kind="function",
            capability_key=function.id,
            inputs={"amount": 8},
        )
        assert replay["replayed"] is True
        assert replay["capability_invocation_id"] == receipts[0][
            "capability_invocation_id"
        ]
        assert db.scalar(select(func.count()).select_from(PlatformMigrationCheckpoint)) == 2
        serialized_response = json.dumps(
            [*receipts, replay], ensure_ascii=False, sort_keys=True
        )
        ledger = json.dumps(
            [
                row.payload
                for row in db.scalars(select(PlatformMigrationCheckpoint)).all()
            ],
            ensure_ascii=False,
            sort_keys=True,
        )
        assert marker not in serialized_response
        assert marker not in ledger
        assert "score" not in ledger
    finally:
        _close(db)


@pytest.mark.parametrize(
    ("tool_name", "result_name", "arguments", "legacy_result", "expected_code"),
    [
        (
            "legacy_runtime_tool",
            "legacy_runtime_tool",
            {},
            {"score": 6.0},
            "shadow_legacy_target_unproven",
        ),
        (
            "run_function",
            "another_tool",
            {"function_id": "placeholder", "params": {"amount": 8}},
            {"score": 6.0},
            "legacy_tool_identity_mismatch",
        ),
    ],
)
def test_server_shadow_executor_rejects_unattributable_legacy_results(
    tool_name: str,
    result_name: str,
    arguments: dict,
    legacy_result: dict,
    expected_code: str,
) -> None:
    db = _db()
    try:
        case_key = (
            "legacy-proof-alias"
            if tool_name == "legacy_runtime_tool"
            else "legacy-proof-name"
        )
        _tenant, user, _scenario, llm, function, agent = _shadow_function_world(
            db, case_key
        )
        if arguments.get("function_id") == "placeholder":
            arguments["function_id"] = function.id
        message = _shadow_message(
            db,
            key=case_key,
            user=user,
            agent=agent,
            llm=llm,
            capability_kind="function",
            capability_key=function.id,
            inputs={"amount": 8},
            legacy_result=legacy_result,
            legacy_tool_name=tool_name,
            legacy_arguments=arguments,
            legacy_result_name=result_name,
        )
        with pytest.raises(agent_migration_service.AgentMigrationError) as blocked:
            agent_migration_service.execute_server_shadow_validation(
                db,
                agent.id,
                source_message_id=message.id,
                legacy_tool_result_id=message.tool_results[0]["id"],
                capability_kind="function",
                capability_key=function.id,
                inputs={"amount": 8},
            )
        assert blocked.value.code == expected_code
        assert db.scalar(select(func.count()).select_from(CapabilityInvocation)) == 0
    finally:
        _close(db)


def test_server_shadow_executor_rejects_legacy_arguments_for_another_input() -> None:
    db = _db()
    try:
        _tenant, user, _scenario, llm, function, agent = _shadow_function_world(
            db, "legacy-input-link"
        )
        message = _shadow_message(
            db,
            key="legacy-input-link",
            user=user,
            agent=agent,
            llm=llm,
            capability_kind="function",
            capability_key=function.id,
            inputs={"amount": 8},
            legacy_result={"score": 42.0},
            legacy_arguments={
                "function_id": function.id,
                "params": {"amount": 80},
            },
        )
        with pytest.raises(agent_migration_service.AgentMigrationError) as blocked:
            agent_migration_service.execute_server_shadow_validation(
                db,
                agent.id,
                source_message_id=message.id,
                legacy_tool_result_id=message.tool_results[0]["id"],
                capability_kind="function",
                capability_key=function.id,
                inputs={"amount": 8},
            )
        assert blocked.value.code == "shadow_legacy_input_mismatch"
        assert db.scalar(select(func.count()).select_from(CapabilityInvocation)) == 0
    finally:
        _close(db)


def test_generic_function_rejects_managed_data_even_when_results_match() -> None:
    db = _db()
    try:
        tenant, user, scenario, llm, function, agent = _shadow_function_world(
            db, "managed-data-proof"
        )
        dataset = LogicalDataset(
            id="dataset-shadow-managed",
            tenant_id=tenant.id,
            key="shadow-managed",
            name="Managed records",
        )
        schema = DatasetSchema(
            id="schema-shadow-managed",
            tenant_id=tenant.id,
            dataset_id=dataset.id,
            schema_version=1,
            schema_hash="a" * 64,
            compatibility="none",
            schema_document={"type": "array"},
        )
        version = DatasetVersion(
            id="version-shadow-managed",
            tenant_id=tenant.id,
            dataset_id=dataset.id,
            schema_id=schema.id,
            version_number=1,
            status="ready",
            content_hash="b" * 64,
        )
        port = ScenarioCapabilityPort(
            id="port-shadow-managed",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            capability_kind="function",
            capability_key=function.id,
            port_key="records",
            name="Managed records",
            direction="input",
            role="invocation_input",
            media_kind="dataset",
            dataset_id=dataset.id,
            dataset_schema_id=schema.id,
            schema_document={"type": "array"},
            is_required=True,
            cardinality="one",
            binding_policy="per_invocation",
            status="active",
        )
        db.add(dataset)
        db.flush()
        db.add(schema)
        db.flush()
        db.add_all([version, port])
        db.commit()
        override = DataBindingOverride(
            port_key="records",
            binding_kind="dataset_version",
            reference_id=version.id,
            signature=version.content_hash,
        )
        message = _shadow_message(
            db,
            key="managed-data-proof",
            user=user,
            agent=agent,
            llm=llm,
            capability_kind="function",
            capability_key=function.id,
            inputs={"amount": 8},
            legacy_result={"score": 6.0},
            binding_overrides=(override,),
        )
        with pytest.raises(agent_migration_service.AgentMigrationError) as blocked:
            agent_migration_service.execute_server_shadow_validation(
                db,
                agent.id,
                source_message_id=message.id,
                legacy_tool_result_id=message.tool_results[0]["id"],
                capability_kind="function",
                capability_key=function.id,
                inputs={"amount": 8},
                managed_inputs=[
                    {
                        "port_key": "records",
                        "dataset_version_id": version.id,
                        "expected_signature": version.content_hash,
                    }
                ],
            )
        # The owned port now reaches the selected provider. The generic
        # structured-function provider rejects managed handles before a
        # comparison can incorrectly treat them as absent.
        assert blocked.value.code == "shadow_capability_failed"
        invocation = db.scalar(select(CapabilityInvocation))
        assert invocation is not None and invocation.status == "failed"
        assert invocation.error_code == "provider_execution_failed"
        assert db.scalar(select(func.count()).select_from(PlatformMigrationCheckpoint)) == 0
    finally:
        _close(db)


def test_gate_rechecks_succeeded_invocation_instead_of_trusting_checkpoint() -> None:
    db = _db()
    try:
        _tenant, user, _scenario, llm, function, agent = _shadow_function_world(
            db, "gate-recheck"
        )
        receipts = []
        for index in (1, 2):
            message = _shadow_message(
                db,
                key=f"gate-recheck-{index}",
                user=user,
                agent=agent,
                llm=llm,
                capability_kind="function",
                capability_key=function.id,
                inputs={"amount": 8},
                legacy_result={"score": 6.0},
            )
            receipts.append(
                agent_migration_service.execute_server_shadow_validation(
                    db,
                    agent.id,
                    source_message_id=message.id,
                    legacy_tool_result_id=message.tool_results[0]["id"],
                    capability_kind="function",
                    capability_key=function.id,
                    inputs={"amount": 8},
                )
            )
            db.commit()
        assert receipts[-1]["gate"]["passed"] is True

        checkpoint = db.scalar(
            select(PlatformMigrationCheckpoint)
            .where(
                PlatformMigrationCheckpoint.stage == "shadow_metric",
                PlatformMigrationCheckpoint.item_key.like(
                    f"{agent.id}:%"
                ),
            )
            .order_by(PlatformMigrationCheckpoint.completed_at.desc())
        )
        assert checkpoint is not None
        original_payload = copy.deepcopy(checkpoint.payload)
        tampered_payload = copy.deepcopy(original_payload)
        tampered_payload["source"]["data_equivalence_hash"] = "f" * 64
        checkpoint.payload = tampered_payload
        db.commit()
        tampered_gate = agent_migration_service.evaluate_migration_gate(db, agent.id)
        assert tampered_gate["passed"] is False
        assert tampered_gate["metrics"]["eligible_observations"] == 1
        assert {item["code"] for item in tampered_gate["reasons"]} >= {
            "shadow_evidence_unverified",
            "insufficient_shadow_observations",
        }

        checkpoint.payload = original_payload
        db.commit()
        assert agent_migration_service.evaluate_migration_gate(db, agent.id)[
            "passed"
        ] is True

        invocation = db.get(
            CapabilityInvocation,
            receipts[-1]["capability_invocation_id"],
        )
        shadow_correlation_id = invocation.correlation_id
        invocation.correlation_id = "agent-normal-invocation"
        db.commit()
        unrelated_gate = agent_migration_service.evaluate_migration_gate(db, agent.id)
        assert unrelated_gate["passed"] is False
        assert unrelated_gate["metrics"]["eligible_observations"] == 1

        invocation.correlation_id = shadow_correlation_id
        db.commit()
        assert agent_migration_service.evaluate_migration_gate(db, agent.id)[
            "passed"
        ] is True
        invocation.status = "failed"
        db.commit()
        gate = agent_migration_service.evaluate_migration_gate(db, agent.id)
        assert gate["passed"] is False
        assert gate["metrics"]["eligible_observations"] == 1
        assert {item["code"] for item in gate["reasons"]} >= {
            "shadow_evidence_unverified",
            "insufficient_shadow_observations",
        }
    finally:
        _close(db)


def test_server_shadow_executor_rejects_input_tampering_and_cross_scope() -> None:
    db = _db()
    try:
        tenant, user, scenario, llm, function, agent = _shadow_function_world(
            db, "executor-security"
        )
        message = _shadow_message(
            db,
            key="executor-security",
            user=user,
            agent=agent,
            llm=llm,
            capability_kind="function",
            capability_key=function.id,
            inputs={"amount": 8},
            legacy_result={"score": 6.0},
        )
        with pytest.raises(agent_migration_service.AgentMigrationError) as tampered:
            agent_migration_service.execute_server_shadow_validation(
                db,
                agent.id,
                source_message_id=message.id,
                legacy_tool_result_id=message.tool_results[0]["id"],
                capability_kind="function",
                capability_key=function.id,
                inputs={"amount": 80},
            )
        assert tampered.value.code == "shadow_input_mismatch"

        other_agent = Agent(
            id="agent-executor-security-other",
            tenant_id=tenant.id,
            name="Other validation Agent",
            scenario_id=scenario.id,
            llm_config_id=llm.id,
            data_source_ids=[],
            capability_scope=agent.capability_scope,
            runtime_binding_mode="shadow",
        )
        db.add(other_agent)
        db.commit()
        with pytest.raises(agent_migration_service.AgentMigrationError) as cross_agent:
            agent_migration_service.execute_server_shadow_validation(
                db,
                other_agent.id,
                source_message_id=message.id,
                legacy_tool_result_id=message.tool_results[0]["id"],
                capability_kind="function",
                capability_key=function.id,
                inputs={"amount": 8},
            )
        assert cross_agent.value.code == "shadow_source_not_found"

        tenant_b, user_b, _scenario_b, _agent_b = _world(db, "executor-security-b")
        db.info["tenant_id"] = tenant_b.id
        db.info["user_id"] = user_b.id
        with pytest.raises(agent_migration_service.AgentMigrationError) as cross_tenant:
            agent_migration_service.execute_server_shadow_validation(
                db,
                agent.id,
                source_message_id=message.id,
                legacy_tool_result_id=message.tool_results[0]["id"],
                capability_kind="function",
                capability_key=function.id,
                inputs={"amount": 8},
            )
        assert cross_tenant.value.code == "agent_not_found"
    finally:
        _close(db)


def test_server_shadow_executor_rejects_side_effecting_capability() -> None:
    db = _db()
    try:
        _tenant, user, scenario, agent = _world(db, "executor-side-effect")
        llm = db.get(LLMConfig, agent.llm_config_id)
        entity = OntologyEntity(
            id="entity-executor-side-effect",
            scenario_id=scenario.id,
            name="Governed object",
        )
        action = OntologyAction(
            id="action-executor-side-effect",
            scenario_id=scenario.id,
            entity_id=entity.id,
            name="Mutate governed object",
            input_schema={"type": "object"},
            executor_type="unbound",
            executor_config={},
            enabled=True,
            requires_confirmation=True,
            idempotency_required=True,
        )
        scope = agent_capability_service.explicit_empty_scope()
        scope["actions"] = {"mode": "explicit", "selected_ids": [action.id]}
        agent.capability_scope = scope
        agent.runtime_binding_mode = "shadow"
        agent.data_source_ids = []
        db.add_all([entity, action])
        db.commit()
        message = _shadow_message(
            db,
            key="executor-side-effect",
            user=user,
            agent=agent,
            llm=llm,
            capability_kind="action",
            capability_key=action.id,
            inputs={},
            legacy_result={"status": "preview"},
        )
        with pytest.raises(agent_migration_service.AgentMigrationError) as blocked:
            agent_migration_service.execute_server_shadow_validation(
                db,
                agent.id,
                source_message_id=message.id,
                legacy_tool_result_id=message.tool_results[0]["id"],
                capability_kind="action",
                capability_key=action.id,
                inputs={},
            )
        assert blocked.value.code == "shadow_side_effect_forbidden"
        assert db.scalar(select(func.count()).select_from(PlatformMigrationCheckpoint)) == 0
    finally:
        _close(db)


def test_persisted_shadow_snapshot_is_diagnostic_not_a_gate_claim() -> None:
    db = _db()
    try:
        _tenant, user, _scenario, agent = _world(db, "snapshot")
        agent.runtime_binding_mode = "shadow"
        conversation = Conversation(
            id="conversation-snapshot",
            agent_id=agent.id,
            created_by_user_id=user.id,
        )
        message = Message(
            id="message-snapshot",
            conversation_id=conversation.id,
            role="assistant",
            content="Legacy result",
            input_snapshot={
                "runtime": {
                    "configured_mode": "shadow",
                    "selected_path": "legacy",
                    "fallback": {"used": False},
                    "shadow": {
                        "comparison": {
                            "legacy_source_count": 2,
                            "capability_data_handle_count": 1,
                        }
                    },
                    "legacy_context": {"definition_hash": "a" * 64},
                    "capability_context": {
                        "definition_hash": "a" * 64,
                        "complete": True,
                    },
                }
            },
        )
        db.add_all([conversation, message])
        db.commit()
        status = agent_migration_service.refresh_shadow_observations(db, agent.id)
        db.commit()
        assert status["refreshed_observations"] == 1
        assert status["gate"]["passed"] is False
        observation = status["shadow_observations"][0]
        assert observation["schema"]["equal"] is True
        assert observation["rows"]["comparable"] is False
        assert observation["result"]["comparable"] is False
        assert observation["gate_eligible"] is False
        replay = agent_migration_service.refresh_shadow_observations(db, agent.id)
        assert replay["refreshed_observations"] == 0
    finally:
        _close(db)


def test_agent_migration_acl_and_generic_update_guard() -> None:
    db = _db()
    try:
        tenant_a, user_a, _scenario_a, agent_a = _world(db, "acl-a")
        tenant_b, user_b, _scenario_b, _agent_b = _world(db, "acl-b")
        db.info["tenant_id"] = tenant_b.id
        db.info["user_id"] = user_b.id
        with pytest.raises(agent_migration_service.AgentMigrationError) as missing:
            agent_migration_service.change_agent_mode(
                db,
                agent_a.id,
                target_mode="shadow",
                reason="Cross tenant attempt",
            )
        assert missing.value.code == "agent_not_found"
        db.info["tenant_id"] = tenant_a.id
        db.info["user_id"] = user_a.id
        with pytest.raises(agent_migration_service.AgentMigrationError) as guarded:
            agent_migration_service.assert_direct_mode_update_allowed(agent_a, "shadow")
        assert guarded.value.code == "agent_mode_migration_required"
        agent_migration_service.assert_direct_mode_update_allowed(agent_a, "legacy")
    finally:
        _close(db)
