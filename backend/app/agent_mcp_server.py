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
from .services import agent_mcp_service, capability_mcp_service


_authenticated_service = contextvars.ContextVar[str | None](
    "authenticated_agent_mcp_service", default=None
)
_authenticated_capability = contextvars.ContextVar[
    capability_mcp_service.AuthenticatedCapabilityMCP | None
]("authenticated_capability_mcp", default=None)


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
        authenticated_agent = await asyncio.to_thread(
            agent_mcp_service.authenticate_token,
            raw_token,
        )
        authenticated_capability = (
            None
            if authenticated_agent is not None
            else await asyncio.to_thread(
                capability_mcp_service.authenticate_token,
                raw_token,
            )
        )
        if authenticated_agent is None and authenticated_capability is None:
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
        service_token = _authenticated_service.set(
            authenticated_agent.service_id if authenticated_agent is not None else None
        )
        capability_token = _authenticated_capability.set(authenticated_capability)
        try:
            await self.app(scope, receive, send)
        finally:
            _authenticated_capability.reset(capability_token)
            _authenticated_service.reset(service_token)


settings = get_settings()
mcp_server = FastMCP(
    name="Ontology Platform Agent Gateway",
    instructions=(
        "Use invoke_agent with an Agent publication token. Use the generic capability tools "
        "with a capabilities-scoped external API key; all execution uses governed references."
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
    inputs: Annotated[
        dict[str, Any] | None,
        Field(default=None, description="可选；本次调用的结构化 JSON 输入"),
    ] = None,
    managed_inputs: Annotated[
        list[dict[str, Any]] | None,
        Field(default=None, max_length=100, description="可选；按端口提交的受管数据引用"),
    ] = None,
    capability: Annotated[
        dict[str, Any] | None,
        Field(default=None, description="可选；明确指定 kind/key 能力目标"),
    ] = None,
    idempotency_key: Annotated[
        str | None,
        Field(default=None, min_length=1, max_length=180, description="可选；副作用调用的稳定幂等键"),
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
        inputs=inputs,
        managed_inputs=managed_inputs,
        capability=capability,
        idempotency_key=idempotency_key,
    )
    await ctx.info("Agent 调用完成。")
    return result


def _capability_identity() -> capability_mcp_service.AuthenticatedCapabilityMCP:
    authenticated = _authenticated_capability.get()
    if authenticated is None:
        raise capability_mcp_service.CapabilityMCPError(
            "this MCP credential is not authorized for generic capabilities"
        )
    return authenticated


@mcp_server.tool(
    name="list_capabilities",
    title="发现业务能力",
    description="返回指定场景和环境中当前主体可见的机器可读能力契约与就绪状态。",
    structured_output=True,
)
async def list_capabilities(
    ctx: Context,
    scenario_id: Annotated[str, Field(min_length=1, max_length=32)],
    environment: Annotated[
        str,
        Field(pattern=r"^(dev|staging|prod)$"),
    ] = "prod",
) -> dict[str, Any]:
    await ctx.info("正在解析能力目录。")
    items = await asyncio.to_thread(
        capability_mcp_service.list_capabilities,
        _capability_identity(),
        scenario_id=scenario_id,
        environment=environment,
    )
    return {"capabilities": items}


@mcp_server.tool(
    name="invoke_capability",
    title="调用业务能力",
    description=(
        "使用结构化输入和受管数据引用调用指定能力；不接受连接串、凭据、SQL 或物理表名。"
    ),
    structured_output=True,
)
async def invoke_capability(
    ctx: Context,
    scenario_id: Annotated[str, Field(min_length=1, max_length=32)],
    capability_kind: Annotated[
        str,
        Field(pattern=r"^(function|action|rule|workflow)$"),
    ],
    capability_key: Annotated[str, Field(min_length=1, max_length=240)],
    environment: Annotated[
        str,
        Field(pattern=r"^(dev|staging|prod)$"),
    ] = "prod",
    inputs: dict[str, Any] | None = None,
    managed_inputs: Annotated[
        list[dict[str, Any]],
        Field(max_length=100),
    ]
    | None = None,
    mode: Annotated[
        str,
        Field(pattern=r"^(execute|preview|confirm)$"),
    ] = "execute",
    idempotency_key: Annotated[str | None, Field(max_length=180)] = None,
    correlation_id: Annotated[str | None, Field(max_length=240)] = None,
    request_id: Annotated[str | None, Field(max_length=64)] = None,
    expected_definition_hash: Annotated[
        str | None,
        Field(pattern=r"^[0-9a-f]{64}$"),
    ] = None,
    expected_deployment_fingerprint: Annotated[
        str | None,
        Field(pattern=r"^[0-9a-f]{64}$"),
    ] = None,
    confirmation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    await ctx.info("正在执行能力调用。")
    result = await asyncio.to_thread(
        capability_mcp_service.invoke_capability,
        _capability_identity(),
        scenario_id=scenario_id,
        capability_kind=capability_kind,
        capability_key=capability_key,
        environment=environment,
        inputs=inputs,
        managed_inputs=managed_inputs,
        mode=mode,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
        request_id=request_id,
        expected_definition_hash=expected_definition_hash,
        expected_deployment_fingerprint=expected_deployment_fingerprint,
        confirmation=confirmation,
    )
    await ctx.info("能力调用已形成可审计回执。")
    return result


@mcp_server.tool(
    name="get_capability_receipt",
    title="查询能力回执",
    description="查询由当前 API key 主体创建且当前仍有权读取的能力调用回执。",
    structured_output=True,
)
async def get_capability_receipt(
    ctx: Context,
    invocation_id: Annotated[str, Field(min_length=1, max_length=32)],
) -> dict[str, Any]:
    await ctx.info("正在读取能力回执。")
    return await asyncio.to_thread(
        capability_mcp_service.get_receipt,
        _capability_identity(),
        invocation_id=invocation_id,
    )


# The parent FastAPI lifespan owns ``mcp_server.session_manager.run()``.
mcp_app = AgentMCPBearerMiddleware(mcp_server.streamable_http_app())
