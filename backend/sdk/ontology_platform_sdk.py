"""A deliberately small Python client for ``/api/external/v1``.

It only targets the read-only v1 boundary.  Side-effecting Actions and
workflows remain intentionally absent until their approval/idempotency contract
can be made equally explicit for third-party callers.
"""
from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import quote, urlsplit

import httpx


class ExternalApiError(RuntimeError):
    """A safe error that never includes the API key or response body."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OntologyPlatformClient:
    """Synchronous client for scenario discovery and authorized object reads."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: float = 15.0,
        http_client: Any | None = None,
        allow_insecure_http: bool = False,
    ) -> None:
        cleaned_url = str(base_url or "").strip().rstrip("/")
        parsed = urlsplit(cleaned_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url 必须是带主机名的 http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url 不能包含凭据、查询参数或片段")
        if parsed.scheme == "http":
            if not allow_insecure_http:
                raise ValueError("base_url 默认必须使用 HTTPS；本地联调需显式 allow_insecure_http=True")
            if not _is_loopback_host(parsed.hostname):
                raise ValueError("明文 HTTP 仅允许 localhost 或回环地址")
        if not str(api_key or "").strip():
            raise ValueError("api_key 不能为空")
        self._base_url = cleaned_url
        self._api_key = str(api_key).strip()
        self._client = http_client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = http_client is None

    def __enter__(self) -> "OntologyPlatformClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
            self._owns_client = False

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    def _request(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        try:
            response = self._client.request(
                "GET",
                self._url(path),
                params=params,
                headers={"X-API-Key": self._api_key, "Accept": "application/json"},
                # A caller may pass an httpx client configured to follow
                # redirects.  Never permit that setting to forward a bearer
                # credential to an arbitrary Location target.
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise ExternalApiError("外部 API 请求失败") from exc
        if 300 <= response.status_code < 400:
            raise ExternalApiError(
                f"外部 API 返回 HTTP {response.status_code}", status_code=response.status_code
            )
        if response.status_code >= 400:
            raise ExternalApiError(
                f"外部 API 返回 HTTP {response.status_code}", status_code=response.status_code
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ExternalApiError("外部 API 返回了无效 JSON") from exc

    def identity(self) -> dict[str, Any]:
        return self._request("identity")

    def list_scenarios(self) -> list[dict[str, Any]]:
        payload = self._request("scenarios")
        if not isinstance(payload, list):
            raise ExternalApiError("外部 API 返回了无效场景列表")
        return payload

    def list_entities(self, scenario_id: str) -> list[dict[str, Any]]:
        payload = self._request(f"scenarios/{quote(str(scenario_id), safe='')}/entities")
        if not isinstance(payload, list):
            raise ExternalApiError("外部 API 返回了无效实体列表")
        return payload

    def list_objects(
        self,
        scenario_id: str,
        *,
        query: str = "",
        entity_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "q": query,
            "limit": limit,
            "offset": offset,
        }
        if entity_id is not None:
            params["entity_id"] = entity_id
        payload = self._request(
            f"scenarios/{quote(str(scenario_id), safe='')}/objects", params=params
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise ExternalApiError("外部 API 返回了无效对象分页")
        return payload

    def get_object(self, scenario_id: str, object_id: str) -> dict[str, Any]:
        payload = self._request(
            "scenarios/"
            f"{quote(str(scenario_id), safe='')}/objects/{quote(str(object_id), safe='')}"
        )
        if not isinstance(payload, dict):
            raise ExternalApiError("外部 API 返回了无效对象")
        return payload


def _is_loopback_host(hostname: str) -> bool:
    """Return whether an HTTP endpoint is definitely local to this machine.

    ``urlsplit`` removes IPv6 brackets for us.  ``localhost`` is intentionally
    accepted as the conventional development endpoint; all numeric addresses
    must be recognized by ``ipaddress`` as loopback rather than merely sharing
    a string prefix with ``127.0.0.1``.
    """
    normalized = str(hostname or "").strip().rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False
