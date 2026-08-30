from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    Agent,
    BusinessScenario,
    Conversation,
    FunctionDefinition,
    LLMConfig,
    Message,
    PlatformMigrationCheckpoint,
    Tenant,
    User,
)
from app.routers import agents, platform_migrations
from app.services import (
    agent_capability_service,
    agent_runtime_adapter,
    permission_service,
)
from app.services.auth_service import get_tenant_db


def test_migration_control_endpoints_commit_gates_and_rollback() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    tenant_id = "tenant-migration-api"
    user_id = "user-migration-api"
    agent_id = "agent-migration-api"
    function_id = "function-migration-api"
    with SessionLocal() as db:
        tenant = Tenant(id=tenant_id, name="Migration API tenant")
        user = User(
            id=user_id,
            tenant_id=tenant_id,
            email="migration-api@example.test",
            password_hash="test-only",
            status="active",
        )
        scenario = BusinessScenario(
            id="scenario-migration-api",
            tenant_id=tenant_id,
            name="Migration API scenario",
        )
        llm = LLMConfig(
            id="llm-migration-api",
            tenant_id=tenant_id,
            name="Validation model",
            model="test-model",
            capabilities=["chat", "tool"],
            enabled=True,
        )
        agent = Agent(
            id=agent_id,
            tenant_id=tenant_id,
            name="Validation Agent",
            scenario_id=scenario.id,
            llm_config_id=llm.id,
            capability_scope={
                **agent_capability_service.explicit_empty_scope(),
                "functions": {
                    "mode": "explicit",
                    "selected_ids": [function_id],
                },
            },
            runtime_binding_mode="legacy",
        )
        function = FunctionDefinition(
            id=function_id,
            scenario_id=scenario.id,
            name="Deterministic API score",
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
        db.add_all([tenant, user, scenario, llm, function, agent])
        db.commit()
        permission_service.ensure_organization(db, tenant_id, owner_user_id=user_id)
        db.commit()

    app = FastAPI()
    app.include_router(agents.router, prefix="/api")
    app.include_router(platform_migrations.router, prefix="/api")

    def override_db():
        db = SessionLocal()
        db.info["tenant_id"] = tenant_id
        db.info["user_id"] = user_id
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_tenant_db] = override_db
    client = TestClient(app)
    try:
        started = client.post("/api/platform/migrations/legacy-catalog/start")
        assert started.status_code == 200, started.text
        completed = client.post(
            "/api/platform/migrations/legacy-catalog/run", json={"batch_size": 10}
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "verified"

        bypass = client.put(
            f"/api/agents/{agent_id}",
            json={
                "name": "Validation Agent",
                "scenario_id": "scenario-migration-api",
                "llm_config_id": "llm-migration-api",
                "data_source_ids": [],
                "capability_scope": agent_capability_service.explicit_empty_scope(),
                "runtime_binding_mode": "shadow",
            },
        )
        assert bypass.status_code == 409, bypass.text
        assert bypass.json()["detail"]["code"] == "agent_mode_migration_required"

        shadow = client.post(
            f"/api/agents/{agent_id}/migration/mode",
            json={
                "target_mode": "shadow",
                "reason": "Begin controlled validation",
                "idempotency_key": "api-to-shadow",
            },
        )
        assert shadow.status_code == 200, shadow.text
        assert shadow.json()["runtime_binding_mode"] == "shadow"
        blocked = client.post(
            f"/api/agents/{agent_id}/migration/mode",
            json={
                "target_mode": "prefer_capability",
                "reason": "Attempt before gate",
                "idempotency_key": "api-too-early",
            },
        )
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["detail"]["code"] == "migration_gate_failed"

        raw_marker = "RAW-API-SHADOW-MARKER-DO-NOT-LEAK"
        message_ids: list[tuple[str, str]] = []
        with SessionLocal() as db:
            db.info["tenant_id"] = tenant_id
            db.info["user_id"] = user_id
            persisted_agent = db.get(Agent, agent_id)
            persisted_llm = db.get(LLMConfig, "llm-migration-api")
            for index in (1, 2):
                turn_input = agent_runtime_adapter.AgentTurnInput(
                    structured_inputs={"amount": 8},
                    target_kind="function",
                    target_key=function_id,
                )
                context = agent_runtime_adapter.build_runtime_context(
                    db,
                    persisted_agent,
                    persisted_llm,
                    turn_input=turn_input,
                )
                snapshot = agent_runtime_adapter.input_snapshot(context)
                legacy_definition_hash = snapshot["runtime"]["legacy_context"][
                    "definition_hash"
                ]
                conversation_id = f"shadow-api-conversation-{index}"
                message_id = f"shadow-api-message-{index}"
                result_id = f"shadow-api-result-{index}"
                conversation = Conversation(
                    id=conversation_id,
                    agent_id=agent_id,
                    created_by_user_id=user_id,
                )
                message = Message(
                    id=message_id,
                    conversation_id=conversation_id,
                    role="assistant",
                    content=f"Persisted response {raw_marker}",
                    tool_calls=[
                        {
                            "id": result_id,
                            "type": "function",
                            "function": {
                                "name": "run_function",
                                "arguments": json.dumps(
                                    {
                                        "function_id": function_id,
                                        "params": {"amount": 8},
                                    },
                                    sort_keys=True,
                                ),
                            },
                        }
                    ],
                    tool_results=[
                        {
                            "id": result_id,
                            "name": "run_function",
                            "result": json.dumps(
                                {
                                    "score": 6.0,
                                    "definition_hash": legacy_definition_hash,
                                }
                            ),
                            "private_marker": raw_marker,
                        }
                    ],
                    stream_finalized=True,
                    input_snapshot=snapshot,
                )
                db.add_all([conversation, message])
                message_ids.append((message_id, result_id))
            db.commit()

        validation_receipts = []
        for message_id, result_id in message_ids:
            validated = client.post(
                f"/api/agents/{agent_id}/migration/shadow/validate",
                json={
                    "source_message_id": message_id,
                    "legacy_tool_result_id": result_id,
                    "capability_kind": "function",
                    "capability_key": function_id,
                    "inputs": {"amount": 8},
                    "managed_inputs": [],
                },
            )
            assert validated.status_code == 200, validated.text
            validation_receipts.append(validated.json())
        assert validation_receipts[0]["gate"]["passed"] is False
        assert validation_receipts[1]["gate"]["passed"] is True
        assert raw_marker not in json.dumps(validation_receipts, sort_keys=True)

        replay = client.post(
            f"/api/agents/{agent_id}/migration/shadow/validate",
            json={
                "source_message_id": message_ids[0][0],
                "legacy_tool_result_id": message_ids[0][1],
                "capability_kind": "function",
                "capability_key": function_id,
                "inputs": {"amount": 8},
            },
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True
        assert replay.json()["capability_invocation_id"] == validation_receipts[0][
            "capability_invocation_id"
        ]

        injected_metric = client.post(
            f"/api/agents/{agent_id}/migration/shadow/validate",
            json={
                "source_message_id": message_ids[0][0],
                "legacy_tool_result_id": message_ids[0][1],
                "capability_kind": "function",
                "capability_key": function_id,
                "inputs": {"amount": 8},
                "legacy_result_hash": "a" * 64,
            },
        )
        assert injected_metric.status_code == 422, injected_metric.text
        with SessionLocal() as db:
            ledger = json.dumps(
                [row.payload for row in db.query(PlatformMigrationCheckpoint).all()],
                ensure_ascii=False,
                sort_keys=True,
            )
        assert raw_marker not in ledger
        assert "score" not in ledger

        rolled_back = client.post(
            f"/api/agents/{agent_id}/migration/rollback",
            json={
                "reason": "Return to known path",
                "idempotency_key": "api-rollback",
            },
        )
        assert rolled_back.status_code == 200, rolled_back.text
        assert rolled_back.json()["runtime_binding_mode"] == "legacy"
    finally:
        client.close()
        engine.dispose()
