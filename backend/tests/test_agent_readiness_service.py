from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import (
    Agent,
    BusinessScenario,
    FunctionDefinition,
    LLMConfig,
    ScenarioCapabilityPort,
    Tenant,
    User,
)
from app.routers import agents as agents_router
from app.services import agent_capability_service, agent_readiness_service, permission_service


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    tenant = Tenant(id="tenant-agent-ready", name="Agent readiness tenant")
    user = User(
        id="user-agent-ready",
        tenant_id=tenant.id,
        email="agent-ready@example.test",
        password_hash="test-only",
        status="active",
    )
    scenario = BusinessScenario(
        id="scenario-agent-ready",
        tenant_id=tenant.id,
        name="Text-only capability",
    )
    llm = LLMConfig(
        id="llm-agent-ready",
        tenant_id=tenant.id,
        name="Validation model",
        model="test-model",
        capabilities=["chat", "tool"],
        enabled=True,
    )
    function = FunctionDefinition(
        id="function-agent-ready",
        scenario_id=scenario.id,
        name="Analyze current input",
        input_schema={
            "type": "object",
            "properties": {"amount": {"type": "number"}},
        },
        output_schema={"type": "object"},
        runtime_kind="weighted_score",
        runtime_config={"weights": {"amount": 1}},
    )
    db.add_all([tenant, user, scenario, llm, function])
    db.commit()
    db.info["tenant_id"] = tenant.id
    db.info["user_id"] = user.id
    permission_service.ensure_organization(db, tenant.id, owner_user_id=user.id)
    db.commit()
    return engine, db, scenario, llm


def _agent(scenario: BusinessScenario, llm: LLMConfig, *, mode: str) -> Agent:
    return Agent(
        id=f"agent-{mode}",
        tenant_id=scenario.tenant_id,
        name=f"Agent {mode}",
        scenario_id=scenario.id,
        llm_config_id=llm.id,
        data_source_ids=[],
        capability_scope=agent_capability_service.explicit_empty_scope(),
        runtime_binding_mode=mode,
    )


def test_zero_data_capability_is_valid_without_entities_sources_or_mappings() -> None:
    engine, db, scenario, llm = _session()
    try:
        agent = _agent(scenario, llm, mode="capability_only")
        db.add(agent)
        db.commit()

        readiness = agent_readiness_service.compute_agent_readiness(
            db, agent, environment="dev"
        )
        assert readiness["definition_valid"] is True
        assert readiness["validation_ready"] is True
        assert readiness["release_ready"] is True
        assert readiness["runtime_ready"] is True
        assert agents_router._agent_readiness_missing(db, agent) == []

        output = agents_router._out(agent, db)
        assert output.readiness.source == "server"
        assert output.validation_ready is True
        assert output.runtime_binding_mode == "capability_only"
    finally:
        db.close()
        engine.dispose()


def test_per_invocation_port_only_blocks_runtime_axis() -> None:
    engine, db, scenario, llm = _session()
    try:
        agent = _agent(scenario, llm, mode="prefer_capability")
        port = ScenarioCapabilityPort(
            id="port-agent-ready-input",
            tenant_id=scenario.tenant_id,
            scenario_id=scenario.id,
            capability_kind="function",
            capability_key="function-agent-ready",
            port_key="request.documents",
            name="Current request documents",
            direction="input",
            role="invocation_input",
            media_kind="document",
            schema_document={"type": "array"},
            is_required=True,
            cardinality="many",
            binding_policy="per_invocation",
            status="active",
            config={},
        )
        db.add_all([agent, port])
        db.commit()

        readiness = agent_readiness_service.compute_agent_readiness(
            db, agent, environment="dev"
        )
        assert readiness["definition_valid"] is True
        assert readiness["validation_ready"] is True
        assert readiness["release_ready"] is True
        assert readiness["runtime_ready"] is False
        assert readiness["runtime"]["missing"] == [
            {
                "code": "invocation_input_required",
                "label": "调用时需提供输入：Current request documents",
                "target": "invocation-input:request.documents",
                "blocking": True,
            }
        ]
        # Opening the validation workbench remains allowed; the input is
        # supplied by that invocation rather than Agent configuration.
        assert agents_router._agent_readiness_missing(db, agent) == []
    finally:
        db.close()
        engine.dispose()


def test_missing_model_blocks_validation_but_not_definition_or_release() -> None:
    engine, db, scenario, llm = _session()
    try:
        agent = _agent(scenario, llm, mode="shadow")
        agent.llm_config_id = None
        db.add(agent)
        db.commit()
        readiness = agent_readiness_service.compute_agent_readiness(
            db, agent, environment="dev"
        )
        assert readiness["definition_valid"] is True
        assert readiness["validation_ready"] is False
        assert readiness["release_ready"] is True
        assert readiness["runtime_ready"] is True
        assert readiness["validation"]["missing"][0]["code"] == "chat_model_required"
    finally:
        db.close()
        engine.dispose()


def test_explicit_legacy_mode_keeps_fixed_data_prerequisites() -> None:
    engine, db, scenario, llm = _session()
    try:
        agent = _agent(scenario, llm, mode="legacy")
        db.add(agent)
        db.commit()
        readiness = agent_readiness_service.compute_agent_readiness(
            db, agent, environment="dev"
        )
        labels = [item["label"] for item in readiness["validation"]["missing"]]
        assert readiness["validation_ready"] is False
        assert "对象类型" in labels
        assert "数据源" in labels
        assert "数据映射" in labels
        assert "映射数据绑定" in labels
        assert agents_router._agent_readiness_missing(db, agent) == [
            "对象类型",
            "数据源",
            "数据映射",
            "映射数据绑定",
        ]
    finally:
        db.close()
        engine.dispose()
