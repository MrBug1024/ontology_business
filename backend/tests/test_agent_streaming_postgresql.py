"""Real connection isolation and lease fencing for the public answer stream."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from time import perf_counter

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models import AgentTurnRun, Message
from app.schemas import ChatRequest
from app.services import agent_turn_service
from app.services.agent_turn_progress_service import TurnProgress
from .access_postgresql import isolated_access_database
from .test_agent_runtime_adapter import _world


@pytest.mark.skipif(os.environ.get("RUN_POSTGRESQL_INTEGRATION_TESTS") != "1", reason="requires an isolated PostgreSQL database")
def test_committed_deltas_survive_worker_loss_and_reject_stale_writer():
    with isolated_access_database() as (url, _admin):
        engine = create_engine(url)
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        try:
            with factory() as db:
                tenant, user, _, _, _, agent = _world(db, "pg-stream")
                queued = agent_turn_service.enqueue_turn(db, agent.id, ChatRequest(message="流式测试", idempotency_key="pg-stream"))
                lease = agent_turn_service.claim_turn(db, queued["id"])
                assert lease is not None
                agent_turn_service.transition_claimed_turn(db, queued["id"], lease=lease, status="invoking_tools")
            progress = TurnProgress(queued["id"], lease, factory)
            queries = []

            def count_query(*_args):
                queries.append(1)

            event.listen(engine, "before_cursor_execute", count_query)
            started = perf_counter()
            progress({"type": "token", "data": "已生成😀"})
            elapsed = perf_counter() - started
            event.remove(engine, "before_cursor_execute", count_query)
            assert len(queries) < 30, "每批正文的授权/写入查询必须有界"
            print(f"PG first delta: {len(queries)} queries, {elapsed * 1000:.1f} ms")
            with factory() as observer:
                observer.info.update(tenant_id=tenant.id, user_id=user.id)
                current = agent_turn_service.get_turn(observer, queued["id"])
                assert current["status"] == "responding"
                assert current["result"]["answer"] == "已生成😀"
                assert not observer.get(Message, queued["assistant_message_id"]).stream_finalized
                run = observer.get(AgentTurnRun, queued["id"])
                run.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                observer.commit()
                replacement = agent_turn_service.claim_turn(observer, run.id)
                assert replacement is not None
                assert replacement.generation > lease.generation
            with pytest.raises(RuntimeError, match="租约"):
                progress({"type": "response_start"})
            successor = TurnProgress(queued["id"], replacement, factory)
            successor({"type": "response_start"})
            successor({"type": "token", "data": "恢复后的答案"})
            with factory() as observer:
                observer.info.update(tenant_id=tenant.id, user_id=user.id)
                assert agent_turn_service.get_turn(observer, queued["id"])["result"]["answer"] == "恢复后的答案"
        finally:
            engine.dispose()
