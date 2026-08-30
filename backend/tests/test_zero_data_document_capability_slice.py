"""SLICE-002: a zero-data text/document capability through all adapters."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    Agent,
    BusinessScenario,
    CapabilityInvocation,
    DataMapping,
    DataSource,
    DatasetSchema,
    DatasetVersion,
    FunctionDefinition,
    LLMConfig,
    LogicalDataset,
    ScenarioCapabilityPort,
    Tenant,
    User,
)
from app.routers import external_capabilities
from app.services import (
    agent_capability_service,
    agent_runtime_adapter,
    capability_application_service,
    capability_invoker as capability_invoker_service,
    capability_mcp_service,
    external_api_service,
    permission_service,
)
from app.services.capability_contracts import (
    Actor,
    CapabilityRef,
    Request,
    ResolvedDeployment,
    RuntimeDataContext,
    canonical_hash,
)
from app.services.capability_invoker import CapabilityInvoker
from app.services.capability_registry import CapabilityProviderRegistry


DOCUMENT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "fragments": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
        },
        "document": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "media_type": {"type": "string", "minLength": 1},
                "text": {"type": "string", "minLength": 1},
                "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
            "required": ["name", "media_type", "text", "sha256"],
            "additionalProperties": False,
        },
    },
    "required": ["fragments", "document"],
    "additionalProperties": False,
}


class _DocumentSemanticsProvider:
    """Test-only deterministic provider with no domain-specific behavior."""

    provider_key = "test.document-semantics"
    provider_version = "1.0.0"

    def contract(
        self,
        capability: CapabilityRef,
        _deployment: ResolvedDeployment,
    ) -> Mapping[str, object]:
        if capability.kind != "function":
            raise ValueError("document semantics fixture supports functions only")
        return {
            "input_schema": DOCUMENT_INPUT_SCHEMA,
            "required_roles": [],
            "required_scopes": [],
            "side_effect": False,
            "requires_confirmation": False,
            "idempotency_required": False,
        }

    def invoke(
        self,
        request: Request,
        actor: Actor,
        deployment: ResolvedDeployment,
        data_context: RuntimeDataContext,
    ) -> Mapping[str, object]:
        if actor.tenant_id != deployment.tenant_id:
            raise PermissionError("actor is outside the resolved deployment")
        if deployment.data_ports or data_context.handles:
            raise ValueError("zero-data provider received an unexpected data binding")

        fragments = tuple(str(item) for item in request.inputs["fragments"])
        document = request.inputs["document"]
        if not isinstance(document, Mapping):
            raise ValueError("document input must be an object")
        text = str(document["text"])
        supplied_hash = str(document["sha256"])
        computed_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if supplied_hash != computed_hash:
            raise ValueError("document content hash does not match")
        return {
            "document": {
                "media_type": str(document["media_type"]),
                "sha256": computed_hash,
                "verified": True,
            },
            "fragment_count": len(fragments),
            "input_semantics_hash": canonical_hash(
                {
                    "document_sha256": computed_hash,
                    "fragments": fragments,
                },
                domain="document-semantics-fixture-v1",
            ),
        }


def _receipt_semantics(document: dict) -> dict:
    return {
        key: document[key]
        for key in (
            "status",
            "capability",
            "definition_hash",
            "deployment_fingerprint",
            "data_context_fingerprint",
            "output",
            "confirmation",
            "error",
        )
    }


def _invocation_provenance(invocation: CapabilityInvocation) -> dict:
    return {
        "scenario_id": invocation.scenario_id,
        "environment": invocation.environment,
        "release_id": invocation.release_id,
        "definition_snapshot_id": invocation.definition_snapshot_id,
        "definition_hash": invocation.definition_hash,
        "deployment_fingerprint": invocation.deployment_fingerprint,
        "data_context_fingerprint": invocation.data_context_fingerprint,
        "input_hash": invocation.input_hash,
        "output_hash": invocation.result_document["output_hash"],
    }


def test_zero_data_document_capability_works_through_agent_rest_and_mcp() -> None:
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

    tenant = Tenant(id="tenant-zero-doc", name="Zero-data document tenant")
    owner = User(
        id="owner-zero-doc",
        tenant_id=tenant.id,
        email="owner-zero-doc@example.test",
        password_hash="test-only",
        status="active",
    )
    scenario = BusinessScenario(
        id="scenario-zero-doc",
        tenant_id=tenant.id,
        name="项目需求分析",
        status="active",
    )
    function = FunctionDefinition(
        id="function-zero-doc",
        scenario_id=scenario.id,
        name="文本与文档语义处理",
        description="验证零数据能力可消费逐次提供的文本与文档。",
        input_schema=DOCUMENT_INPUT_SCHEMA,
        output_schema={
            "type": "object",
            "properties": {
                "document": {"type": "object"},
                "fragment_count": {"type": "integer"},
                "input_semantics_hash": {"type": "string"},
            },
        },
        runtime_kind="provider",
        runtime_config={
            "provider_key": _DocumentSemanticsProvider.provider_key,
            "provider_version": _DocumentSemanticsProvider.provider_version,
            "provider_config": {},
        },
    )
    llm = LLMConfig(
        id="llm-zero-doc",
        tenant_id=tenant.id,
        name="Zero-data validation model",
        model="test-model",
        capabilities=["chat", "tool"],
        enabled=True,
    )
    scope = agent_capability_service.explicit_empty_scope()
    scope["functions"] = {"mode": "explicit", "selected_ids": [function.id]}
    agent = Agent(
        id="agent-zero-doc",
        tenant_id=tenant.id,
        name="Zero-data validation Agent",
        scenario_id=scenario.id,
        llm_config_id=llm.id,
        capability_scope=scope,
        runtime_binding_mode="capability_only",
        data_source_ids=[],
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
            name="Zero-data REST acceptance",
            scopes=["capabilities:read", "capabilities:invoke"],
            expires_in_days=1,
        )
        mcp_key, mcp_token = external_api_service.issue_key(
            seed_db,
            tenant_id=tenant.id,
            user_id=owner.id,
            issued_by_user_id=owner.id,
            name="Zero-data MCP acceptance",
            scopes=["capabilities:read", "capabilities:invoke"],
            expires_in_days=1,
        )
        seed_db.commit()
    finally:
        seed_db.close()

    document_text = "Users need a reviewable scope. Constraints arrive in separate notes."
    document_hash = hashlib.sha256(document_text.encode("utf-8")).hexdigest()
    invocation_input = {
        "fragments": [
            "The first note describes the desired outcome.",
            "A later note adds an acceptance constraint.",
        ],
        "document": {
            "name": "notes.txt",
            "media_type": "text/plain",
            "text": document_text,
            "sha256": document_hash,
        },
    }

    provider = _DocumentSemanticsProvider()
    registry = CapabilityProviderRegistry()
    registry.register_instance(provider)
    registry.seal()
    shared_invoker = CapabilityInvoker(registry)

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

    try:
        with (
            patch.object(
                capability_invoker_service,
                "default_provider_registry",
                registry,
            ),
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
                        structured_inputs=invocation_input,
                        target_kind="function",
                        target_key=function.id,
                    ),
                )
                assert runtime.deployment.data_ports == ()
                assert runtime.runtime_data_context.handles == ()
                agent_catalog = runtime.public_catalog()
                assert agent_catalog[0]["data_ports"] == []
                assert agent_catalog[0]["readiness"]["ready"] is True

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

            rest_catalog_response = client.get(
                f"/api/external/v2/scenarios/{scenario.id}/capabilities",
                params={"environment": "dev"},
                headers={"X-API-Key": rest_token},
            )
            assert rest_catalog_response.status_code == 200, rest_catalog_response.text
            rest_catalog = rest_catalog_response.json()
            assert rest_catalog[0]["data_ports"] == []

            rest_response = client.post(
                (
                    f"/api/external/v2/scenarios/{scenario.id}/capabilities/"
                    f"function/{function.id}/invoke"
                ),
                headers={"X-API-Key": rest_token},
                json={"environment": "dev", "inputs": invocation_input},
            )
            assert rest_response.status_code == 200, rest_response.text
            rest_receipt = rest_response.json()

            mcp_auth = capability_mcp_service.authenticate_token(mcp_token)
            assert mcp_auth is not None
            mcp_catalog = capability_mcp_service.list_capabilities(
                mcp_auth,
                scenario_id=scenario.id,
                environment="dev",
            )
            assert mcp_catalog[0]["data_ports"] == []
            mcp_receipt = capability_mcp_service.invoke_capability(
                mcp_auth,
                scenario_id=scenario.id,
                capability_kind="function",
                capability_key=function.id,
                environment="dev",
                inputs=invocation_input,
            )

        assert invoker_factory.call_count == 3
        assert kernel_spy.call_count == 3
        assert [call.kwargs["invocation_source"] for call in kernel_spy.call_args_list] == [
            "agent",
            "rest",
            "mcp",
        ]
        expected_receipt = _receipt_semantics(agent_receipt)
        assert _receipt_semantics(rest_receipt) == expected_receipt
        assert _receipt_semantics(mcp_receipt) == expected_receipt
        assert expected_receipt["output"]["document"] == {
            "media_type": "text/plain",
            "sha256": document_hash,
            "verified": True,
        }
        assert expected_receipt["output"]["fragment_count"] == 2

        audit_db = SessionLocal()
        try:
            zero_data_models = (
                ScenarioCapabilityPort,
                DataSource,
                DataMapping,
                LogicalDataset,
                DatasetSchema,
                DatasetVersion,
            )
            for model in zero_data_models:
                assert audit_db.scalar(select(func.count()).select_from(model)) == 0

            invocations = {
                name: audit_db.get(CapabilityInvocation, receipt["invocation_id"])
                for name, receipt in {
                    "agent": agent_receipt,
                    "rest": rest_receipt,
                    "mcp": mcp_receipt,
                }.items()
            }
            assert all(invocations.values())
            expected_provenance = _invocation_provenance(invocations["agent"])
            assert _invocation_provenance(invocations["rest"]) == expected_provenance
            assert _invocation_provenance(invocations["mcp"]) == expected_provenance
            assert invocations["agent"].invocation_source == "agent"
            assert invocations["agent"].principal_id == agent.id
            assert invocations["rest"].invocation_source == "rest"
            assert invocations["rest"].principal_id == rest_key.id
            assert invocations["mcp"].invocation_source == "mcp"
            assert invocations["mcp"].principal_id == mcp_key.id

            persisted_audit = json.dumps(
                [item.request_document for item in invocations.values()],
                ensure_ascii=False,
                sort_keys=True,
            )
            assert document_text not in persisted_audit
            assert invocation_input["fragments"][0] not in persisted_audit
        finally:
            audit_db.close()
    finally:
        client.close()
        engine.dispose()
