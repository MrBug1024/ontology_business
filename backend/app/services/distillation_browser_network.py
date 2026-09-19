"""Browser HTTP is fulfilled through a bounded pinned transport, never direct egress."""
from __future__ import annotations

import time
import json
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from ..distillation_target_schemas import TargetSystem, canonical_page_path
from .readonly_http_adapter import _resolve_target
from .release_service import _secret_key


MAX_RESOURCE_BYTES = 4 * 1024 * 1024
MAX_SESSION_BYTES = 32 * 1024 * 1024
MAX_REQUESTS = 180
REQUEST_SECONDS = 12


class BrowserAccessError(ValueError):
    """Only constant, safe messages are exposed at the investigation boundary."""


def within_scope(target: TargetSystem, url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if parsed.username or parsed.password or f"{parsed.scheme}://{parsed.netloc}" != target.base_url:
            return False
        path = canonical_page_path(parsed.path or "/")
        return any(path == allowed or path.startswith(allowed.rstrip("/") + "/") for allowed in target.allowed_paths)
    except ValueError:
        return False


class PinnedBrowserTransport(httpx.BaseTransport):
    def __init__(self, target):
        self.target = target
        self.inner = httpx.HTTPTransport(trust_env=False, retries=0)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        pinned = self.target
        if (request.url.scheme, request.url.raw_host.decode("ascii"), request.url.port or (443 if request.url.scheme == "https" else 80)) != (pinned.scheme, pinned.hostname, pinned.port):
            raise BrowserAccessError("浏览器请求超出已授权站点")
        headers = request.headers.copy()
        headers["Host"] = pinned.authority
        extensions = {**request.extensions, "sni_hostname": pinned.hostname}
        return self.inner.handle_request(httpx.Request(request.method, request.url.copy_with(host=pinned.address),
            headers=headers, stream=request.stream, extensions=extensions))

    def close(self):
        self.inner.close()


class BrowserNetwork:
    def __init__(self, target: TargetSystem, authorize, redact, remember_secret):
        self.target, self.authorize, self.redact = target, authorize, redact
        self.remember_secret = remember_secret
        self.client = httpx.Client(transport=PinnedBrowserTransport(_resolve_target(target.base_url)),
            follow_redirects=False, trust_env=False, timeout=httpx.Timeout(REQUEST_SECONDS, connect=5))
        self.requests = self.bytes = 0
        self.blocked: list[dict[str, str]] = []
        self.login_paths: set[str] = set()
        self.login_active = False
        self.query_active = False
        self.deadline = time.monotonic() + 30

    def block(self, path: str, reason: str):
        record = {"path": self.redact(path[:1000]), "reason": reason}
        if record not in self.blocked and len(self.blocked) < 10:
            self.blocked.append(record)

    def route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        path = parsed.path or "/"
        try:
            self.authorize()
            if not within_scope(self.target, request.url):
                raise BrowserAccessError("请求超出已配置站点或路径范围")
            if request.redirected_from:
                raise BrowserAccessError("未跟随重定向；请核对目标后显式导航")
            # URL credentials are never transmitted, even if a page tries a GET login.
            if self.redact(unquote(request.url)) != unquote(request.url):
                raise BrowserAccessError("地址包含疑似凭据，已阻止发送")
            scope = self.target.browser
            permitted_post = request.method == "POST" and (
                (self.login_active and path in self.login_paths)
                or (self.query_active and path in scope.readonly_post_paths))
            if request.method not in {"GET", "HEAD"} and not permitted_post:
                raise BrowserAccessError("未授权的提交已阻止；需要在系统配置中明确登录或只读查询接口")
            if request.resource_type in {"image", "media", "font"}:
                route.abort()
                return
            if self.requests >= MAX_REQUESTS or self.bytes >= MAX_SESSION_BYTES or time.monotonic() > self.deadline:
                raise BrowserAccessError("浏览器读取达到本轮时间、请求或流量上限")
            headers = {key: value for key, value in request.all_headers().items()
                if key.lower() not in {"host", "content-length", "connection", "accept-encoding"}}
            headers["accept-encoding"] = "identity"
            body = request.post_data_buffer
            if body and len(body) > 64 * 1024:
                raise BrowserAccessError("查询或登录请求过大")
            self.requests += 1
            with self.client.stream(request.method, request.url, headers=headers, content=body) as response:
                if 300 <= response.status_code < 400:
                    candidate = urljoin(request.url, response.headers.get("location", ""))
                    location = urlsplit(candidate)
                    destination = location.path + ("#" + location.fragment if location.fragment else "") if within_scope(self.target, candidate) else "跨站地址，需单独配置授权"
                    self.block(path, "重定向已阻止，请显式打开返回的站内地址")
                    cookies = response.headers.get_list("set-cookie")
                    route.fulfill(status=409, content_type="text/plain", headers={"set-cookie": "\n".join(cookies)} if cookies else {},
                        body=self.redact("redirect_blocked: " + destination)[:1500])
                    return
                if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                    raise BrowserAccessError("响应未遵守未压缩读取约定")
                chunks = bytearray()
                for chunk in response.iter_raw(chunk_size=8192):
                    self.bytes += len(chunk)
                    if len(chunks) + len(chunk) > MAX_RESOURCE_BYTES or self.bytes > MAX_SESSION_BYTES or time.monotonic() > self.deadline:
                        raise BrowserAccessError("页面资源超过读取上限")
                    chunks.extend(chunk)
                for key, value in response.headers.items():
                    if _secret_key(key) and key.lower() != "set-cookie":
                        self.remember_secret(value)
                if "application/json" in response.headers.get("content-type", ""):
                    try:
                        self.remember_json_secrets(json.loads(chunks), 0)
                    except (ValueError, RecursionError):
                        pass
                response_headers = {key: value for key, value in response.headers.items()
                    if key.lower() not in {"content-length", "content-encoding", "transfer-encoding", "connection", "set-cookie"}}
                cookies = response.headers.get_list("set-cookie")
                if cookies:
                    response_headers["set-cookie"] = "\n".join(cookies)
                route.fulfill(status=response.status_code, headers=response_headers, body=bytes(chunks))
        except BrowserAccessError as exc:
            self.block(path, str(exc))
            route.abort()
        except Exception:
            # Do not serialize HTTP/Playwright errors: they may contain auth bodies.
            self.block(path, "读取失败或授权已变化，请重新核对配置")
            route.abort()

    def close(self):
        self.client.close()

    def remember_json_secrets(self, value, depth):
        if depth > 8:
            return
        if isinstance(value, dict):
            for key, child in list(value.items())[:100]:
                if _secret_key(key) and isinstance(child, str):
                    self.remember_secret(child)
                elif isinstance(child, (dict, list)):
                    self.remember_json_secrets(child, depth + 1)
        elif isinstance(value, list):
            for child in value[:100]:
                self.remember_json_secrets(child, depth + 1)
