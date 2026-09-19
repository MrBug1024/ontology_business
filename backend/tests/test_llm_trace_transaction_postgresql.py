"""PostgreSQL rejects oversized trace operations without poisoning business work."""
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import LLMInvocationTrace
from app.services import llm_service
from isolated_postgresql import isolated_postgresql


def test_rejected_trace_does_not_poison_callers_transaction(isolated_postgresql):
    payload = llm_service.TracePayload(tenant_id=None, llm_config_id=None, provider="test", model="test",
        capability="chat", operation="x" * 34, status="succeeded", latency_ms=1, input_tokens=1,
        output_tokens=1, total_tokens=2, estimated_cost=0, currency="USD", tool_count=0)
    with Session(isolated_postgresql.runtime_engine) as db:
        assert db.scalar(text("SELECT 1")) == 1
        llm_service._persist_trace(payload, db=db)
        assert db.scalar(text("SELECT 2")) == 2
        db.commit()
        valid = llm_service.TracePayload(**{**payload.__dict__, "operation": "distillation_conversation"})
        llm_service._persist_trace(valid, db=db)
        assert db.scalar(select(LLMInvocationTrace.operation)) == "distillation_conversation"
