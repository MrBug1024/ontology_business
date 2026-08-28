"""Authenticated Streamable HTTP MCP gateway for published Agents."""
from __future__ import annotations

import asyncio
import contextvars
import json
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
        token = _authenticated_service.set(authenticated.service_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _authenticated_service.reset(token)


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
        Field(default=None, max_length=32, description="可选；继续此前由同一 MCP 服务创建的对话"),
    ] = None,
) -> dict[str, Any]:
    service_id = _authenticated_service.get()
    if not service_id:
        raise agent_mcp_service.AgentMCPError("Agent MCP 身份上下文不存在")
    await ctx.info("已接收请求，正在调用绑定的 Agent。")
    result = await asyncio.to_thread(
        agent_mcp_service.invoke_published_agent,
        service_id,
        message=message,
        conversation_id=conversation_id,
    )
    await ctx.info("Agent 调用完成。")
    return result


# The parent FastAPI lifespan owns ``mcp_server.session_manager.run()``.
mcp_app = AgentMCPBearerMiddleware(mcp_server.streamable_http_app())
