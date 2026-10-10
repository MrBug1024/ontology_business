"""Process-local WebSocket registry bridging sync workers and connector clients.

The investigation worker runs in a thread (``asyncio.to_thread``) while the
connector WebSocket endpoint runs on the event loop. This gateway bridges the
two with two queues per connection: an ``asyncio.Queue`` for loop-side sends
and a thread ``queue.Queue`` for worker-side receives, so no cross-thread
asyncio primitives are shared. Single-instance deployments only — the same
known boundary as the in-process browser slots.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
import uuid

from ..config import get_settings

PROTOCOL_VERSION = 1
CLOSE_REPLACED = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_UPGRADE_REQUIRED = 4402
CLOSE_REVOKED = 4403
CLOSE_IDLE = 4410


class ConnectorUnavailable(RuntimeError):
    """Constant-message failure surfaced to the investigation boundary."""


class ConnectorConnection:
    def __init__(self, session_id: str, project_id: str, target_key: str, loop: asyncio.AbstractEventLoop):
        self.session_id, self.project_id, self.target_key = session_id, project_id, target_key
        self.loop = loop
        # Loop-side: JSON strings to send, or {"close": code} to terminate.
        self.outbound: asyncio.Queue = asyncio.Queue()
        # Worker-side: parsed inbound messages, or None when disconnected.
        self.inbound: queue.Queue = queue.Queue()
        self.dead = False
        self._last_seen = time.monotonic()
        self._seen_lock = threading.Lock()

    def mark_seen(self) -> None:
        with self._seen_lock:
            self._last_seen = time.monotonic()

    def seconds_since_seen(self) -> float:
        with self._seen_lock:
            return time.monotonic() - self._last_seen

    def close_from_worker(self, code: int) -> None:
        """Thread-safe shutdown request; the endpoint performs the close."""
        self.outbound.put_nowait({"close": code})


class ConnectorGateway:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connections: dict[str, ConnectorConnection] = {}

    def register(self, session_id: str, project_id: str, target_key: str,
                 loop: asyncio.AbstractEventLoop) -> tuple[ConnectorConnection, ConnectorConnection | None]:
        """Attach a fresh connection; returns (connection, replaced predecessor)."""
        with self._lock:
            replaced = self._connections.get(session_id)
            if replaced is not None:
                replaced.dead = True
                replaced.inbound.put_nowait(None)
            connection = ConnectorConnection(session_id, project_id, target_key, loop)
            self._connections[session_id] = connection
            return connection, replaced

    def unregister(self, session_id: str, connection: ConnectorConnection) -> None:
        with self._lock:
            if self._connections.get(session_id) is connection:
                del self._connections[session_id]
        connection.dead = True
        connection.inbound.put_nowait(None)

    def get(self, session_id: str) -> ConnectorConnection | None:
        with self._lock:
            connection = self._connections.get(session_id)
            return None if connection is None or connection.dead else connection

    def disconnect(self, session_id: str, code: int) -> None:
        """Ask a live connection to close (revocation, shutdown)."""
        connection = self.get(session_id)
        if connection is not None:
            connection.close_from_worker(code)


_gateway = ConnectorGateway()


def gateway() -> ConnectorGateway:
    return _gateway


def call(session_id: str, op: str, args: dict, timeout: float | None = None) -> dict:
    """Invoke one connector op from a worker thread and await its response.

    Returns the connector's ``result`` payload. Raises ``ConnectorUnavailable``
    with constant safe messages on disconnect or timeout; connector-side
    ``BrowserAccessError`` messages arrive as regular error responses and are
    returned to the caller, which maps them at the investigation boundary.
    """
    connection = _gateway.get(session_id)
    if connection is None:
        raise ConnectorUnavailable("本机调查连接器未连接，请重新执行连接命令")
    budget = timeout or get_settings().distillation_connector_call_timeout_seconds
    request_id = uuid.uuid4().hex
    payload = json.dumps({"type": "request", "id": request_id, "op": op, "args": args}, ensure_ascii=False)
    try:
        enqueue = asyncio.run_coroutine_threadsafe(connection.outbound.put(payload), connection.loop)
        enqueue.result(timeout=min(5.0, budget))
    except Exception as exc:  # noqa: BLE001 - loop gone or queue blocked
        raise ConnectorUnavailable("本机调查连接器通道不可用，请重新连接") from exc
    deadline = time.monotonic() + budget
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ConnectorUnavailable("本机调查操作超时，请检查本机网络或重新执行连接命令")
        try:
            message = connection.inbound.get(timeout=remaining)
        except queue.Empty as exc:
            raise ConnectorUnavailable("本机调查操作超时，请检查本机网络或重新执行连接命令") from exc
        if message is None:
            connection.dead = True
            raise ConnectorUnavailable("本机调查连接器已断开，请重新执行连接命令")
        if message.get("type") == "response" and message.get("id") == request_id:
            if not message.get("ok"):
                raise ConnectorUnavailable(str(message.get("error") or "本机调查操作失败")[:400])
            result = message.get("result")
            if not isinstance(result, dict):
                raise ConnectorUnavailable("本机调查返回了无法识别的结果")
            return result
        # Unsolicited messages (late responses, pings) are ignored here.
