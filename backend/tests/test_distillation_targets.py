"""Target investigation accepts only explicit anonymous reads and inert evidence."""
from __future__ import annotations

import hashlib
import asyncio
from concurrent.futures import Future
from threading import BoundedSemaphore
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.distillation_schemas import DistillationDocument
from app.distillation_target_schemas import TargetSystem
from app.services import distillation_target_service, mcp_service, readonly_http_adapter, release_service


def _target(**changes):
    return TargetSystem(**({"key": "portal", "name": "Operations", "base_url": "https://example.com",
        "purpose": "Review intake steps", "allowed_paths": ["/intake", "/help"]} | changes))


@pytest.mark.parametrize("origin", ["http://example.com", "https://user:password@example.com",
    "https://example.com?token=test", "https://example.com#section", "https://example.com/path",
    "https://example.com/?", "https://example.com/\\admin"])
def test_target_origin_rejects_credentials_and_non_origin_urls(origin):
    with pytest.raises(ValidationError):
        _target(base_url=origin)


@pytest.mark.parametrize("path", ["https://example.com/a", "//other.test/a", "/a?token=test", "/a#section",
    "/a/../private", "/a/%2e%2e/private", "/%252e%252e/private", "/a\\private", "/a%0d%0aX:1"])
def test_target_page_requires_exact_safe_path(path):
    with pytest.raises(ValidationError):
        _target(allowed_paths=[path])


def test_target_configuration_is_bounded_and_password_fields_are_not_accepted():
    with pytest.raises(ValidationError):
        _target(password="synthetic")
    with pytest.raises(ValidationError):
        _target(allowed_paths=["/same", "/same"])
    with pytest.raises(ValidationError):
        DistillationDocument(target_systems=[_target(), _target()])
    with pytest.raises(ValidationError):
        _target(access_mode="browser_login")
    assert _target(base_url="https://EXAMPLE.com/").base_url == "https://example.com"


def test_disabled_or_unconfigured_path_is_rejected_before_network(monkeypatch):
    monkeypatch.setattr(readonly_http_adapter, "read_document", lambda _url: pytest.fail("network read"))
    for target, path in ((_target(enabled=False), "/intake"), (_target(), "/intake/delete"), (_target(), "/unknown")):
        with pytest.raises(distillation_target_service.TargetReadError):
            distillation_target_service.read_target_system(target, path)


def _page(monkeypatch, body: str, *, status=200, content_type="text/html"):
    document = readonly_http_adapter.ReadonlyDocument(status, content_type, body.encode())
    monkeypatch.setattr(readonly_http_adapter, "read_document", lambda _url: document)
    return document


def test_target_read_returns_visible_evidence_without_scripts_form_values_or_outside_links(monkeypatch):
    document = _page(monkeypatch, """<title>Intake</title><p>Submit a case, then review evidence.</p>
        <script>secret_script_value</script><input type="hidden" value="hidden_value">
        <input aria-label="Case subject" value="private_field_value">
        <a href="/help">Guide</a><a href="/outside">Outside</a>
        <a href="https://other.test/help">External</a><a href="/help?token=synthetic">Token</a>""")
    result = distillation_target_service.read_target_system(_target(), "/intake")
    assert result["status"] == "observed" and result["read_only"]
    assert result["title"] == "Intake"
    assert "Submit a case" in result["text"]
    assert result["allowed_links"] == ["/help"]
    assert result["visible_fields"] == ["Case subject"]
    assert result["content_sha256"] == hashlib.sha256(document.body).hexdigest()
    assert all(value not in str(result) for value in ("secret_script_value", "hidden_value", "private_field_value"))


@pytest.mark.parametrize("body,status,expected", [
    ('<p>Sign in</p><input type="password">', 200, "login_required"),
    ("", 401, "login_required"), ("", 403, "login_required"),
    ("", 302, "redirect_blocked"), ("<script>render_app()</script>", 200, "javascript_required"),
])
def test_login_redirect_and_script_only_pages_are_incomplete_investigations(monkeypatch, body, status, expected):
    _page(monkeypatch, body, status=status)
    result = distillation_target_service.read_target_system(_target(), "/intake")
    assert result["status"] == expected
    assert any("未完成业务调查" in item for item in result["limitations"])


def test_target_json_secrets_are_redacted_as_structured_values(monkeypatch):
    _page(monkeypatch, '{"password":"synthetic-private-value","stage":"review"}', content_type="application/json")
    result = distillation_target_service.read_target_system(_target(), "/intake")
    assert "synthetic-private-value" not in result["text"]
    assert "review" in result["text"]
    assert release_service.safe_snapshot_content({"content": result["text"]}) == {"content": result["text"]}
    assert any("凭据字段已排除" in item for item in result["limitations"])


def test_nested_json_redaction_keeps_safe_fields_and_list_positions(monkeypatch):
    _page(monkeypatch, '{"items":[{"api_key":"synthetic-private-value","stage":"review"},'
        '"password=synthetic-private-value",{"count":2}]}', content_type="application/json")
    result = distillation_target_service.read_target_system(_target(), "/intake")
    assert "synthetic-private-value" not in result["text"]
    assert '"stage": "review"' in result["text"] and '"count": 2' in result["text"]
    assert "已隐藏敏感内容" in result["text"]
    assert release_service.safe_snapshot_content({"content": result["text"]}) == {"content": result["text"]}


