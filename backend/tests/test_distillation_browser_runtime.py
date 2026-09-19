"""Opt-in tests with real Chromium and a disposable synthetic business website."""
from __future__ import annotations

import json
import os
import secrets
from urllib.parse import parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from app.config import get_settings
from app.distillation_target_schemas import TargetSystem
from app.services.distillation_browser_network import BrowserAccessError, within_scope
from app.services.distillation_browser_runtime import InvestigationBrowser


def test_browser_scope_rejects_cross_origin_and_encoded_escape():
    target = TargetSystem(key="system", name="System", base_url="https://example.com", purpose="research", allowed_paths=["/work"], browser={})
    assert within_scope(target, "https://example.com/work/history?period=1#/list")
    for address in ["https://example.com/workflow", "https://example.com/work/%2e%2e/admin", "https://outside.example/work", "https://example.com@outside.example/work"]:
        assert not within_scope(target, address)


@pytest.fixture
def business_site(monkeypatch):
    if os.getenv("RUN_BROWSER_INTEGRATION_TESTS") != "1":
        pytest.skip("Set RUN_BROWSER_INTEGRATION_TESTS=1 with installed Chromium")
    credential = {"username": "reader-" + secrets.token_hex(8), "secret": secrets.token_urlsafe(24)}
    cookie = secrets.token_hex(24)
    received = []
    page = '''<!doctype html><html><head><title>Sample business system</title></head><body>
      <form action="/login" method="post"><label>Account<input id="account"></label><label>Password<input id="pass" type="password"></label><button>Log in</button></form>
      <script>
      new WebSocket(location.origin.replace('http', 'ws') + '/blocked-websocket');
      document.querySelector('form').onsubmit = async event => {
        event.preventDefault(); const answer = await fetch('/login', {method:'POST',body:JSON.stringify({username:document.querySelector('#account').value,secret:document.querySelector('#pass').value})});
        if (!answer.ok) return; const rows = await (await fetch('/data')).json();
        document.body.innerHTML = '<h1>Historical results</h1><label>Period<input id="period"></label><button id="query">Search</button><button id="unsafe">Run operation</button><a href="/details">Details</a><div id="rows"></div>';
        const render = data => document.querySelector('#rows').innerHTML='<table><tr><th>period</th><th>result</th></tr>'+data.map(r=>'<tr><td>'+r.period+'</td><td>'+r.result+'</td></tr>').join('')+'</table>';
        render(rows); document.querySelector('#query').onclick = async () => render(await (await fetch('/query', {method:'POST',body:JSON.stringify({period:document.querySelector('#period').value})})).json());
        document.querySelector('#unsafe').onclick=()=>fetch('/mutate', {method:'POST',body:'business mutation'});
      };
      </script></body></html>'''

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, status, body, content_type="text/html", headers=None):
            raw = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            received.append(("GET", self.path))
            if self.path == "/":
                self.respond(200, page)
            elif self.path == "/form-entry":
                self.respond(200, '<form action="/redirect-login" method="post"><label>Account<input name="username"></label><label>Password<input name="secret" type="password"></label><button>Log in</button></form>')
            elif self.path == "/protected-result":
                if f"session={cookie}" not in self.headers.get("Cookie", ""):
                    self.respond(401, "Login required")
                else:
                    self.respond(200, '<h1>Confirmed historical result</h1><p>' + cookie + '</p><script src="https://outside.invalid/blocked.js"></script>')
            elif self.path == "/data":
                if f"session={cookie}" not in self.headers.get("Cookie", ""):
                    self.respond(401, "Login required")
                else:
                    self.respond(200, '[{"period":"2026-01","result":"closed"},{"period":"2026-02","result":"reworked"}]', "application/json")
            elif self.path == "/details":
                self.respond(200, "<h1>Process details</h1><p>received → reviewed → closed</p>")
            else:
                self.respond(404, "Missing")

        def do_POST(self):
            received.append(("POST", self.path))
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path == "/login" and json.loads(raw) == credential:
                self.respond(200, "{}", "application/json", {"Set-Cookie": f"session={cookie}; HttpOnly; Path=/"})
            elif self.path == "/redirect-login" and {key: value[0] for key, value in parse_qs(raw.decode()).items()} == credential:
                self.respond(302, "", headers={"Location": "/protected-result", "Set-Cookie": f"session={cookie}; HttpOnly; Path=/"})
            elif self.path == "/query":
                self.respond(200, '[{"period":"2026-02","result":"reworked"}]', "application/json")
            else:
                self.respond(403, "Denied")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = get_settings()
    monkeypatch.setattr(settings, "distillation_browser_enabled", True)
    monkeypatch.setattr(settings, "allow_insecure_mcp_http", True)
    monkeypatch.setattr(settings, "mcp_private_host_allowlist", "127.0.0.1")
    target = TargetSystem(key="system", name="Test system", base_url=f"http://127.0.0.1:{server.server_port}", purpose="research", allowed_paths=["/"], access_mode="authorized_readonly", browser={"readonly_post_paths": ["/query"]})
    try:
        yield target, credential, received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def ref(page, name):
    matches = [item for item in page["elements"] if item["name"] == name]
    assert len(matches) == 1, (name, page["elements"])
    return matches[0]["ref"]


