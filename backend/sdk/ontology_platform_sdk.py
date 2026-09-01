"""Dependency-light clients for the versioned external API.

``OntologyPlatformClient`` preserves the read-only v1 contract.
``CapabilityClient`` wraps the v2 capability protocol without implementing any
business behavior locally.
"""
from __future__ import annotations

import ipaddress
from typing import Any, Literal, Self
from urllib.parse import quote, urlsplit

import httpx


CapabilityKind = Literal["function", "action", "rule", "workflow"]
_CAPABILITY_KINDS = frozenset(("function", "action", "rule", "workflow"))


def _capability_kind(value: str) -> CapabilityKind:
    kind = str(value or "").strip()
    if kind not in _CAPABILITY_KINDS:
        raise ValueError("kind 必须是 function、action、rule 或 workflow")
    return kind  # type: ignore[return-value]


class ExternalApiError(RuntimeError):
    """A safe error that never includes the API key or response body."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _ExternalHttpClient:
    """Shared credential-safe HTTP transport for one external API version."""

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

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
            self._owns_client = False

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        form_data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = self._client.request(
                method,
                self._url(path),
                params=params,
                json=json_body,
                data=form_data,
                files=files,
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

class OntologyPlatformClient(_ExternalHttpClient):
    """Synchronous client for v1 scenario discovery and authorized object reads."""

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


class CapabilityClient(_ExternalHttpClient):
    """Synchronous client for ``/api/external/v2`` capability endpoints."""

    def list_scenarios(self) -> list[dict[str, Any]]:
        payload = self._request("scenarios")
        if not isinstance(payload, list):
            raise ExternalApiError("外部 API 返回了无效场景列表")
        return payload

    def list_capabilities(
        self,
        scenario_id: str,
        *,
        environment: str = "prod",
    ) -> list[dict[str, Any]]:
        payload = self._request(
            f"scenarios/{quote(str(scenario_id), safe='')}/capabilities",
            params={"environment": environment},
        )
        if not isinstance(payload, list):
            raise ExternalApiError("外部 API 返回了无效能力列表")
        return payload

    def upload_invocation_attachment(
        self,
        filename: str,
        content: bytes,
        *,
        content_type: str = "application/octet-stream",
        name: str | None = None,
        description: str = "",
        expires_in_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Upload bytes as a temporary managed asset for an ``asset_version_id`` input."""
        if not isinstance(content, bytes) or not content:
            raise ValueError("content 必须是非空 bytes")
        normalized_filename = str(filename or "").strip()
        if not normalized_filename:
            raise ValueError("filename 不能为空")
        form_data: dict[str, Any] = {"description": description}
        if name is not None:
            form_data["name"] = name
        if expires_in_seconds is not None:
            form_data["expires_in_seconds"] = str(expires_in_seconds)
        payload = self._request(
            "assets/upload",
            method="POST",
            form_data=form_data,
            files={"file": (normalized_filename, content, content_type)},
        )
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("version"), dict)
            or not isinstance(payload["version"].get("id"), str)
        ):
            raise ExternalApiError("外部 API 返回了无效附件版本")
        return payload

    def get_capability(
        self,
        scenario_id: str,
        kind: CapabilityKind,
        key: str,
        *,
        environment: str = "prod",
    ) -> dict[str, Any]:
        normalized_kind = _capability_kind(kind)
        payload = self._request(
            "scenarios/"
            f"{quote(str(scenario_id), safe='')}/capabilities/"
            f"{quote(normalized_kind, safe='')}/{quote(str(key), safe='')}",
            params={"environment": environment},
        )
        if not isinstance(payload, dict):
            raise ExternalApiError("外部 API 返回了无效能力契约")
        return payload

    def list_managed_input_options(
        self,
        scenario_id: str,
        kind: CapabilityKind,
        key: str,
        port_key: str,
        *,
        environment: str = "prod",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        normalized_kind = _capability_kind(kind)
        payload = self._request(
            "scenarios/"
            f"{quote(str(scenario_id), safe='')}/capabilities/"
            f"{quote(normalized_kind, safe='')}/{quote(str(key), safe='')}/ports/"
            f"{quote(str(port_key), safe='')}/managed-input-options",
            params={
                "environment": environment,
                "limit": limit,
                "offset": offset,
            },
        )
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("items"), list)
            or payload.get("port_key") != str(port_key)
        ):
            raise ExternalApiError("外部 API 返回了无效受管输入选项目录")
        return payload

    def invoke_capability(
        self,
        scenario_id: str,
        kind: CapabilityKind,
        key: str,
        *,
        inputs: dict[str, Any] | None = None,
        managed_inputs: list[dict[str, Any]] | None = None,
        environment: str = "prod",
        mode: str = "execute",
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
        request_id: str | None = None,
        expected_definition_hash: str | None = None,
        expected_deployment_fingerprint: str | None = None,
        confirmation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_kind = _capability_kind(kind)
        document: dict[str, Any] = {
            "environment": environment,
            "mode": mode,
            "inputs": dict(inputs or {}),
            "managed_inputs": list(managed_inputs or []),
        }
        optional = {
            "idempotency_key": idempotency_key,
            "correlation_id": correlation_id,
            "request_id": request_id,
            "expected_definition_hash": expected_definition_hash,
            "expected_deployment_fingerprint": expected_deployment_fingerprint,
            "confirmation": confirmation,
        }
        document.update({name: value for name, value in optional.items() if value is not None})
        payload = self._request(
            "scenarios/"
            f"{quote(str(scenario_id), safe='')}/capabilities/"
            f"{quote(normalized_kind, safe='')}/{quote(str(key), safe='')}/invoke",
            method="POST",
            json_body=document,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("invocation_id"), str):
            raise ExternalApiError("外部 API 返回了无效能力回执")
        return payload

    def get_invocation_receipt(self, invocation_id: str) -> dict[str, Any]:
        payload = self._request(
            f"invocations/{quote(str(invocation_id), safe='')}"
        )
        if not isinstance(payload, dict) or payload.get("invocation_id") != invocation_id:
            raise ExternalApiError("外部 API 返回了无效能力回执")
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
