from types import SimpleNamespace

import pytest

from app.models import BusinessScenario
from app.services import agent_readiness_service as readiness


@pytest.fixture
def target_context(monkeypatch):
    scenario = SimpleNamespace(id="scenario")
    agent = SimpleNamespace(scenario_id=scenario.id, llm_config_id="model",
                            capability_scope={}, runtime_binding_mode="capability_only")
    definition = SimpleNamespace(release_id="published", capability_ports={})
    monkeypatch.setattr(readiness.tenant_service, "get_visible",
                        lambda db, model, key: scenario if model is BusinessScenario else object())
    monkeypatch.setattr(readiness.tenant_service, "current_tenant_id", lambda db: "tenant")
    monkeypatch.setattr(readiness.permission_service, "require_scenario_permission", lambda *args: None)
    monkeypatch.setattr(readiness.agent_capability_service, "normalize_scope", lambda *a, **k: {})
    monkeypatch.setattr(readiness.agent_capability_service, "validate_scope", lambda *a, **k: None)
    monkeypatch.setattr(readiness.llm_service, "supports_capability", lambda *a: True)
    monkeypatch.setattr(readiness.runtime_definition_service, "resolve_authoring",
                        lambda *args: (_ for _ in ()).throw(ValueError("draft is invalid")))
    monkeypatch.setattr(readiness.runtime_definition_service, "resolve_active",
                        lambda *a, **k: definition)
    return agent, definition


def test_published_validation_is_independent_of_invalid_current_draft(target_context):
    agent, _ = target_context
    result = readiness.compute_agent_readiness(None, agent, release_id="published")
    assert result["validation"]["ready"]
    assert result["release"]["ready"]
    assert result["runtime"]["ready"]
    assert not readiness.compute_agent_readiness(None, agent)["validation"]["ready"]


def test_unavailable_release_never_falls_back_to_current_draft(target_context, monkeypatch):
    agent, _ = target_context
    monkeypatch.setattr(readiness.runtime_definition_service, "resolve_active",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("unavailable")))
    monkeypatch.setattr(readiness.runtime_definition_service, "resolve_authoring", lambda *a: object())
    monkeypatch.setattr(readiness.release_service, "capture_snapshot_content", lambda *a: {})
    result = readiness.compute_agent_readiness(None, agent, release_id="unavailable")
    assert not result["validation"]["ready"]
    assert not result["runtime"]["ready"]
    assert result["release"]["missing"][0]["code"] == "active_release_required"


def test_chat_admission_checks_its_already_pinned_release(monkeypatch):
    from app.routers import agents
    selected = []
    monkeypatch.setattr(readiness, "compute_agent_readiness", lambda *a, **k:
                        selected.append(k.get("release_id")) or {"validation": {"missing": []}})
    agent = SimpleNamespace(runtime_binding_mode="capability_only")
    runtime = SimpleNamespace(runtime_definition=SimpleNamespace(release_id="published"))
    assert agents._agent_readiness_missing(None, agent, runtime_context=runtime) == []
    assert selected == ["published"]


def test_runtime_catalog_discovers_the_explicit_release(monkeypatch):
    from app.routers import agents
    selected = []
    monkeypatch.setattr(agents, "_agent", lambda *a: object())
    monkeypatch.setattr(agents.agent_runtime_adapter, "CapabilityAgentRuntime", lambda *a, **k:
                        selected.append(k.get("release_id")) or
                        SimpleNamespace(public_catalog=lambda: [{"key": "published_only"}]))
    assert agents.get_agent_runtime_capabilities("agent", db=None, release_id="published") == [
        {"key": "published_only"}]
    assert selected == ["published"]


def test_agent_readiness_endpoint_forwards_selected_release(monkeypatch):
    from app.routers import agents
    monkeypatch.setattr(agents, "_agent", lambda *a: object())
    monkeypatch.setattr(agents, "_out", lambda *a, **k: k)
    assert agents.get_agent("agent", db=None, release_id="published") == {"release_id": "published"}


def test_durable_retry_preserves_original_release(monkeypatch):
    from app.models import Message
    from app.schemas import ChatRequest
    from app.services import agent_turn_service as turns
    run = SimpleNamespace(id="run", tenant_id="tenant", revision=2, status="failed",
                          agent_id="agent", user_message_id="message", conversation_id="conversation")
    child = SimpleNamespace(id="child", parent_run_id=run.id)
    captured = []
    db = SimpleNamespace(refresh=lambda *a, **k: None, scalar=lambda *a: None,
                         get=lambda model, key: SimpleNamespace(content="case") if model is Message else child,
                         commit=lambda: None)
    monkeypatch.setattr(turns, "_owned_run", lambda *a, **k: run)
    monkeypatch.setattr(turns, "_open_authenticated_request", lambda *a:
                        ChatRequest(message="case", release_id="published", idempotency_key="original"))
    monkeypatch.setattr(turns, "enqueue_turn", lambda db, agent, payload, **k:
                        captured.append(payload) or {"id": "child"})
    monkeypatch.setattr(turns, "_public_run", lambda child: {"id": child.id})
    assert turns.retry_turn(db, run.id, expected_revision=2, idempotency_key="retry") == {"id": "child"}
    assert captured[0].release_id == "published"
    assert captured[0].idempotency_key == "retry"
