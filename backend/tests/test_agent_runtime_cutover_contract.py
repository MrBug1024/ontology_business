from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.config import Settings
from app.services import agent_engine


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_historical_agent_runtime_is_not_deployment_configurable() -> None:
    assert "new_agent_runtime_binding_mode" not in Settings.model_fields
    example = (BACKEND_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "NEW_AGENT_RUNTIME_BINDING_MODE" not in example


def test_public_agent_loop_requires_capability_runtime_without_legacy_construction() -> None:
    db = SimpleNamespace(info={})
    agent = SimpleNamespace(id="agent-capability-only", scenario_id="scenario-generic")
    llm = SimpleNamespace(id="llm-generic", model="test-model")

    with patch.object(
        agent_engine,
        "AgentContext",
        side_effect=AssertionError("legacy AgentContext must not be constructed"),
    ):
        events = agent_engine.run_agent(
            db,
            agent,
            llm,
            [],
            "evaluate the current request",
            "Generic scenario",
            "",
            runtime_context=None,
        )
        with pytest.raises(agent_engine.AgentRuntimeContextError) as blocked:
            next(events)

    assert blocked.value.code == "capability_runtime_required"


def test_public_agent_loop_delegates_only_to_matching_capability_runtime() -> None:
    db = SimpleNamespace(info={})
    agent = SimpleNamespace(id="agent-capability-only", scenario_id="scenario-generic")
    llm = SimpleNamespace(id="llm-generic", model="test-model")

    class CapabilityRuntime:
        runtime_path = "capability"

        def __init__(self) -> None:
            self.db = db
            self.agent = agent

        def run_agent(self, history, user_message):
            assert history == []
            assert user_message == "evaluate the current request"
            yield {"type": "done", "data": "ok"}

    events = list(
        agent_engine.run_agent(
            db,
            agent,
            llm,
            [],
            "evaluate the current request",
            "Generic scenario",
            "",
            runtime_context=CapabilityRuntime(),
        )
    )

    assert events == [{"type": "done", "data": "ok"}]


@pytest.mark.parametrize("mismatch", ["database", "agent"])
def test_public_agent_loop_rejects_mismatched_capability_runtime(mismatch: str) -> None:
    db = SimpleNamespace(info={})
    agent = SimpleNamespace(id="agent-capability-only", scenario_id="scenario-generic")
    llm = SimpleNamespace(id="llm-generic", model="test-model")
    runtime = SimpleNamespace(
        runtime_path="capability",
        db=SimpleNamespace(info={}) if mismatch == "database" else db,
        agent=(
            SimpleNamespace(id="another-agent")
            if mismatch == "agent"
            else agent
        ),
        run_agent=lambda _history, _message: iter(()),
    )

    events = agent_engine.run_agent(
        db,
        agent,
        llm,
        [],
        "evaluate the current request",
        "Generic scenario",
        "",
        runtime_context=runtime,
    )
    with pytest.raises(agent_engine.AgentRuntimeContextError) as blocked:
        next(events)

    assert blocked.value.code == "capability_runtime_mismatch"