def test_real_browser_login_dynamic_rows_query_navigation_and_write_rejection(business_site):
    target, credentials, received = business_site
    session = InvestigationBrowser(target, lambda: None, credentials)
    try:
        session.start()
        page = session.navigate("/")
        assert page["observation"]["status"] == "login_required"
        assert page["login_configured"] is True
        assert "login_business_system" in page["login_instructions"]
        assert ("GET", "/blocked-websocket") not in received
        page = session.login(page["page_id"], ref(page, "Account"), ref(page, "Password"), ref(page, "Log in"))
        assert page["observation"]["status"] == "observed"
        assert "closed" in page["observation"]["text"] and "reworked" in str(page["tables"])
        assert all(value not in json.dumps(page) for value in credentials.values())
        old_id, old_ref = page["page_id"], ref(page, "Search")
        page = session.fill(page["page_id"], ref(page, "Period"), "2026-02")
        with pytest.raises(BrowserAccessError, match="过期"):
            session.click(old_id, old_ref, "query")
        page = session.click(page["page_id"], ref(page, "Search"), "query")
        assert "2026-01" not in page["observation"]["text"] and ("POST", "/query") in received
        page = session.click(page["page_id"], ref(page, "Run operation"), "query")
        assert ("POST", "/mutate") not in received
        assert any(item["path"] == "/mutate" for item in page["blocked_requests"])
        page = session.click(page["page_id"], ref(page, "Details"), "navigate")
        assert "received → reviewed → closed" in page["observation"]["text"]
    finally:
        session.close()


def test_browser_rechecks_revocation_and_rejects_private_host_by_default(business_site, monkeypatch):
    target, credentials, received = business_site
    enabled = True

    def authorize():
        if not enabled:
            raise BrowserAccessError("授权已撤销")

    session = InvestigationBrowser(target, authorize, credentials)
    try:
        session.start()
        session.navigate("/")
        enabled = False
        count = len(received)
        with pytest.raises(BrowserAccessError, match="撤销"):
            session.navigate("/details")
        assert len(received) == count
    finally:
        session.close()
    monkeypatch.setattr(get_settings(), "mcp_private_host_allowlist", "")
    session = InvestigationBrowser(target, lambda: None, credentials)
    with pytest.raises(BrowserAccessError):
        session.start()


def test_form_login_keeps_cookie_without_following_redirect_and_redacts_echo(business_site):
    target, credentials, received = business_site
    session = InvestigationBrowser(target, lambda: None, credentials)
    try:
        session.start()
        page = session.navigate("/form-entry")
        page = session.login(page["page_id"], ref(page, "Account"), ref(page, "Password"), ref(page, "Log in"))
        assert ("POST", "/redirect-login") in received
        assert ("GET", "/protected-result") not in received
        assert "redirect_blocked: /protected-result" in page["observation"]["text"]
        page = session.navigate("/protected-result")
        assert "Confirmed historical result" in page["observation"]["text"]
        assert "[已隐藏凭据]" in page["observation"]["text"]
        assert all(cookie["value"] not in json.dumps(page) for cookie in session.context.cookies())
        assert any(item["path"] == "/blocked.js" for item in page["blocked_requests"])
    finally:
        session.close()