class _Payload(httpx.AsyncByteStream):
    def __init__(self, body: bytes):
        self.body = body
        self.closed = False

    async def __aiter__(self):
        yield self.body

    async def aclose(self):
        self.closed = True


def _transport(monkeypatch, *, status=200, headers=None, body=b"visible page"):
    seen, streams = [], []
    target = mcp_service._PinnedTarget("https", "example.com", 443, "example.com", "93.184.216.34")
    monkeypatch.setattr(readonly_http_adapter, "_resolve_target", lambda _url: target)

    def respond(request):
        seen.append(request)
        stream = _Payload(body)
        streams.append(stream)
        return httpx.Response(status, headers=headers or {"content-type": "text/plain"}, stream=stream)

    def client(pinned_target, *, headers, timeout):
        transport = mcp_service._PinnedAsyncHTTPTransport(pinned_target, inner=httpx.MockTransport(respond))
        return httpx.AsyncClient(transport=transport, headers=headers, timeout=timeout,
                                 follow_redirects=False, trust_env=False)

    monkeypatch.setattr(readonly_http_adapter, "_pinned_http_client", client)
    return seen, streams


def test_http_reader_uses_get_pinned_ip_and_sni_without_credentials(monkeypatch):
    seen, streams = _transport(monkeypatch)
    result = readonly_http_adapter.read_document("https://example.com/intake")
    assert result.body == b"visible page"
    assert len(seen) == 1 and seen[0].method == "GET"
    assert seen[0].url.host == "93.184.216.34" and seen[0].headers["host"] == "example.com"
    assert seen[0].extensions["sni_hostname"] == "example.com"
    assert "authorization" not in seen[0].headers and "cookie" not in seen[0].headers
    assert streams[0].closed


def test_http_reader_does_not_follow_redirects(monkeypatch):
    seen, streams = _transport(monkeypatch, status=302, headers={"location": "https://internal.test/private"})
    result = readonly_http_adapter.read_document("https://example.com/intake")
    assert result.status_code == 302 and result.body == b""
    assert len(seen) == 1 and streams[0].closed


def test_http_timeout_cancels_read_and_closes_response(monkeypatch):
    _seen, streams = _transport(monkeypatch)

    async def delayed_chunks(self):
        await asyncio.sleep(1)
        yield self.body

    monkeypatch.setattr(_Payload, "__aiter__", delayed_chunks)
    monkeypatch.setattr(readonly_http_adapter, "HTTP_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(readonly_http_adapter.ReadonlyHttpError, match="超时"):
        readonly_http_adapter.read_document("https://example.com/intake")
    assert streams[0].closed


@pytest.mark.parametrize("headers,body", [
    ({"content-type": "image/png"}, b"image"),
    ({"content-type": "text/plain", "content-encoding": "gzip"}, b"compressed"),
    ({"content-type": "text/plain", "content-length": "9999999"}, b"small"),
    ({"content-type": "text/plain"}, b"x" * (readonly_http_adapter.MAX_DOCUMENT_BYTES + 1)),
], ids=["binary", "compressed", "declared_oversize", "stream_oversize"])
def test_http_reader_rejects_unsupported_or_oversized_responses_and_closes(monkeypatch, headers, body):
    _seen, streams = _transport(monkeypatch, headers=headers, body=body)
    with pytest.raises(readonly_http_adapter.ReadonlyHttpError):
        readonly_http_adapter.read_document("https://example.com/intake")
    assert streams[0].closed


def test_dns_timeout_keeps_capacity_until_resolution_really_finishes(monkeypatch):
    future = Future()
    monkeypatch.setattr(readonly_http_adapter, "_RESOLVER", SimpleNamespace(submit=lambda *_args: future))
    monkeypatch.setattr(readonly_http_adapter, "_RESOLUTION_SLOTS", BoundedSemaphore(1))
    monkeypatch.setattr(readonly_http_adapter, "DNS_TIMEOUT_SECONDS", 0.001)
    try:
        with pytest.raises(readonly_http_adapter.ReadonlyHttpError, match="超时"):
            readonly_http_adapter._resolve_target("https://example.com")
        with pytest.raises(readonly_http_adapter.ReadonlyHttpError, match="繁忙"):
            readonly_http_adapter._resolve_target("https://example.com")
    finally:
        future.set_result(object())
    assert readonly_http_adapter._RESOLUTION_SLOTS.acquire(blocking=False)


def test_private_and_mixed_dns_destinations_are_rejected(monkeypatch):
    monkeypatch.setattr(mcp_service, "get_settings", lambda: SimpleNamespace(
        allow_insecure_mcp_http=False, mcp_private_host_allowlist=""))
    with pytest.raises(ValueError):
        mcp_service._assert_safe_remote_target("https://127.0.0.1/")
    monkeypatch.setattr(mcp_service.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError):
        mcp_service._assert_safe_remote_target("https://example.com/")
