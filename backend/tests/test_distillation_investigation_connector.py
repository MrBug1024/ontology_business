"""Investigation connector unit coverage: tokens, gateway RPC, executor routing.

Browser behavior itself (Playwright against a real target) is intentionally
not mocked here; these tests pin the security-relevant invariants that must
hold between the platform and the member-machine connector script.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import get_settings
from app.distillation_target_schemas import BrowserScope, TargetSystem
from app.services import distillation_investigation_connector_service as connectors
from app.services.distillation_browser_network import within_scope as server_within_scope
from app.services.distillation_investigation_connector_gateway import ConnectorGateway, ConnectorUnavailable


def _load_connector_module():
    key = "investigation_connector"
    if key in sys.modules:
        return sys.modules[key]
    path = Path(__file__).resolve().parents[1] / "connectors" / "investigation_connector.py"
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[key] = module
    return module


@pytest.fixture
def connector_enabled(monkeypatch):
    monkeypatch.setenv("DISTILLATION_CONNECTOR_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _target(**overrides) -> TargetSystem:
    fields = dict(
        key="erp", name="内网ERP", base_url="https://erp.example.com", purpose="订单查询",
        allowed_paths=["/orders", "/report", "/login"],
        browser=BrowserScope(entry_path="/orders", login_paths=["/login"]),
    )
    fields.update(overrides)
    return TargetSystem(**fields)


class _FakeResult:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class _FakeDb:
    def __init__(self, scalar_value=None, existing=(), stored=None):
        self.info = {"tenant_id": "tenant-1", "user_id": "user-1"}
        self._scalar_value = scalar_value
        self._existing = list(existing)
        self._stored = stored if stored is not None else {}
        self.added = []

    def scalars(self, _statement):
        return _FakeResult(self._existing)

    def scalar(self, _statement):
        return self._scalar_value

    def add(self, row):
        self.added.append(row)

    def flush(self):
        pass

    def commit(self):
        pass

    def get(self, _model, row_id):
        return self._stored.get(row_id)


def test_connector_tokens_use_domain_separated_stable_hashes():
    first, second = connectors.generate_token(), connectors.generate_token()
    assert first.startswith("diconn_") and second.startswith("diconn_") and first != second
    assert connectors.token_hash(first) == connectors.token_hash(first)
    assert connectors.token_hash(first) != connectors.token_hash(second)
    # Domain separation: not a bare SHA-256 of the token material.
    assert connectors.token_hash(first) != hashlib.sha256(first.encode()).hexdigest()


def test_connector_command_rejects_shell_metacharacters():
    assert connectors.connector_command("https://platform.example.com", "diconn_ok-token1")
    for hostile in ("diconn_evil';rm", "diconn_a&b;c", 'diconn_x"y'):
        with pytest.raises(ValueError):
            connectors.connector_command("https://platform.example.com", hostile)
    with pytest.raises(ValueError):
        connectors.connector_command("https://platform.example.com/'; drop", "diconn_ok-token1")


def test_create_session_rotates_previous_and_returns_token_once(connector_enabled):
    previous = SimpleNamespace(status="connected", revoked_at=None, revoked_reason="")
    db = _FakeDb(existing=[previous])
    payload, token = connectors.create_session(db, "project-1", "user-1", _target(),
        origin="https://platform.example.com")
    assert previous.status == "revoked"
    assert token.startswith("diconn_") and token in payload["command"]
    assert payload["script_url"].endswith("/api/distillation-connector/script")
    assert payload["status"] == "pending"
    assert len(db.added) == 1 and db.added[0].status == "pending"


def test_create_session_requires_enabled_deployment(monkeypatch):
    monkeypatch.delenv("DISTILLATION_CONNECTOR_ENABLED", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(HTTPException) as error:
            connectors.create_session(_FakeDb(), "project-1", "user-1", _target(), origin="https://x")
        assert error.value.status_code == 409
    finally:
        get_settings.cache_clear()


def test_find_active_rejects_expired_session_rows():
    live = SimpleNamespace(expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    assert connectors.find_active_by_token(_FakeDb(scalar_value=live), connectors.generate_token()) is live
    stale = SimpleNamespace(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                            status="pending", revoked_reason="")
    assert connectors.find_active_by_token(_FakeDb(scalar_value=stale), connectors.generate_token()) is None
    assert stale.status == "expired"


def test_revoke_marks_authoritative_status():
    row = SimpleNamespace(id="session-1", project_id="project-1", tenant_id="tenant-1",
                          target_key="erp", status="connected", token_prefix="diconn_abc",
                          connector_info=None, created_at=datetime.now(timezone.utc),
                          expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                          connected_at=None, last_seen_at=None, disconnected_at=None,
                          revoked_at=None, revoked_reason="")
    payload = connectors.revoke(_FakeDb(stored={"session-1": row}), "project-1", "session-1", "测试断开")
    assert payload["status"] == "revoked" and row.revoked_reason == "测试断开"
    with pytest.raises(HTTPException) as error:
        connectors.revoke(_FakeDb(), "project-1", "missing", "x")
    assert error.value.status_code == 404


def test_active_connection_requires_matching_scope():
    from app.services.distillation_access_service import target_hash

    attached = SimpleNamespace(status="connected", scope_hash=target_hash(_target()),
                               expires_at=datetime.now(timezone.utc) + timedelta(minutes=5), revoked_reason="")
    assert connectors.active_connection_session(_FakeDb(scalar_value=attached), "project-1", _target()) is attached
    changed = _target(allowed_paths=["/other"])
    assert connectors.active_connection_session(_FakeDb(scalar_value=attached), "project-1", changed) is None
    assert attached.status == "expired"


# --- Connector script (member machine) vs server policy parity ---------------


def test_connector_scope_matches_server_scope():
    connector = _load_connector_module()
    target = _target()
    target_payload = target.model_dump(mode="json")
    cases = [
        "https://erp.example.com/orders",
        "https://erp.example.com/orders/detail",
        "https://erp.example.com/report",
        "https://erp.example.com/admin",
        "https://evil.example.com/orders",
        "https://user:pass@erp.example.com/orders",
        "https://erp.example.com:8443/orders",
    ]
    for url in cases:
        assert connector.within_scope(target_payload, url) == server_within_scope(target, url), url


class _FakeResponse:
    def __init__(self, status=200, headers=None, body=b"page"):
        self.status = status
        self.headers = headers or {"content-type": "text/html"}
        self._body = body

    def body(self):
        return self._body


class _FakeRoute:
    def __init__(self, method="GET", url="https://erp.example.com/orders", resource_type="document",
                 redirected_from=None, post_data_buffer=None, response=None):
        self.request = SimpleNamespace(method=method, url=url, resource_type=resource_type,
                                       redirected_from=redirected_from, post_data_buffer=post_data_buffer)
        self.response = response or _FakeResponse()
        self.aborted = False
        self.fulfillment = None

    def abort(self):
        self.aborted = True

    def fetch(self, **_kwargs):
        return self.response

    def fulfill(self, **kwargs):
        self.fulfillment = kwargs


def test_connector_policy_blocks_writes_and_out_of_scope():
    connector = _load_connector_module()
    target = _target().model_dump(mode="json")
    policy = connector.LocalPolicy(target)

    permitted = _FakeRoute()
    policy.route(permitted)
    assert not permitted.aborted and permitted.fulfillment["status"] == 200

    write = _FakeRoute(method="POST")
    policy.route(write)
    assert write.aborted and any("未授权的提交" in item["reason"] for item in policy.blocked)

    foreign = _FakeRoute(url="https://evil.example.com/orders")
    policy.route(foreign)
    assert foreign.aborted

    asset = _FakeRoute(resource_type="image")
    policy.route(asset)
    assert asset.aborted and asset.fulfillment is None

    redirect = _FakeRoute(response=_FakeResponse(status=302, headers={"location": "/orders"}))
    policy.route(redirect)
    assert redirect.fulfillment["status"] == 409
    assert str(redirect.fulfillment["body"]).startswith("redirect_blocked:")

    policy.login_active = True
    login_post = _FakeRoute(method="POST", url="https://erp.example.com/login", post_data_buffer=b"u=1")
    policy.route(login_post)
    assert not login_post.aborted


def test_connector_runtime_requires_open_before_other_ops():
    connector = _load_connector_module()
    runtime = connector.ConnectorRuntime(headless=True)
    with pytest.raises(connector.BrowserAccessError):
        runtime.handle("browser.snapshot", {})


def test_connector_runtime_dispatches_open_and_close(monkeypatch):
    connector = _load_connector_module()
    opened = {}

    class _FakeSession:
        def __init__(self, target, credentials, headless):
            opened["target_key"] = target["key"]
            opened["credentials"] = dict(credentials or {})
            opened["headless"] = headless

        def start(self, entry_path):
            opened["entry"] = entry_path
            return {"observation": {"status": "observed"}}

        def close(self):
            opened["closed"] = True

    monkeypatch.setattr(connector, "ConnectorSession", _FakeSession)
    runtime = connector.ConnectorRuntime(headless=False)
    result = runtime.handle("browser.open", {
        "target": _target().model_dump(mode="json"), "credentials": {"username": "expert"}})
    assert result["observation"]["status"] == "observed"
    assert opened["entry"] == "/orders" and opened["headless"] is False
    runtime.handle("browser.close", {})
    assert opened["closed"] is True
    with pytest.raises(connector.BrowserAccessError):
        runtime.handle("browser.snapshot", {})


# --- Gateway RPC bridging sync workers with the event loop -------------------


class _LoopThread:
    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self.loop

    def __exit__(self, *_args):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()
        return False


def test_gateway_call_roundtrip_and_error_mapping():
    gateway = ConnectorGateway()
    with _LoopThread() as loop:
        connection, _replaced = gateway.register("session-1", "project-1", "erp", loop)

        async def echo_consumer():
            item = await connection.outbound.get()
            request = json.loads(item)
            connection.inbound.put_nowait({"type": "response", "id": request["id"],
                                           "ok": True, "result": {"echo": request["op"]}})

        asyncio.run_coroutine_threadsafe(echo_consumer(), loop)
        result = gateway_module_call("session-1", "browser.snapshot", {"offset": 0}, gateway=gateway, timeout=5)
        assert result == {"echo": "browser.snapshot"}

        async def error_consumer():
            item = await connection.outbound.get()
            request = json.loads(item)
            connection.inbound.put_nowait({"type": "response", "id": request["id"],
                                           "ok": False, "error": "该地址疑似业务写操作"})

        asyncio.run_coroutine_threadsafe(error_consumer(), loop)
        with pytest.raises(ConnectorUnavailable, match="业务写操作"):
            gateway_module_call("session-1", "browser.navigate", {"path": "/x"}, gateway=gateway, timeout=5)

        connection.inbound.put_nowait(None)
        with pytest.raises(ConnectorUnavailable, match="已断开"):
            gateway_module_call("session-1", "browser.snapshot", {}, gateway=gateway, timeout=5)
        # The drained disconnect marker also marks the connection dead, so the
        # next call fails immediately instead of waiting for a timeout.
        with pytest.raises(ConnectorUnavailable, match="未连接"):
            gateway_module_call("session-1", "browser.snapshot", {}, gateway=gateway, timeout=5)

    with pytest.raises(ConnectorUnavailable, match="未连接"):
        gateway_module_call("session-1", "browser.snapshot", {}, gateway=gateway, timeout=1)


def test_gateway_call_times_out_without_response():
    gateway = ConnectorGateway()
    with _LoopThread() as loop:
        gateway.register("session-2", "project-1", "erp", loop)
        started = time.monotonic()
        with pytest.raises(ConnectorUnavailable, match="超时"):
            gateway_module_call("session-2", "browser.snapshot", {}, gateway=gateway, timeout=0.2)
        assert time.monotonic() - started < 5


def gateway_module_call(session_id, op, args, *, gateway, timeout):
    from app.services import distillation_investigation_connector_gateway as module

    saved = module._gateway
    module._gateway = gateway
    try:
        return module.call(session_id, op, args, timeout=timeout)
    finally:
        module._gateway = saved


def test_gateway_replacement_kicks_previous_connection():
    gateway = ConnectorGateway()
    with _LoopThread() as loop:
        first, _none = gateway.register("session-3", "project-1", "erp", loop)
        second, replaced = gateway.register("session-3", "project-1", "erp", loop)
        assert replaced is first and first.dead
        assert gateway.get("session-3") is second
        gateway.unregister("session-3", second)
        assert gateway.get("session-3") is None


# --- Executor and BrowserTurn routing ----------------------------------------


def test_connector_executor_maps_ops_and_requires_authorization(monkeypatch):
    from app.services import distillation_connector_executor as executor
    from app.services.distillation_browser_network import BrowserAccessError

    recorded = []
    authorizations = []

    def fake_call(session_id, op, args, timeout=None):
        recorded.append((session_id, op))
        if op == "browser.open":
            return {"observation": {"status": "observed"}, "screenshot": None}
        if op == "browser.bad":
            raise ConnectorUnavailable("本机调查连接器已断开，请重新执行连接命令")
        return {}

    monkeypatch.setattr(executor, "connector_call", fake_call)
    target = _target()
    investigation = executor.ConnectorInvestigation("session-9", target,
        lambda: authorizations.append(1), {"username": "expert", "secret": "s3cret"})
    payload = investigation.open()
    assert payload["observation"]["status"] == "observed"
    assert recorded[0] == ("session-9", "browser.open")
    assert authorizations
    investigation.navigate("/orders")
    investigation.snapshot(0)
    investigation.fill("page", "ref", "value")
    investigation.click("page", "ref", "query")
    investigation.login("page", "u", "p", "s")
    assert {op for _sid, op in recorded} == {"browser.open", "browser.navigate", "browser.snapshot",
        "browser.fill", "browser.click", "browser.login"}
    monkeypatch.setattr(executor, "connector_call",
        lambda *_a, **_k: (_ for _ in ()).throw(ConnectorUnavailable("本机调查连接器已断开")))
    with pytest.raises(BrowserAccessError):
        investigation.snapshot(0)


def test_browser_turn_prefers_attached_connector(monkeypatch, connector_enabled):
    from app.services import distillation_browser_runtime as runtime_module
    from app.services import distillation_connector_executor as executor
    from app.services import distillation_investigation_connector_service as service

    init_arguments = {}

    class _FakeRemote:
        target = None

        def __init__(self, session_id, target, authorize, credentials=None):
            init_arguments["session_id"] = session_id
            self.target = target
            _FakeRemote.target = target

        def open(self):
            init_arguments["opened"] = True
            return {"observation": {"status": "observed"}}

        def close(self):
            pass

    monkeypatch.setattr(executor, "ConnectorInvestigation", _FakeRemote)
    monkeypatch.setattr(service, "active_connection_session",
        lambda db, project_id, target: SimpleNamespace(id="session-active"))

    class _SessionFactory:
        def __call__(self):
            return self

        def __enter__(self):
            return SimpleNamespace(commit=lambda: None)

        def __exit__(self, *_args):
            return False

    turn = runtime_module.BrowserTurn(lambda _target: None, "project-1", _SessionFactory())
    turn.open(_target(), None)
    assert init_arguments == {"session_id": "session-active", "opened": True}


def test_browser_turn_falls_back_to_local_executor(monkeypatch, connector_enabled):
    from app.services import distillation_browser_runtime as runtime_module
    from app.services import distillation_investigation_connector_service as service

    monkeypatch.setattr(service, "active_connection_session", lambda db, project_id, target: None)

    class _FakeLocal:
        target = None

        def __init__(self, target, authorize, credentials=None):
            _FakeLocal.target = target

        def start(self):
            return None

        def navigate(self, path):
            return {"observation": {"status": "observed"}, "entry": path}

        def close(self):
            pass

    monkeypatch.setattr(runtime_module, "InvestigationBrowser", _FakeLocal)
    target = _target()

    class _SessionFactory:
        def __call__(self):
            return self

        def __enter__(self):
            return SimpleNamespace(commit=lambda: None)

        def __exit__(self, *_args):
            return False

    turn = runtime_module.BrowserTurn(lambda _target: None, "project-1", _SessionFactory())
    content = turn.open(target, None)
    assert content["entry"] == "/orders" and _FakeLocal.target is target


def test_screenshot_decode_bounds():
    from app.services import distillation_screenshot_service as screenshots

    payload = base64.b64encode(b"\xff\xd8jpeg").decode("ascii")
    assert screenshots.decode_step_screenshot(payload) == b"\xff\xd8jpeg"
    assert screenshots.decode_step_screenshot(None) is None
    assert screenshots.decode_step_screenshot("not-base64!!") is None
    oversized = base64.b64encode(b"x" * (screenshots.MAX_SCREENSHOT_BYTES + 1)).decode("ascii")
    assert screenshots.decode_step_screenshot(oversized) is None


def test_browser_tools_pops_screenshot_from_model_content(monkeypatch):
    from app.distillation_browser_schemas import BrowserTarget
    from app.services import distillation_browser_tools as browser_tools

    encoded = base64.b64encode(b"\xff\xd8snapshot").decode("ascii")

    class _FakeSession:
        target = SimpleNamespace(key="erp")

        def open(self, target, credentials):
            return {"observation": {"target_key": "erp", "url": "https://erp.example.com/orders",
                "title": "订单", "status": "observed", "text": "页面", "read_only": True,
                "visible_fields": [], "allowed_links": [], "content_sha256": "a" * 64,
                "retrieved_at": "2026-10-10T00:00:00+00:00", "limitations": ["x"]},
                "screenshot": encoded}

    document = SimpleNamespace(target_systems=[_target()])
    turn = SimpleNamespace(project_id="project-1")
    result = browser_tools.execute(_FakeDb(), "open_business_system",
        BrowserTarget(target_key="erp"), document, turn, SimpleNamespace(open=_FakeSession().open))
    assert result.screenshot == b"\xff\xd8snapshot"
    assert "screenshot" not in result.content
