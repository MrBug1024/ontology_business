"""Bounded public text deltas committed independently of model/tool I/O."""
from __future__ import annotations

from collections.abc import Callable, Mapping
import time
from typing import Any, TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AgentTurnRun
from . import agent_turn_input_service, permission_service

if TYPE_CHECKING:
    from .agent_turn_worker_service import TurnLease


TEXT_FLUSH_SECONDS = 0.1
TEXT_CHUNK_CHARACTERS = 1024
MAX_RESPONSE_CHARACTERS = 200_000
MAX_PROGRESS_EVENTS = 4096


class TurnProgress:
    """A request-local buffer; Run/Event remain the only replay authority."""

    def __init__(
        self, run_id: str, lease: TurnLease, session_factory: Callable[[], Session],
        *, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.run_id = run_id
        self.lease = lease
        self.session_factory = session_factory
        self.clock = clock
        self.pending = ""
        self.last_flush: float | None = None
        self.event_count = 0

    def __call__(self, event: Mapping[str, Any]) -> None:
        kind = event.get("type")
        if kind == "response_start":
            self.flush()
            self._write("answer_reset", "")
            self.last_flush = None
        elif kind == "token" and isinstance(event.get("data"), str):
            self.pending += event["data"]
            if (self.last_flush is None
                or self.clock() - self.last_flush >= TEXT_FLUSH_SECONDS
                or len(self.pending) >= TEXT_CHUNK_CHARACTERS):
                self.flush()
        elif kind in {"tool_call", "tool_result", "done"}:
            self.flush()
            if kind == "tool_call":
                self._write("invoking_tools", "")

    def flush(self) -> None:
        while self.pending:
            chunk = self.pending[:TEXT_CHUNK_CHARACTERS]
            self._write("answer_delta", chunk)
            self.pending = self.pending[len(chunk):]
            self.last_flush = self.clock()

    def _write(self, kind: str, text: str) -> None:
        # Import the state machine here to keep its worker wiring acyclic.
        from . import agent_turn_worker_service as worker

        if self.event_count >= MAX_PROGRESS_EVENTS:
            raise agent_turn_input_service.AgentTurnError(
                "agent_turn_output_limit", "回答超过输出上限，请缩小请求范围",
            )
        with self.session_factory() as db:
            run = db.scalar(select(AgentTurnRun).where(
                AgentTurnRun.id == self.run_id,
            ).with_for_update().execution_options(populate_existing=True))
            now = agent_turn_input_service._now()
            if (run is None or not worker._lease_matches(run, self.lease, as_of=now)
                or run.status not in {"planning", "invoking_tools", "responding"}):
                raise RuntimeError("Agent Turn 执行租约已失效")
            db.info.update(tenant_id=run.tenant_id, user_id=run.requested_by_user_id)
            permission_service.require_principal(db)
            agent_turn_input_service._require_agent(db, run.agent_id)
            result = dict(run.result_document or {})
            previous = result.get("answer", "")
            if not isinstance(previous, str):
                raise RuntimeError("Agent Turn 正文状态无效")
            status = "responding" if kind == "answer_delta" else "invoking_tools"
            data: dict[str, Any] = {"status": status, "label": worker.STATUS_LABELS[status]}
            if kind == "answer_delta":
                if len(previous) + len(text) > MAX_RESPONSE_CHARACTERS:
                    raise agent_turn_input_service.AgentTurnError(
                        "agent_turn_output_limit", "回答超过输出上限，请缩小请求范围",
                    )
                # JavaScript string offsets use UTF-16 code units, including emoji.
                data.update(delta=text, offset=len(previous.encode("utf-16-le")) // 2)
                result["answer"] = previous + text
            elif kind == "answer_reset":
                result["answer"] = ""
            run.result_document = result
            run.status = status
            run.revision += 1
            run.updated_at = now
            worker._append_event(db, run, kind, data)
            db.commit()
        self.event_count += 1
