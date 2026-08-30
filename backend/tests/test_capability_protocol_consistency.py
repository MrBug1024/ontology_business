"""Cross-adapter acceptance for the protocol-neutral capability kernel."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    Agent,
    BusinessScenario,
    CapabilityInvocation,
    FunctionDefinition,
    LLMConfig,
    Tenant,
    User,
)
from app.routers import external_capabilities
from app.services import (
    agent_capability_service,
    agent_runtime_adapter,
    capability_application_service,
    capability_mcp_service,
    external_api_service,
    permission_service,
)
from app.services.capability_invoker import CapabilityInvoker


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_AND_KERNEL_MODULES = (
    BACKEND_ROOT / "app" / "agent_mcp_server.py",
    BACKEND_ROOT / "app" / "routers" / "external_capabilities.py",
    BACKEND_ROOT / "app" / "services" / "agent_runtime_adapter.py",
    BACKEND_ROOT / "app" / "services" / "capability_application_service.py",
    BACKEND_ROOT / "app" / "services" / "capability_contracts.py",
    BACKEND_ROOT / "app" / "services" / "capability_invoker.py",
    BACKEND_ROOT / "app" / "services" / "capability_mcp_service.py",
    BACKEND_ROOT / "app" / "services" / "runtime_input_service.py",
)
FORBIDDEN_SCENARIO_MARKERS = (
    "medical_audit",
    "run_medical_audit",
    "health_insurance",
    "project_manager",
    "医保",
    "诊断",
    "处方",
    "结算",
    "项目经理",
    "项目需求分析",
)


def _semantic_receipt(document: dict) -> dict:
    """Fields whose meaning must not depend on the transport adapter."""

    return {
        "status": document["status"],
        "capability": document["capability"],
        "definition_hash": document["definition_hash"],
        "deployment_fingerprint": document["deployment_fingerprint"],
        "data_context_fingerprint": document["data_context_fingerprint"],
        "output": document["output"],
        "confirmation": document["confirmation"],
        "error": document["error"],
    }


def _provenance(invocation: CapabilityInvocation) -> dict:
    return {
        "scenario_id": invocation.scenario_id,
        "environment": invocation.environment,
        "definition_hash": invocation.definition_hash,
        "deployment_fingerprint": invocation.deployment_fingerprint,
        "data_context_fingerprint": invocation.data_context_fingerprint,
        "release_id": invocation.release_id,
        "definition_snapshot_id": invocation.definition_snapshot_id,
        "input_hash": invocation.input_hash,
        "structured_input_hash": invocation.request_document["structured_inputs"]["hash"],
        "output_hash": invocation.result_document["output_hash"],
    }


def test_capability_protocol_and_kernel_have_no_business_scenario_branches() -> None:
    missing = [str(module) for module in PROTOCOL_AND_KERNEL_MODULES if not module.exists()]
    assert not missing, f"protocol/kernel modules missing from hardcoding gate: {missing}"

    for module in PROTOCOL_AND_KERNEL_MODULES:
        source = module.read_text(encoding="utf-8").casefold()
        for marker in FORBIDDEN_SCENARIO_MARKERS:
            assert marker.casefold() not in source, (
                f"{module.relative_to(BACKEND_ROOT)} contains business-specific marker "
                f"{marker!r}"
            )


def test_agent_rest_and_mcp_preserve_capability_semantics_and_audit_identity() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    Base.metadata.create_all(engine)

    tenant = Tenant(id="tenant-protocol", name="Protocol tenant")
    owner = User(
        id="owner-protocol",
        tenant_id=tenant.id,
        email="owner-protocol@example.test",
        password_hash="test-only",
        status="active",
    )
    scenario = BusinessScenario(
        id="scenario-protocol",
        tenant_id=tenant.id,
        name="Protocol-neutral scenario",
        status="active",
    )
    function = FunctionDefinition(
        id="function-protocol",
        scenario_id=scenario.id,
        name="Deterministic transform",
        description="A generic zero-data capability used for protocol acceptance.",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "number"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"score": {"type": "number"}},
        },
        runtime_kind="weighted_score",
        runtime_config={"weights": {"value": 0.5}, "bias": 2},
    )
    llm = LLMConfig(
        id="llm-protocol",
        tenant_id=tenant.id,
        name="Protocol validation model",
        model="test-model",
        capabilities=["chat", "tool"],
        enabled=True,
    )
    scope = agent_capability_service.explicit_empty_scope()
    scope["functions"] = {"mode": "explicit", "selected_ids": [function.id]}
    agent = Agent(
        id="agent-protocol",
        tenant_id=tenant.id,
        name="Protocol validation Agent",
        scenario_id=scenario.id,
        llm_config_id=llm.id,
        capability_scope=scope,
        runtime_binding_mode="capability_only",
    )

    seed_db = SessionLocal()
    try:
        seed_db.add_all([tenant, owner, scenario, function, llm, agent])
        seed_db.commit()
        permission_service.ensure_organization(
            seed_db,
            tenant.id,
            owner_user_id=owner.id,
        )
        rest_key, rest_token = external_api_service.issue_key(
            seed_db,
            tenant_id=tenant.id,
            user_id=owner.id,
            issued_by_user_id=owner.id,
            name="REST protocol acceptance",
            scopes=["capabilities:read", "capabilities:invoke"],
            expires_in_days=1,
        )
        mcp_key, mcp_token = external_api_service.issue_key(
            seed_db,
            tenant_id=tenant.id,
            user_id=owner.id,
            issued_by_user_id=owner.id,
            name="MCP protocol acceptance",
            scopes=["capabilities:read", "capabilities:invoke"],
            expires_in_days=1,
        )
        seed_db.commit()
    finally:
        seed_db.close()

    app = FastAPI()
    app.include_router(external_capabilities.router, prefix="/api")

    def override_db():
        request_db = SessionLocal()
        request_db.info["tenant_id"] = tenant.id
        request_db.info["user_id"] = owner.id
        try:
            yield request_db
        finally:
            request_db.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    shared_invoker = CapabilityInvoker()
    input_document = {"value": 6}

    try:
        with (
            patch.object(
                capability_application_service,
                "CapabilityInvoker",
                return_value=shared_invoker,
            ) as invoker_factory,
            patch.object(
                shared_invoker,
                "invoke",
                wraps=shared_invoker.invoke,
            ) as kernel_spy,
            patch.object(capability_mcp_service, "SessionLocal", SessionLocal),
        ):
            agent_db = SessionLocal()
            try:
                agent_db.info["tenant_id"] = tenant.id
                agent_db.info["user_id"] = owner.id
                runtime = agent_runtime_adapter.build_runtime_context(
                    agent_db,
                    agent_db.get(Agent, agent.id),
                    agent_db.get(LLMConfig, llm.id),
                    turn_input=agent_runtime_adapter.AgentTurnInput(
                        structured_inputs=input_document,
                        target_kind="function",
                        target_key=function.id,
                    ),
                )
                agent_db.info["action_audit_context"] = {"agent_id": agent.id}
                try:
                    agent_receipt = json.loads(
                        runtime.execute_tool(
                            "invoke_capability",
                            {"kind": "function", "key": function.id},
                        )
                    )
                finally:
                    agent_db.info.pop("action_audit_context", None)
                assert agent_receipt.get("error") is None, agent_receipt
                agent_db.commit()
            finally:
                agent_db.close()

            rest_response = client.post(
                (
                    f"/api/external/v2/scenarios/{scenario.id}/capabilities/"
                    f"function/{function.id}/invoke"
                ),
                headers={"X-API-Key": rest_token},
                json={"environment": "dev", "inputs": input_document},
            )
            assert rest_response.status_code == 200, rest_response.text
            rest_receipt = rest_response.json()

            mcp_auth = capability_mcp_service.authenticate_token(mcp_token)
            assert mcp_auth is not None
            mcp_receipt = capability_mcp_service.invoke_capability(
                mcp_auth,
                scenario_id=scenario.id,
                capability_kind="function",
                capability_key=function.id,
                environment="dev",
                inputs=input_document,
            )

        assert invoker_factory.call_count == 3
        assert kernel_spy.call_count == 3
        assert [call.kwargs["invocation_source"] for call in kernel_spy.call_args_list] == [
            "agent",
            "rest",
            "mcp",
        ]

        expected_semantics = _semantic_receipt(agent_receipt)
        assert _semantic_receipt(rest_receipt) == expected_semantics
        assert _semantic_receipt(mcp_receipt) == expected_semantics
        assert expected_semantics["output"] == {"score": 5.0}

        audit_db = SessionLocal()
        try:
            invocations = {
                name: audit_db.get(CapabilityInvocation, receipt["invocation_id"])
                for name, receipt in {
                    "agent": agent_receipt,
                    "rest": rest_receipt,
                    "mcp": mcp_receipt,
                }.items()
            }
            assert all(invocations.values())
            assert _provenance(invocations["rest"]) == _provenance(invocations["agent"])
            assert _provenance(invocations["mcp"]) == _provenance(invocations["agent"])

            agent_audit = invocations["agent"]
            assert agent_audit.invocation_source == "agent"
            assert agent_audit.principal_type == "agent"
            assert agent_audit.principal_id == agent.id
            assert agent_audit.agent_id == agent.id
            assert agent_audit.requested_by_user_id == owner.id
            assert agent_audit.correlation_id.startswith("agent:")

            rest_audit = invocations["rest"]
            assert rest_audit.invocation_source == "rest"
            assert rest_audit.principal_type == "external_api"
            assert rest_audit.principal_id == rest_key.id
            assert rest_audit.agent_id is None
            assert rest_audit.requested_by_user_id == owner.id
            assert rest_audit.correlation_id.startswith("rest:")

            mcp_audit = invocations["mcp"]
            assert mcp_audit.invocation_source == "mcp"
            assert mcp_audit.principal_type == "external_api"
            assert mcp_audit.principal_id == mcp_key.id
            assert mcp_audit.agent_id is None
            assert mcp_audit.requested_by_user_id == owner.id
            assert mcp_audit.correlation_id.startswith("mcp:")

            assert len({item.id for item in invocations.values()}) == 3
            assert rest_audit.principal_id != mcp_audit.principal_id
        finally:
            audit_db.close()
    finally:
        client.close()
        engine.dispose()
