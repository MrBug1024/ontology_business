"""Process-local wake-up channel for durable assistant capability streams.

PostgreSQL remains authoritative for compilation jobs and assistant messages.
This module only wakes active SSE subscribers as soon as a worker commits a
new checkpoint.  Subscribers always reload the owner-scoped durable snapshot,
so a missed notification, worker restart, or multi-process deployment is
repaired by the stream heartbeat without inventing browser polling state.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class CompilationSubscription:
    job_id: str
    version: int


class CompilationEventBroker:
    """Small condition-variable broker carrying versions, never business data."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._versions: dict[str, tuple[int, float]] = {}

    def subscribe(self, job_id: str) -> CompilationSubscription:
        normalized = str(job_id or "").strip()
        if not normalized:
            raise ValueError("编译任务事件订阅缺少任务 ID")
        with self._condition:
            version = self._versions.get(normalized, (0, 0.0))[0]
        return CompilationSubscription(job_id=normalized, version=version)

    def publish(self, job_id: str) -> None:
        normalized = str(job_id or "").strip()
        if not normalized:
            return
        now = time.monotonic()
        with self._condition:
            current = self._versions.get(normalized, (0, now))[0]
            self._versions[normalized] = (current + 1, now)
            # Bound stale bookkeeping without ever dropping an active wake-up.
            if len(self._versions) > 2048:
                cutoff = now - 3600
                stale = [
                    key for key, (_version, touched_at) in self._versions.items()
                    if touched_at < cutoff and key != normalized
                ]
                for key in stale[:1024]:
                    self._versions.pop(key, None)
            self._condition.notify_all()

    def wait(
        self,
        subscription: CompilationSubscription,
        *,
        timeout: float,
    ) -> CompilationSubscription:
        bounded_timeout = max(0.05, min(float(timeout), 30.0))
        deadline = time.monotonic() + bounded_timeout
        with self._condition:
            while True:
                version = self._versions.get(subscription.job_id, (0, 0.0))[0]
                if version != subscription.version:
                    return CompilationSubscription(subscription.job_id, version)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return subscription
                self._condition.wait(remaining)


compilation_event_broker = CompilationEventBroker()


def subscribe(job_id: str) -> CompilationSubscription:
    return compilation_event_broker.subscribe(job_id)


def publish(job_id: str) -> None:
    compilation_event_broker.publish(job_id)


def wait(
    subscription: CompilationSubscription,
    *,
    timeout: float = 1.0,
) -> CompilationSubscription:
    return compilation_event_broker.wait(subscription, timeout=timeout)
