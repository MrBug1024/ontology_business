"""Read only explicitly allowed pages and return bounded, untrusted observations."""
from __future__ import annotations

import hashlib
import base64
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from ..distillation_target_schemas import TargetSystem, canonical_page_path
from . import readonly_http_adapter, release_service


class TargetReadError(ValueError):
    """The caller can expose this bounded, credential-free message."""


class _PageObservation(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.in_title = False
        self.title: list[str] = []
        self.text: list[str] = []
        self.links: list[str] = []
        self.fields: list[str] = []
        self.password_field = False
        self.has_script = False

    def handle_starttag(self, tag: str, attributes: list[tuple[str, str | None]]) -> None:
        attrs = dict(attributes)
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self.hidden_depth += 1
            self.has_script = self.has_script or tag == "script"
        if self.hidden_depth:
            return
        if tag == "title":
            self.in_title = True
        if tag == "a" and attrs.get("href") and len(self.links) < 200:
            self.links.append(str(attrs["href"]))
        if tag == "input":
            kind = str(attrs.get("type") or "text").casefold()
            self.password_field = self.password_field or kind == "password"
            if kind not in {"hidden", "password"} and len(self.fields) < 40:
                label = attrs.get("aria-label") or attrs.get("placeholder")
                if label:
                    self.fields.append(str(label)[:200])

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg", "template"} and self.hidden_depth:
            self.hidden_depth -= 1
        if tag == "title":
            self.in_title = False

    def handle_data(self, value: str) -> None:
        if self.hidden_depth:
            return
        if self.in_title:
            self.title.append(value)
        elif value.strip():
            self.text.append(value.strip())


def _safe_text(value: str, limit: int) -> str:
    result = release_service.safe_snapshot_content({"content": value[:limit]})
    content = result.get("content")
    return content if isinstance(content, str) else "内容包含疑似凭据，已排除"


def _public_json_value(value):
    """Remove secret fields after redaction so serialized output stays safe text."""
    if release_service._is_marker(value):
        return "已隐藏敏感内容"
    if isinstance(value, dict):
        return {key: _public_json_value(child) for key, child in value.items()
                if not release_service._is_marker(child)}
    if isinstance(value, list):
        return [_public_json_value(child) for child in value]
    return value


def _allowed_links(target: TargetSystem, path: str, links: list[str]) -> list[str]:
    allowed = []
    for link in links:
        try:
            parsed = urlsplit(urljoin(target.base_url + path, link))
        except ValueError:
            continue
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            continue
        if f"{parsed.scheme}://{parsed.netloc}" != target.base_url:
            continue
        try:
            candidate = canonical_page_path(parsed.path or "/")
        except ValueError:
            continue
        if candidate in target.allowed_paths and candidate not in allowed:
            allowed.append(candidate)
    return allowed[:20]


def read_target_system(target: TargetSystem, page_path: str, *, authorization: str | None = None) -> dict:
    """Caller authorizes and snapshots a project before ending its read transaction."""
    if not target.enabled:
        raise TargetReadError("目标系统调查已停用")
    if (target.access_mode == "authorized_readonly") != (authorization is not None):
        raise TargetReadError("受保护系统必须先取得有效只读授权；公开页面不接受凭据")
    try:
        path = canonical_page_path(page_path)
    except ValueError as exc:
        raise TargetReadError("请使用目标系统中明确允许的页面路径") from exc
    if path not in target.allowed_paths:
        raise TargetReadError("该页面不在人工配置的允许调查范围内")
    url = target.base_url + path
    try:
        document = readonly_http_adapter.read_document(url, authorization=authorization) if authorization else readonly_http_adapter.read_document(url)
    except readonly_http_adapter.ReadonlyHttpError as exc:
        raise TargetReadError(str(exc)) from exc
    content = document.body.decode("utf-8", errors="replace")
    secret_values = []
    if authorization:
        # A remote API can echo the credential without a recognizable field name.
        # Remove exact secret forms before HTML/JSON or model-visible processing.
        encoded = authorization.split(" ", 1)[1]
        values = [authorization, encoded]
        if authorization.startswith("Basic "):
            decoded = base64.b64decode(encoded).decode()
            values.extend([decoded, *decoded.split(":", 1)])
        for value in sorted(set(values), key=len, reverse=True):
            if value:
                content = content.replace(value, "[已隐藏凭据]")
        secret_values = [value for value in values if value]

    def redact(value):
        if isinstance(value, str):
            for secret in sorted(secret_values, key=len, reverse=True):
                value = value.replace(secret, "[已隐藏凭据]")
            return value
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            return {redact(key): redact(item) for key, item in value.items()}
        return value
    page = _PageObservation()
    json_redacted = False
    if document.content_type in {"text/html", "application/xhtml+xml"}:
        page.feed(content)
        content = redact("\n".join(page.text))
    elif document.content_type == "application/json":
        try:
            parsed = redact(json.loads(content))
            sanitized = release_service.safe_snapshot_content({"response": parsed})
            json_redacted = sanitized["response"] != parsed
            content = json.dumps(_public_json_value(sanitized["response"]), ensure_ascii=False, allow_nan=False)
        except (ValueError, RecursionError) as exc:
            raise TargetReadError("目标返回了无法安全读取的 JSON，请使用脱敏导出资料") from exc
    status = "observed"
    if document.status_code in {401, 403} or page.password_field:
        status = "login_required"
    elif 300 <= document.status_code < 400:
        status = "redirect_blocked"
    elif not content.strip() and page.has_script:
        status = "javascript_required"
    limitations = ["本次仅匿名 GET 读取一页；未执行脚本、登录、点击或表单提交。",
                   "页面文字是待核对证据，不能证明后台流程、实际执行或业务结果。"]
    if authorization:
        limitations[0] = "本次使用明确授权的专用凭据 GET 读取一个允许页面/接口；未执行脚本、表单登录、点击或业务写入。"
    if json_redacted:
        limitations.append("JSON 中疑似凭据字段已排除，其余业务内容仍需人工核对。")
    if status != "observed":
        limitations.append("当前页面未完成业务调查；尚未配置安全登录或浏览器适配器，可上传登录后导出的资料继续。")
    if len(content) > 16_000:
        limitations.append("页面正文超过本次上限，仅返回前 16000 字符。")
    return {"target_key": target.key, "url": url, "status": status, "read_only": True,
            "title": _safe_text(redact(re.sub(r"\s+", " ", " ".join(page.title))), 200),
            "text": _safe_text(redact(content), 16_000), "visible_fields": [_safe_text(redact(label), 200) for label in page.fields],
            "allowed_links": _allowed_links(target, path, page.links),
            "content_sha256": hashlib.sha256(document.body).hexdigest(),
            "retrieved_at": datetime.now(timezone.utc).isoformat(), "limitations": limitations}
