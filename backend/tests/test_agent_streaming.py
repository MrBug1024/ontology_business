from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import AgentTurnRun, Message
from app.routers import agent_turns, agents
from app.services import agent_runtime_adapter, agent_turn_service
from app.services.agent_turn_progress_service import TurnProgress
from app.services.auth_service import get_tenant_db
from .test_agent_runtime_adapter import _world, db
from .test_agent_turn_service import _database, _payload, turn_database


def test_model_text_is_observable_before_model_finishes(db):
    _, _, _, llm, _, agent = _world(db, "stream-first")
    runtime = agent_runtime_adapter.build_runtime_context(db, agent, llm)
    exhausted = False

    def model_stream(*_args, **_kwargs):
        nonlocal exhausted
        yield {"type": "token", "content": "第一段"}
        assert not exhausted
        yield {"type": "token", "content": "第二段"}
        exhausted = True

    with patch.object(agent_runtime_adapter.llm_service, "chat_stream", model_stream):
        stream = runtime.run_agent([], "请分析")
        first = next(event for event in stream if event["type"] == "token")
        assert first["data"] == "第一段"
        assert not exhausted, "正文必须在模型生成结束之前交付"
        assert list(stream)[-1] == {"type": "done", "data": "第一段第二段"}


def test_tool_preamble_does_not_contaminate_final_answer(db):
    _, _, _, _, function, agent = _world(db, "stream-rounds")
    rounds = 0

    def model_stream(*_args, **_kwargs):
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            yield {"type": "token", "content": "先进行计算"}
            yield {"type": "tool_calls", "tool_calls": [{
                "id": "calculation", "function": {"name": "invoke_capability", "arguments": {
                    "kind": "function", "key": function.id, "inputs": {},
                }},
            }]}
        else:
            yield {"type": "token", "content": "最终结果"}

    observed = []
    with patch.object(agent_runtime_adapter.llm_service, "chat_stream", model_stream):
        result = agents.invoke_agent_once(
            agent.id, message="请计算", conversation_id=None, db=db,
            inputs={"amount": 8}, capability={"kind": "function", "key": function.id},
            on_event=observed.append,
        )
    assert result["answer"] == "最终结果"
    assert [event["data"] for event in observed if event["type"] == "token"] == ["先进行计算", "最终结果"]
    assert sum(event["type"] == "response_start" for event in observed) == 2


def _claimed(factory):
    with _database(factory) as session:
        queued = agent_turn_service.enqueue_turn(session, "turn-agent", _payload())
        lease = agent_turn_service.claim_turn(session, queued["id"])
        assert lease is not None
        assert agent_turn_service.transition_claimed_turn(
            session, queued["id"], lease=lease, status="invoking_tools",
        )
        return queued, lease


def test_partial_text_is_durable_bounded_and_contains_no_tool_arguments(turn_database):
    queued, lease = _claimed(turn_database)
    progress = TurnProgress(queued["id"], lease, turn_database, clock=lambda: 1.0)
    progress({"type": "response_start"})
    progress({"type": "token", "data": "第一段😀"})
    with _database(turn_database) as session:
        run = agent_turn_service.get_turn(session, queued["id"])
        assert run["status"] == "responding"
        assert run["result"]["answer"] == "第一段😀"
        assert not session.get(Message, queued["assistant_message_id"]).stream_finalized
        cursor = run["revision"]
    progress({"type": "token", "data": "第二段"})
    progress({"type": "tool_call", "data": {"arguments": {"token": "never-publish"}}})
    with _database(turn_database) as session:
        events = agent_turn_service.list_turn_events(session, queued["id"], after_revision=cursor)
        assert events[0]["data"]["offset"] == 5
        assert events[0]["data"]["delta"] == "第二段"
        assert "never-publish" not in json.dumps([event["data"] for event in events])
    progress({"type": "response_start"})
    progress({"type": "token", "data": "新回答"})
    with _database(turn_database) as session:
        assert agent_turn_service.get_turn(session, queued["id"])["result"]["answer"] == "新回答"


def test_cancellation_fences_buffered_text(turn_database):
    queued, lease = _claimed(turn_database)
    progress = TurnProgress(queued["id"], lease, turn_database, clock=lambda: 1.0)
    progress({"type": "token", "data": "已收到"})
    progress({"type": "token", "data": "迟到内容"})
    with _database(turn_database) as session:
        current = agent_turn_service.get_turn(session, queued["id"])
        agent_turn_service.cancel_turn(session, queued["id"], expected_revision=current["revision"])
    with pytest.raises(RuntimeError, match="租约"):
        progress.flush()
    with _database(turn_database) as session:
        assert "迟到内容" not in agent_turn_service.get_turn(session, queued["id"])["result"]["answer"]


def test_sse_replays_all_pages_before_terminal_marker(turn_database):
    queued, lease = _claimed(turn_database)
    tick = iter(range(1000))
    progress = TurnProgress(queued["id"], lease, turn_database, clock=lambda: float(next(tick)))
    for _ in range(205):
        progress({"type": "token", "data": "字"})
    with _database(turn_database) as session:
        agent_turn_service.finalize_turn(session, queued["id"], lease=lease, status="succeeded", result={"answer": "字" * 205})
        terminal_revision = session.get(AgentTurnRun, queued["id"]).revision
    app = FastAPI()
    app.include_router(agent_turns.router, prefix="/api")

    def tenant_database():
        with _database(turn_database) as session:
            yield session

    app.dependency_overrides[get_tenant_db] = tenant_database
    with TestClient(app) as client, patch.object(agent_turns, "SessionLocal", turn_database):
        response = client.get(f"/api/agent-turns/{queued['id']}/events")
    assert response.status_code == 200
    assert response.text.count('"type":"answer_delta"') == 205
    assert f"id: {terminal_revision}\n" in response.text
    assert response.text.endswith("data: [DONE]\n\n")
