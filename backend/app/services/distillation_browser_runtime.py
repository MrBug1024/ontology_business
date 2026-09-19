"""Ephemeral, per-claim browser. Durable authority stays in the conversation lease."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from threading import BoundedSemaphore
from urllib.parse import quote, unquote, urlsplit

from ..config import get_settings
from ..distillation_target_schemas import TargetSystem
from .distillation_browser_network import BrowserAccessError, BrowserNetwork, within_scope
from .distillation_target_service import _safe_text


MAX_ELEMENTS = 80
MAX_SESSION_SECONDS = 900
MAX_LOGIN_ATTEMPTS = 2
BROWSER_ENV_KEYS = frozenset({"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "USERPROFILE", "LOCALAPPDATA", "HOME", "LANG", "DISPLAY", "XAUTHORITY"})
# Process-local capacity controls resources only; DB lease/auth control correctness.
_SLOTS = BoundedSemaphore(get_settings().distillation_browser_max_sessions)
_MUTATION = re.compile(r"删除|新增|保存|作废|支付|发送|审批|停用|启用|导入|上传|delete|remove|save|create|approve|pay|send|upload|logout|退出", re.I)
_ELEMENT_DESCRIPTION = """el => ({
  tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || '',
  role: el.getAttribute('role') || '',
  name: (el.getAttribute('aria-label') || (el.labels && [...el.labels].map(l => l.innerText).join(' ')) || el.innerText || el.getAttribute('placeholder') || el.getAttribute('name') || '').slice(0, 200),
  href: el.tagName === 'A' ? el.getAttribute('href') || '' : '',
  options: el.tagName === 'SELECT' ? [...el.options].slice(0, 30).map(o => ({label:o.label.slice(0,100), value:o.value.slice(0,100)})) : []
})"""


class InvestigationBrowser:
    def __init__(self, target: TargetSystem, authorize, credentials=None):
        self.target, self.authorize = target, authorize
        self._credentials = dict(credentials or {})
        self._secrets = set(value for value in self._credentials.values() if value)
        self.started = time.monotonic()
        self.page_id = ""
        self.elements = {}
        self.observed_url = ""
        self.login_attempts = 0
        self.network = self.playwright = self.browser = self.context = self.page = None
        self.has_slot = False

    def redact(self, value: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            for variant in {secret, quote(secret, safe="")}:
                value = value.replace(variant, "[已隐藏凭据]")
        return _safe_text(value, 24_000)

    def remember_secret(self, value: str):
        if value and len(self._secrets) < 200 and len(value) <= 8192:
            self._secrets.add(value)

    def redact_content(self, value):
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, list):
            return [self.redact_content(item) for item in value]
        if isinstance(value, dict):
            return {key: self.redact_content(item) for key, item in value.items()}
        return value

    def start(self):
        if not get_settings().distillation_browser_enabled:
            raise BrowserAccessError("当前部署尚未启用浏览器调查，请管理员安装浏览器运行时并启用")
        if not _SLOTS.acquire(blocking=False):
            raise BrowserAccessError("浏览器调查繁忙，请稍后继续")
        self.has_slot = True
        stage = "authorization"
        try:
            from playwright.sync_api import sync_playwright

            self.authorize()
            stage = "network_policy"
            self.network = BrowserNetwork(self.target, self.authorize, self.redact, self.remember_secret)
            stage = "driver_start"
            self.playwright = sync_playwright().start()
            stage = "chromium_start"
            self.browser = self.playwright.chromium.launch(headless=True, chromium_sandbox=True, timeout=20_000,
                env={key: value for key, value in os.environ.items() if key.upper() in BROWSER_ENV_KEYS},
                args=["--disable-background-networking", "--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])
            self.context = self.browser.new_context(accept_downloads=False, service_workers="block")
            stage = "context_setup"
            self.context.set_default_timeout(5000)
            self.context.set_default_navigation_timeout(25_000)
            self.context.route("**/*", self.network.route)
            # An intercepted socket never reaches a server unless explicitly
            # connected. Avoid a synchronous close RPC inside this callback:
            # real Vite/HMR pages can deadlock Playwright's sync dispatcher.
            self.context.route_web_socket("**/*", lambda _socket: None)
            self.page = self.context.new_page()
            self.context.on("page", lambda page: page.close() if page != self.page else None)
            self.page.on("dialog", lambda dialog: dialog.dismiss())
            self.page.on("download", lambda download: download.cancel())
            self.page.add_init_script("Object.defineProperty(window, 'RTCPeerConnection', { value: undefined }); Object.defineProperty(window, 'webkitRTCPeerConnection', { value: undefined });")
        except Exception as exc:
            logging.getLogger(__name__).warning("Investigation browser start failed stage=%s error_type=%s", stage, type(exc).__name__)
            self.close()
            if isinstance(exc, BrowserAccessError):
                raise
            raise BrowserAccessError("浏览器启动失败，请核对 Chromium 安装和部署配置") from exc

    def prepare(self):
        self.authorize()
        if self.page is None or time.monotonic() - self.started > MAX_SESSION_SECONDS:
            raise BrowserAccessError("本轮浏览器会话已失效，请重新打开系统")
        self.network.deadline = time.monotonic() + 30
        self.network.blocked = []
        self.network.login_active = self.network.query_active = False

    def navigate(self, path: str):
        self.prepare()
        url = self.target.base_url + path
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

    def settle(self):
        # Bounded wait includes JS-rendered data and login requests. Never waits
        # indefinitely on applications with continuous polling.
        self.page.wait_for_timeout(200)
        try:
            self.page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        self.authorize()
        for cookie in self.context.cookies():
            if cookie.get("value"):
                self.remember_secret(cookie["value"])

    def snapshot(self, offset=0):
        self.authorize()
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
        if self.network.blocked and not text.strip():
            status = "javascript_required"
        observation = {"target_key": self.target.key, "url": self.redact(self.page.url)[:2048], "title": title,
            "status": status, "text": text, "read_only": True, "visible_fields": fields, "allowed_links": links,
            "content_sha256": hashlib.sha256(text.encode()).hexdigest(), "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "limitations": ["真实浏览器当前可见页面的有界快照；不代表隐藏记录、完整样本或后台流程已获验证。"]}
        tables = self.page.locator("table").evaluate_all("""tables => tables.slice(0,5).map(t => [...t.rows].slice(0,51).map(r => [...r.cells].slice(0,20).map(c => c.innerText.slice(0,200))))""")
        return {"observation": observation, "page_id": self.page_id, "elements": controls,
            "login_configured": bool(self._credentials),
            "login_instructions": "凭据已由服务端配置；调用 login_business_system 并传控件引用，不要询问或传入账号密码。"
                if self._credentials else "未配置网页登录；若需要登录，请引导专家使用业务系统配置窗口。",
            "tables": self.redact_content(tables) if len(json.dumps(tables)) < 24_000 else [],
            "tables_truncated": len(json.dumps(tables)) >= 24_000,
            "blocked_requests": list(self.network.blocked), "elements_truncated": count > offset + MAX_ELEMENTS,
            "next_offset": offset + MAX_ELEMENTS if count > offset + MAX_ELEMENTS and offset + MAX_ELEMENTS <= 5000 else None,
            "embedded_frames": len(self.page.frames) - 1}

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
        self.network.query_active = intent == "query"
        try:
            element.click()
            self.settle()
        finally:
            self.network.query_active = False
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
        self.network.login_paths = set(self.target.browser.login_paths)
        if action and within_scope(self.target, action):
            self.network.login_paths.add(urlsplit(action).path or "/")
        self.network.login_active = True
        self.login_attempts += 1
        try:
            username.fill(self._credentials["username"])
            password.fill(self._credentials["secret"])
            submit.click()
            self.settle()
        finally:
            self.network.login_active = False
        return self.snapshot()

    def close(self):
        for resource in (self.context, self.browser, self.network):
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
        self.page = self.context = self.browser = self.network = self.playwright = None
        if self.has_slot:
            _SLOTS.release()
            self.has_slot = False


class BrowserTurn:
    """One transient session; no sessions, cookies or credentials in checkpoints."""
    def __init__(self, authorize):
        self.authorize, self.session = authorize, None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        if self.session:
            self.session.close()

    def open(self, target: TargetSystem, credentials):
        if self.session:
            self.session.close()
        self.session = InvestigationBrowser(target, lambda: self.authorize(target), credentials)
        self.session.start()
        return self.session.navigate(target.browser.entry_path)

    def current(self, target_key):
        if not self.session or self.session.target.key != target_key:
            raise BrowserAccessError("本轮尚未打开该系统，或进程恢复后会话已失效，请重新打开")
        return self.session
