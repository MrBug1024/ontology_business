"""Authenticated Streamable HTTP MCP gateway for published Agents."""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import hmac
import json
import secrets
from typing import Annotated, Any
from urllib.parse import urlparse

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import get_settings
from .services import agent_mcp_service


_authenticated_service = contextvars.ContextVar[str | None](
    "authenticated_agent_mcp_service", default=None
)
_authenticated_external_session = contextvars.ContextVar[str | None](
    "authenticated_agent_mcp_external_session", default=None
)
_MCP_SESSION_ID_HEADER = b"mcp-session-id"
_MAX_EXTERNAL_SESSION_ID_LENGTH = 128
_MCP_SESSION_VERSION = "mcp1"


def _allowed_hosts() -> list[str]:
    settings = get_settings()
    values = [
        item.strip()
        for item in settings.agent_mcp_allowed_hosts.split(",")
        if item.strip()
    ]
    public_url = settings.agent_mcp_public_url.strip()
    if public_url:
        parsed = urlparse(public_url)
        if parsed.netloc and parsed.netloc not in values:
            values.append(parsed.netloc)
    return values


class AgentMCPBearerMiddleware:
    """Authenticate an opaque publication token before MCP parses the body."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        authorization = headers.get("authorization", "").strip()
        raw_token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        authenticated = await asyncio.to_thread(agent_mcp_service.authenticate_token, raw_token)
        if authenticated is None:
            body = json.dumps(
                {"error": "invalid_token", "error_description": "Agent MCP token is invalid or expired"}
            ).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"www-authenticate", b'Bearer error="invalid_token"'),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        external_session_id = headers.get("mcp-session-id", "").strip()
        if external_session_id and not self._valid_session_id(
            external_session_id, authenticated.service_id
        ):
            body = json.dumps(
                {"error": "invalid_session", "error_description": "MCP session id is invalid"}
            ).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 400,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        # FastMCP intentionally remains stateless here so concurrent API workers
        # do not require load-balancer affinity.  We still mint and echo the
        # standard MCP session header; the durable service-layer mapping turns
        # that external session into a stable platform conversation.
        external_session_id = external_session_id or self._new_session_id(
            authenticated.service_id
        )

        async def send_with_session(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                response_headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != _MCP_SESSION_ID_HEADER
                ]
                response_headers.append(
                    (_MCP_SESSION_ID_HEADER, external_session_id.encode("ascii"))
                )
                message = {**message, "headers": response_headers}
            await send(message)

        token = _authenticated_service.set(authenticated.service_id)
        session_token = _authenticated_external_session.set(external_session_id)
        try:
            await self.app(scope, receive, send_with_session)
        finally:
            _authenticated_external_session.reset(session_token)
            _authenticated_service.reset(token)

    @staticmethod
    def _session_signing_key() -> bytes:
        settings = get_settings()
        configured = settings.agent_mcp_session_signing_key.strip()
        if configured:
            return configured.encode("utf-8")
        # Existing deployments can roll forward without an outage, but the
        # fallback remains private because the database URL is server-only.
        # Production configuration should still provide the dedicated key.
        return hashlib.sha256(
            b"ontology-platform/agent-mcp-session-signing/v1\0"
            + settings.database_url.encode("utf-8")
        ).digest()

    @classmethod
    def _session_signature(cls, service_id: str, nonce: str) -> str:
        payload = (
            b"ontology-platform/agent-mcp-session/v1\0"
            + service_id.encode("utf-8")
            + b"\0"
            + nonce.encode("ascii")
        )
        return hmac.new(cls._session_signing_key(), payload, hashlib.sha256).hexdigest()

    @classmethod
    def _new_session_id(cls, service_id: str) -> str:
        nonce = secrets.token_urlsafe(32)
        signature = cls._session_signature(service_id, nonce)
        return f"{_MCP_SESSION_VERSION}.{nonce}.{signature}"

    @classmethod
    def _valid_session_id(cls, value: str, service_id: str) -> bool:
        if not (1 <= len(value) <= _MAX_EXTERNAL_SESSION_ID_LENGTH):
            return False
        version, separator, remainder = value.partition(".")
        nonce, separator2, signature = remainder.partition(".")
        if (
            version != _MCP_SESSION_VERSION
            or not separator
            or not separator2
            or len(nonce) < 32
            or len(signature) != 64
            or not all(char.isascii() and (char.isalnum() or char in "-_") for char in nonce)
            or not all(char in "0123456789abcdef" for char in signature)
        ):
            return False
        return hmac.compare_digest(
            signature,
            cls._session_signature(service_id, nonce),
        )


settings = get_settings()
mcp_server = FastMCP(
    name="Ontology Platform Agent Gateway",
    instructions=(
        "The invoke_agent tool calls the complete configured Agent runtime, including its "
        "governed data, skills, MCP tools and business capabilities."
    ),
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=False,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_hosts(),
        allowed_origins=list(settings.cors_origins),
    ),
)


@mcp_server.tool(
    name="invoke_agent",
    title="调用已发布 Agent",
    description="调用当前凭证绑定的完整平台 Agent，并返回回答、引用和内部工具执行结果。",
    structured_output=True,
)
async def invoke_agent(
    ctx: Context,
    message: Annotated[str, Field(min_length=1, max_length=12000, description="提交给 Agent 的任务或问题")],
    conversation_id: Annotated[
        str | None,
        Field(
            default=None,
            max_length=32,
            description="可选；同一 MCP Session 会自动续接，跨会话时可显式继续此前返回的对话",
        ),
    ] = None,
) -> dict[str, Any]:
    service_id = _authenticated_service.get()
    external_session_id = _authenticated_external_session.get()
    if not service_id:
        raise agent_mcp_service.AgentMCPError("Agent MCP 身份上下文不存在")
    if not external_session_id:
        raise agent_mcp_service.AgentMCPError("Agent MCP 会话上下文不存在")
    await ctx.info("已接收请求，正在调用绑定的 Agent。")
    result = await asyncio.to_thread(
        agent_mcp_service.invoke_published_agent,
        service_id,
        message=message,
        conversation_id=conversation_id,
        external_session_id=external_session_id,
        external_request_id=ctx.request_id,
    )
    await ctx.info("Agent 调用完成。")
    return result


# The parent FastAPI lifespan owns ``mcp_server.session_manager.run()``.
mcp_app = AgentMCPBearerMiddleware(mcp_server.streamable_http_app())
