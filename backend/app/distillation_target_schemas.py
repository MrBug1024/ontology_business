"""Explicit network scope for anonymous, read-only target-system observation."""
from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import quote, unquote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def canonical_page_path(value: str) -> str:
    if not value.startswith("/") or value.startswith("//") or len(value) > 1000:
        raise ValueError("允许页面必须是以 / 开头的精确路径")
    decoded = unquote(value, errors="strict")
    if (decoded.startswith("//") or any(char in decoded for char in "?#\\%")
            or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
            or any(part in {".", ".."} for part in decoded.split("/"))):
        raise ValueError("页面路径不能包含查询、片段、转义跳转或凭据")
    return quote(decoded, safe="/:@-._~!$&'()*+,;=")


class BrowserScope(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    entry_path: str = Field(default="/", min_length=1, max_length=1500)
    login_paths: list[str] = Field(default_factory=list, max_length=10)
    readonly_post_paths: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("entry_path")
    @classmethod
    def validate_entry(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc or parsed.query or not value.startswith("/"):
            raise ValueError("入口使用站内路径，可含页面路由片段，不含查询或凭据")
        canonical_page_path(parsed.path or "/")
        if any(ord(char) < 32 or char in "\\\\" for char in value):
            raise ValueError("入口路径无效")
        return value

    @field_validator("login_paths", "readonly_post_paths")
    @classmethod
    def validate_endpoints(cls, values: list[str]) -> list[str]:
        paths = [canonical_page_path(value) for value in values]
        if len(set(paths)) != len(paths):
            raise ValueError("访问路径不能重复")
        return paths


class TargetSystem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    base_url: Annotated[str, Field(min_length=1, max_length=500)]
    purpose: Annotated[str, Field(min_length=1, max_length=4000)]
    allowed_paths: Annotated[list[str], Field(min_length=1, max_length=20)]
    notes: Annotated[str, Field(max_length=4000)] = ""
    access_mode: Literal["anonymous_readonly", "authorized_readonly"] = "anonymous_readonly"
    enabled: bool = True
    browser: BrowserScope | None = None

    @model_validator(mode="after")
    def validate_transport(self):
        if self.base_url.startswith("http://") and self.browser is None:
            raise ValueError("静态页面调查仅支持 HTTPS；内网 HTTP 网站请配置受控浏览器并由部署允许")
        return self

    @field_validator("base_url")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        if any(ord(char) <= 32 or ord(char) == 127 for char in value) or any(char in value for char in "\\?#"):
            raise ValueError("目标系统必须使用无凭据的 HTTPS 站点地址")
        parsed = urlsplit(value)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise ValueError("目标系统地址只填写 HTTPS 站点；页面范围请填写到允许路径")
        try:
            url = httpx.URL(value)
            if url.port == 0:
                raise ValueError("invalid port")
        except (httpx.InvalidURL, ValueError) as exc:
            raise ValueError("目标系统地址无效") from exc
        return str(url.copy_with(path="")).rstrip("/")

    @field_validator("allowed_paths")
    @classmethod
    def validate_paths(cls, values: list[str]) -> list[str]:
        paths = [canonical_page_path(value.strip()) for value in values]
        if len(paths) != len(set(paths)):
            raise ValueError("允许页面路径不能重复")
        return paths
