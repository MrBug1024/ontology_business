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
from mcp.types import Annotations, CallToolResult, TextContent
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


def _parsed_tool_payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _public_citations(value: Any) -> list[dict[str, Any]]:
    """Keep citation identity without repeating retrieved document bodies."""
    if not isinstance(value, list):
        return []
    public_keys = (
        "citation_id",
        "file_id",
        "filename",
        "data_source_id",
        "data_source_name",
        "chunk_id",
        "page",
        "char_start",
        "char_end",
        "url",
        "title",
    )
    return [
        {key: item[key] for key in public_keys if key in item}
        for item in value
        if isinstance(item, dict)
    ]


def _public_confirmation(value: dict[str, Any]) -> dict[str, Any]:
    """Retain confirmation handles and deliverables without replaying row data."""
    public = {
        key: value[key]
        for key in ("status", "preview_log_id", "message")
        if key in value
    }
    response = value.get("response")
    if not isinstance(response, dict):
        return public
    response_public = {
        key: response[key]
        for key in (
            "status",
            "original_status",
            "log_id",
            "target_id",
            "target_name",
            "workflow_run_id",
            "task_id",
            "event_id",
            "duration_ms",
            "error",
        )
        if key in response
    }
    result = response.get("result")
    if isinstance(result, dict):
        result_public = {
            key: result[key]
            for key in ("row_count", "truncated", "next_offset", "message")
            if key in result
        }
        artifact = result.get("artifact")
        if isinstance(artifact, dict):
            result_public["artifact"] = {
                key: artifact[key]
                for key in (
                    "id",
                    "filename",
                    "format",
                    "mime",
                    "size",
                    "sha256",
                    "download_url",
                )
                if key in artifact
            }
        if result_public:
            response_public["result"] = result_public
    public["response"] = response_public
    return public


def _public_agent_result(result: dict[str, Any]) -> dict[str, Any]:
    """Build the compact result sent over MCP; the full trace stays durable."""
    tool_calls = result.get("tool_calls")
    tool_results = result.get("tool_results")
    calls = tool_calls if isinstance(tool_calls, list) else []
    outcomes = tool_results if isinstance(tool_results, list) else []
    failed_count = 0
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        payload = _parsed_tool_payload(outcome.get("result"))
        if not payload or payload.get("ok") is not False:
            continue
        failed_count += 1

    conversation_id = str(result.get("conversation_id") or "")
    public: dict[str, Any] = {
        "answer": str(result.get("answer") or ""),
        "conversation_id": conversation_id,
        "citations": _public_citations(result.get("citations")),
        "tool_execution": {
            "call_count": len(calls),
            "result_count": len(outcomes),
            "failed_count": failed_count,
        },
        "request_id": str(result.get("request_id") or ""),
    }
    confirmation = result.get("confirmation")
    if isinstance(confirmation, dict):
        public["confirmation"] = _public_confirmation(confirmation)
    public["continuation"] = {
        "conversation_id": conversation_id,
        "instruction": (
            "Pass this conversation_id unchanged in a later invoke_agent call to continue "
            "the same Agent conversation. Omit it to start a new conversation."
        ),
    }
    public["replayed"] = bool(result.get("mcp_replayed"))
    input_receipt = result.get("mcp_input_receipt")
    if isinstance(input_receipt, dict):
        public["input_receipt"] = {
            key: input_receipt[key]
            for key in (
                "source",
                "message_sha256",
                "message_length",
            )
            if key in input_receipt
        }
    return public


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
        # do not require load-balancer affinity. We still mint and echo the
        # transport session header, but it never identifies an end-user chat;
        # only the explicit conversation_id tool argument provides continuity.
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
        "This server exposes exactly one public tool named invoke_agent. Call only tool names "
        "returned by tools/list. Pass the user's complete current request in message and call "
        "invoke_agent once for that turn; do not reduce it to keywords or a plan. The server "
        "executes all Agent operations internally, so never call operation names mentioned in "
        "a result. Omit conversation_id for a new conversation and pass the returned handle "
        "unchanged only when continuing that same conversation."
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
    description=(
        "调用平台中已发布的完整 Agent。message 必须包含用户本轮完整请求，不要改写成关键词、"
        "规则检索或执行计划，也不要拆成多个工具调用。平台会在内部完成查询、推理和操作；"
        "外部只需调用本工具。首次调用不传 conversation_id，继续同一对话时原样传回上次返回值。"
    ),
    structured_output=True,
)
async def invoke_agent(
    ctx: Context,
    message: Annotated[
        str,
        Field(
            min_length=1,
            max_length=12000,
            title="用户本轮完整请求",
            description=(
                "终端用户本轮提交给已发布 Agent 的完整请求。保留其中的主体、条件、范围、"
                "空格、引号和换行，不得只传关键词、规则查询、摘要或执行计划"
            ),
            examples=[
                "审计 贵阳泰康乐综合医院  刮痧治疗收费大于两次 的全部违规数据"
            ],
        ),
    ],
    conversation_id: Annotated[
        str | None,
        Field(
            default=None,
            max_length=32,
            description=(
                "首次调用留空；继续同一终端用户会话时必须传入上次返回的 conversation_id，"
                "包括确认回复和 MCP 重连后的调用。开始新对话时必须留空"
            ),
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
        input_source="tool_argument",
    )
    await ctx.info("Agent 调用完成。")
    public_result = _public_agent_result(result)
    request_receipt = {
        "source": "tool_argument",
        "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
        "message_length": len(message),
    }
    if "input_receipt" not in public_result:
        public_result["input_receipt"] = request_receipt
    continuation_text = json.dumps(
        {
            "mcp_continuation": public_result["continuation"],
            "input_receipt": public_result["input_receipt"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=public_result["answer"] or "Agent 调用完成，但没有返回文本答案。",
            ),
            TextContent(
                type="text",
                text=continuation_text,
                annotations=Annotations(audience=["assistant"], priority=1.0),
            ),
        ],
        structuredContent=public_result,
        isError=False,
    )


# The parent FastAPI lifespan owns ``mcp_server.session_manager.run()``.
mcp_app = AgentMCPBearerMiddleware(mcp_server.streamable_http_app())
