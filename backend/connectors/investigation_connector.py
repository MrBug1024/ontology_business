#!/usr/bin/env python3
"""业务蒸馏·本机调查连接器（investigation connector CLI）

在成员自己的电脑上运行，通过出站 WebSocket 连接业务蒸馏平台，把一台
受控的临时 Chromium（独立配置目录，不读取日常浏览器数据）作为业务调
查的浏览器执行端。适用于平台服务器无法直达的内网/专网业务系统。

用法：
    python investigation_connector.py --server "https://平台地址" --token "diconn_..."

依赖（本机 Python 3.10+）：
    python -m pip install "playwright>=1.40,<2" "websockets>=12,<16"
    python -m playwright install chromium

安全边界（与服务端浏览器一致）：
    - 只访问平台下发目标系统的授权路径（域名+路径前缀钉扎）；
    - 仅 GET/HEAD；登录与显式授权的只读查询 POST 除外；
    - 疑似业务写操作（保存/删除/审批等）一律拒绝；
    - 不跟随重定向；登录表单页面不会生成截图；
    - 凭据仅保存在内存中，进程退出即清除，不会写入日志。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import platform as host_platform
import re
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urljoin, urlsplit

CONNECTOR_VERSION = "0.1.0"
PROTOCOL_VERSION = 1
MAX_ELEMENTS = 80
MAX_LOGIN_ATTEMPTS = 2
MAX_REQUESTS = 180
MAX_SESSION_BYTES = 32 * 1024 * 1024
MAX_RESOURCE_BYTES = 4 * 1024 * 1024
MAX_SCREENSHOT_BYTES = 400_000
REQUEST_SECONDS = 20
_MUTATION = re.compile(r"删除|新增|保存|作废|支付|发送|审批|停用|启用|导入|上传|delete|remove|save|create|approve|pay|send|upload|logout|退出", re.I)
_ELEMENT_DESCRIPTION = """el => ({
  tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || '',
  role: el.getAttribute('role') || '',
  name: (el.getAttribute('aria-label') || (el.labels && [...el.labels].map(l => l.innerText).join(' ')) || el.innerText || el.getAttribute('placeholder') || el.getAttribute('name') || '').slice(0, 200),
  href: el.tagName === 'A' ? el.getAttribute('href') || '' : '',
  options: el.tagName === 'SELECT' ? [...el.options].slice(0, 30).map(o => ({label:o.label.slice(0,100), value:o.value.slice(0,100)})) : []
})"""


class BrowserAccessError(ValueError):
    """与服务端一致的恒定安全错误消息。"""


def _canonical_page_path(path: str) -> str:
    cleaned = "/".join(segment for segment in (path or "/").split("/") if segment)
    return "/" + cleaned


def within_scope(target: dict, url: str) -> bool:
    base_url = str(target.get("base_url") or "")
    allowed = [str(item) for item in (target.get("allowed_paths") or [])]
    try:
        parsed = urlsplit(url)
        if parsed.username or parsed.password or not base_url or f"{parsed.scheme}://{parsed.netloc}" != base_url:
            return False
        path = _canonical_page_path(parsed.path or "/")
        return any(path == item or path.startswith(item.rstrip("/") + "/") for item in allowed)
    except ValueError:
        return False


class LocalPolicy:
    """本机直连版网络边界：检查后由本机 Chromium 发出请求。

    与服务端“拦截后经钉扎代理”不同，这里由 route.fetch 在成员机器上直接
    发出请求（因此能到达内网系统），但范围、方法、写操作、重定向与流量
    边界保持同一套规则。
    """

    def __init__(self, target: dict):
        self.target = target
        self.requests = 0
        self.bytes = 0
        self.blocked: list[dict[str, str]] = []
        self.login_paths = {str(item) for item in ((target.get("browser") or {}).get("login_paths") or [])}
        self.login_active = False
        self.query_active = False
        self.deadline = time.monotonic() + 30
        browser = target.get("browser") or {}
        readonly_paths = {str(item) for item in (browser.get("readonly_post_paths") or [])}
        self.readonly_post_paths = readonly_paths

    def block(self, path: str, reason: str) -> None:
        record = {"path": str(path)[:1000], "reason": reason}
        if record not in self.blocked and len(self.blocked) < 10:
            self.blocked.append(record)

    def route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        path = parsed.path or "/"
        try:
            if not within_scope(self.target, request.url):
                raise BrowserAccessError("请求超出已配置站点或路径范围")
            if request.redirected_from:
                raise BrowserAccessError("未跟随重定向；请核对目标后显式导航")
            permitted_post = request.method == "POST" and (
                (self.login_active and path in self.login_paths)
                or (self.query_active and path in self.readonly_post_paths))
            if request.method not in {"GET", "HEAD"} and not permitted_post:
                raise BrowserAccessError("未授权的提交已阻止；需要在系统配置中明确登录或只读查询接口")
            if request.resource_type in {"image", "media", "font"}:
                route.abort()
                return
            if self.requests >= MAX_REQUESTS or self.bytes >= MAX_SESSION_BYTES or time.monotonic() > self.deadline:
                raise BrowserAccessError("浏览器读取达到本轮时间、请求或流量上限")
            body = request.post_data_buffer
            if body and len(body) > 64 * 1024:
                raise BrowserAccessError("查询或登录请求过大")
            self.requests += 1
            response = route.fetch(max_redirects=0, timeout=REQUEST_SECONDS * 1000)
            if 300 <= response.status < 400:
                location = response.headers.get("location", "")
                candidate = urljoin(request.url, location)
                destination_part = urlsplit(candidate)
                destination = destination_part.path + ("#" + destination_part.fragment if destination_part.fragment else "") \
                    if within_scope(self.target, candidate) else "跨站地址，需单独配置授权"
                self.block(path, "重定向已阻止，请显式打开返回的站内地址")
                route.fulfill(status=409, content_type="text/plain",
                              body=("redirect_blocked: " + destination)[:1500])
                return
            payload = response.body()
            if len(payload) > MAX_RESOURCE_BYTES:
                raise BrowserAccessError("页面资源超过读取上限")
            self.bytes += len(payload)
            headers = {key: value for key, value in response.headers.items()
                       if key.lower() not in {"content-length", "content-encoding", "transfer-encoding", "connection"}}
            route.fulfill(status=response.status, headers=headers, body=payload)
        except BrowserAccessError as exc:
            self.block(path, str(exc))
            route.abort()
        except Exception:
            self.block(path, "读取失败或授权已变化，请重新核对配置")
            route.abort()


class ConnectorSession:
    """受控浏览器会话：独立 profile、headed 默认、只读边界。"""

    def __init__(self, target: dict, credentials: dict | None, *, headless: bool):
        self.target = target
        self._credentials = dict(credentials or {})
        self._secrets = {value for value in self._credentials.values() if value}
        self.headless = headless
        self.started = time.monotonic()
        self.page_id = ""
        self.elements: dict[str, tuple] = {}
        self.observed_url = ""
        self.login_attempts = 0
        self.policy = self.playwright = self.browser = self.context = self.page = None

    def redact(self, value: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            for variant in {secret, quote(secret, safe="")}:
                value = value.replace(variant, "[已隐藏凭据]")
        return str(value)[:24_000]

    def redact_content(self, value):
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, list):
            return [self.redact_content(item) for item in value]
        if isinstance(value, dict):
            return {key: self.redact_content(item) for key, item in value.items()}
        return value

    def remember_secret(self, value) -> None:
        if value and isinstance(value, str) and len(self._secrets) < 200 and len(value) <= 8192:
            self._secrets.add(value)

    def start(self, entry_path: str):
        from playwright.sync_api import sync_playwright

        self.policy = LocalPolicy(self.target)
        self.playwright = sync_playwright().start()
        # A dedicated profile directory keeps this browser isolated from the
        # member's daily browser data; persistent context is the supported way.
        profile_dir = tempfile.mkdtemp(prefix="ontology-investigation-")
        self.context = self.playwright.chromium.launch_persistent_context(
            profile_dir, headless=self.headless, timeout=20_000,
            accept_downloads=False, service_workers="block",
            viewport={"width": 1366, "height": 900},
            args=["--disable-background-networking", "--disable-quic",
                  "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])
        self.context.set_default_timeout(5000)
        self.context.set_default_navigation_timeout(25_000)
        self.context.route("**/*", self.policy.route)
        self.context.route_web_socket("**/*", lambda _socket: None)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.context.on("page", lambda page: page.close() if page != self.page else None)
        self.page.on("dialog", lambda dialog: dialog.dismiss())
        self.page.on("download", lambda download: download.cancel())
        self.page.add_init_script(
            "Object.defineProperty(window, 'RTCPeerConnection', { value: undefined });"
            "Object.defineProperty(window, 'webkitRTCPeerConnection', { value: undefined });")
        return self.navigate(entry_path)

    def prepare(self):
        if self.page is None or time.monotonic() - self.started > 900:
            raise BrowserAccessError("本轮浏览器会话已失效，请重新打开系统")
        self.policy.deadline = time.monotonic() + 30
        self.policy.blocked = []
        self.policy.login_active = self.policy.query_active = False

    def settle(self):
        self.page.wait_for_timeout(200)
        try:
            self.page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        for cookie in self.context.cookies():
            if cookie.get("value"):
                self.remember_secret(cookie["value"])

    def navigate(self, path: str):
        self.prepare()
        url = self.target.get("base_url", "") + path
        if not path.startswith("/") or path.startswith("//") or not within_scope(self.target, url):
            raise BrowserAccessError("只能导航到已配置系统的允许页面")
        if _MUTATION.search(unquote(urlsplit(path).path)):
            raise BrowserAccessError("该地址疑似业务写操作，请提供只读调查入口")
        try:
            self.page.goto(url, wait_until="domcontentloaded")
            self.settle()
        except Exception as exc:
            raise BrowserAccessError("页面未能打开；请核对网址、访问范围或登录要求") from exc
        return self.snapshot()

    def snapshot(self, offset=0):
        if not within_scope(self.target, self.page.url):
            raise BrowserAccessError("当前页面不在授权系统范围内，请重新打开入口")
        self.page_id, self.observed_url = uuid.uuid4().hex, self.page.url
        self.elements = {}
        controls, links, fields = [], [], []
        locator = self.page.locator("a,button,input:not([type=hidden]),select,textarea,[role=button],[role=tab]")
        count = locator.count()
        for index in range(offset, min(count, offset + MAX_ELEMENTS)):
            element = locator.nth(index).element_handle()
            if element is None or not element.is_visible():
                continue
            description = element.evaluate(_ELEMENT_DESCRIPTION)
            ref = f"{self.page_id[:12]}_{index}"
            self.elements[ref] = (element, description)
            safe = self.redact_content(description)
            controls.append({"ref": ref, **safe})
            if description["tag"] in {"input", "textarea", "select"} and len(fields) < 40:
                fields.append(safe["name"][:200])
            if description["href"].startswith("/") and len(links) < 20:
                links.append(self.redact(description["href"])[:2048])
        text = self.redact(self.page.locator("body").inner_text(timeout=5000))[:16_000]
        title = self.redact(self.page.title())[:200]
        password_present = self.page.locator('input[type="password"]:visible').count() > 0
        status = "login_required" if password_present else "redirect_blocked" if text.startswith("redirect_blocked:") else "observed"
        if self.policy.blocked and not text.strip():
            status = "javascript_required"
        observation = {"target_key": self.target.get("key", ""), "url": self.redact(self.page.url)[:2048], "title": title,
            "status": status, "text": text, "read_only": True, "visible_fields": fields, "allowed_links": links,
            "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "limitations": ["真实浏览器当前可见页面的有界快照；不代表隐藏记录、完整样本或后台流程已获验证。"]}
        tables = self.page.locator("table").evaluate_all(
            "tables => tables.slice(0,5).map(t => [...t.rows].slice(0,51).map(r => [...r.cells].slice(0,20).map(c => c.innerText.slice(0,200))))")
        content = {"observation": observation, "page_id": self.page_id, "elements": controls,
            "login_configured": bool(self._credentials),
            "login_instructions": "凭据已由服务端配置；调用 login_business_system 并传控件引用，不要询问或传入账号密码。"
                if self._credentials else "未配置网页登录；若需要登录，请引导专家使用业务系统配置窗口。",
            "tables": self.redact_content(tables) if len(json.dumps(tables)) < 24_000 else [],
            "tables_truncated": len(json.dumps(tables)) >= 24_000,
            "blocked_requests": list(self.policy.blocked), "elements_truncated": count > offset + MAX_ELEMENTS,
            "next_offset": offset + MAX_ELEMENTS if count > offset + MAX_ELEMENTS and offset + MAX_ELEMENTS <= 5000 else None,
            "embedded_frames": len(self.page.frames) - 1}
        # 登录表单永不截图；只有完全观察页才生成截图。
        if status == "observed":
            try:
                image = self.page.screenshot(type="jpeg", quality=55)
                if 0 < len(image) <= MAX_SCREENSHOT_BYTES:
                    content["screenshot"] = base64.b64encode(image).decode("ascii")
            except Exception:
                pass
        return content

    def element(self, page_id: str, ref: str):
        if page_id != self.page_id or ref not in self.elements or self.page.url != self.observed_url:
            raise BrowserAccessError("控件引用已过期，请先重新查看页面")
        element, description = self.elements[ref]
        if not element.is_visible() or element.evaluate(_ELEMENT_DESCRIPTION) != description:
            raise BrowserAccessError("页面控件已变化，请先重新查看页面")
        return element, description

    def fill(self, page_id: str, ref: str, value: str):
        self.prepare()
        element, description = self.element(page_id, ref)
        if description["type"].lower() == "password" or self.redact(value) != value:
            raise BrowserAccessError("凭据只能通过专用登录工具注入")
        if description["tag"] == "select":
            element.select_option(value=value)
        elif description["tag"] in {"input", "textarea"}:
            element.fill(value)
        else:
            raise BrowserAccessError("该控件不能填写查询条件")
        self.settle()
        return self.snapshot()

    def click(self, page_id: str, ref: str, intent: str):
        self.prepare()
        element, description = self.element(page_id, ref)
        if description["tag"] not in {"button", "a", "input"} and description["role"] not in {"button", "tab"}:
            raise BrowserAccessError("请选择页面中的导航或查询控件")
        if _MUTATION.search(description["name"] + " " + description["href"]):
            raise BrowserAccessError("该控件疑似业务写操作，调查工具不能执行")
        self.policy.query_active = intent == "query"
        try:
            element.click()
            self.settle()
        finally:
            self.policy.query_active = False
        return self.snapshot()

    def login(self, page_id: str, username_ref: str, password_ref: str, submit_ref: str):
        self.prepare()
        if not self._credentials:
            raise BrowserAccessError("该系统没有有效网页登录授权，请在业务系统中配置账号")
        if self.login_attempts >= MAX_LOGIN_ATTEMPTS:
            raise BrowserAccessError("本轮登录尝试已达上限，请向专家核对账号或验证码")
        username, user_info = self.element(page_id, username_ref)
        password, pass_info = self.element(page_id, password_ref)
        submit, submit_info = self.element(page_id, submit_ref)
        if user_info["tag"] != "input" or pass_info["type"].lower() != "password" or submit_info["tag"] not in {"button", "input"}:
            raise BrowserAccessError("请选择实际账号框、密码框和登录按钮")
        action = password.evaluate("el => el.form ? el.form.action : null")
        browser_cfg = self.target.get("browser") or {}
        self.policy.login_paths = {str(item) for item in (browser_cfg.get("login_paths") or [])}
        if action and within_scope(self.target, action):
            self.policy.login_paths.add(urlsplit(action).path or "/")
        self.policy.login_active = True
        self.login_attempts += 1
        try:
            username.fill(self._credentials.get("username", ""))
            password.fill(self._credentials.get("secret", ""))
            submit.click()
            self.settle()
        finally:
            self.policy.login_active = False
        return self.snapshot()

    def close(self):
        for resource in (self.context, self.browser):
            if resource:
                try:
                    resource.close()
                except Exception:
                    pass
        if self.playwright:
            try:
                self.playwright.stop()
            except Exception:
                pass
        self._credentials.clear()
        self._secrets.clear()
        self.page = self.context = self.browser = self.policy = self.playwright = None


class ConnectorRuntime:
    """协议分发：一条 WS 上顺序执行平台下发的调查操作。"""

    def __init__(self, *, headless: bool):
        self.headless = headless
        self.session: ConnectorSession | None = None
        self._lock = threading.Lock()

    def handle(self, op: str, args: dict) -> dict:
        with self._lock:
            if op == "browser.open":
                if self.session:
                    self.session.close()
                target = args.get("target") or {}
                if not isinstance(target, dict) or not target.get("base_url"):
                    raise BrowserAccessError("连接器收到的目标系统配置无效")
                self.session = ConnectorSession(target, args.get("credentials"), headless=self.headless)
                return self.session.start(str((target.get("browser") or {}).get("entry_path") or "/"))
            if self.session is None:
                raise BrowserAccessError("本轮尚未打开该系统，请重新打开")
            if op == "browser.prepare":
                self.session.prepare()
                self.session.settle()
                return {}
            if op == "browser.navigate":
                return self.session.navigate(str(args.get("path") or "/"))
            if op == "browser.snapshot":
                return self.session.snapshot(int(args.get("offset") or 0))
            if op == "browser.fill":
                return self.session.fill(str(args.get("page_id") or ""), str(args.get("ref") or ""), str(args.get("value") or ""))
            if op == "browser.click":
                return self.session.click(str(args.get("page_id") or ""), str(args.get("ref") or ""), str(args.get("intent") or ""))
            if op == "browser.login":
                return self.session.login(str(args.get("page_id") or ""), str(args.get("username_ref") or ""),
                    str(args.get("password_ref") or ""), str(args.get("submit_ref") or ""))
            if op == "browser.close":
                self.session.close()
                self.session = None
                return {}
        raise BrowserAccessError(f"连接器不支持的操作：{op}")

    def shutdown(self):
        with self._lock:
            if self.session:
                try:
                    self.session.close()
                except Exception:
                    pass
                self.session = None


def ws_url(server: str) -> str:
    base = server.rstrip("/")
    if base.startswith("https://"):
        return base.replace("https://", "wss://", 1) + "/api/distillation-connector/ws"
    if base.startswith("http://"):
        return base.replace("http://", "ws://", 1) + "/api/distillation-connector/ws"
    raise SystemExit("--server 必须以 http:// 或 https:// 开头")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="业务蒸馏·本机调查连接器")
    parser.add_argument("--server", required=True, help="平台地址，例如 https://platform.example.com")
    parser.add_argument("--token", required=True, help="平台生成的一次性连接令牌")
    parser.add_argument("--headless", action="store_true", help="无头模式运行（默认弹出可见浏览器窗口）")
    args = parser.parse_args(argv)

    try:
        from websockets.sync.client import connect
    except ImportError:
        print("缺少依赖：请先执行  python -m pip install \"websockets>=12,<16\" \"playwright>=1.40,<2\"  并  python -m playwright install chromium", file=sys.stderr)
        return 2

    url = ws_url(args.server)
    runtime = ConnectorRuntime(headless=args.headless)
    print(f"正在连接平台 {args.server} …（令牌仅在命令行本地使用，平台只保存其哈希）")
    try:
        with connect(url, additional_headers={"Authorization": f"Bearer {args.token}"},
                     open_timeout=15, close_timeout=5, max_size=8 * 1024 * 1024) as socket:
            socket.send(json.dumps({"type": "hello", "connector_version": CONNECTOR_VERSION,
                "protocol_version": PROTOCOL_VERSION, "platform": f"{host_platform.system()} {host_platform.release()}",
                "capabilities": ["browser"]}))
            accepted = json.loads(socket.recv(timeout=15))
            if accepted.get("type") != "accepted":
                print("平台拒绝了连接器握手，请重新生成命令。", file=sys.stderr)
                return 1
            print("已连接。本机现在作为业务调查浏览器执行端；关闭本窗口或 Ctrl+C 即断开。")
            while True:
                try:
                    raw = socket.recv(timeout=20)
                except TimeoutError:
                    socket.send(json.dumps({"type": "ping"}))
                    continue
                message = json.loads(raw)
                if message.get("type") != "request":
                    continue
                request_id, op, op_args = message.get("id"), message.get("op"), message.get("args") or {}
                print(f"· 执行调查操作 {op} …")
                try:
                    result = runtime.handle(op, op_args)
                    reply = {"type": "response", "id": request_id, "ok": True, "result": result}
                except BrowserAccessError as exc:
                    reply = {"type": "response", "id": request_id, "ok": False, "error": str(exc)}
                except Exception as exc:  # noqa: BLE001 - 本地控制台仅记录类型便于诊断
                    print(f"  操作异常类型: {type(exc).__name__}", flush=True)
                    reply = {"type": "response", "id": request_id, "ok": False, "error": "本机浏览器操作失败，请稍后重试或重新打开系统"}
                socket.send(json.dumps(reply, ensure_ascii=False))
    except KeyboardInterrupt:
        print("已手动断开。")
    except Exception as exc:  # noqa: BLE001 - 面向成员的恒定提示
        print(f"连接已断开（{type(exc).__name__}）。令牌可能已过期或被撤销，请在平台重新生成命令。", file=sys.stderr)
    finally:
        runtime.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
