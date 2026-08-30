"""Agent MCP publication lifecycle, opaque credentials and invocation audit."""
from __future__ import annotations

import hashlib
import json
import secrets
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..external_api_models import AgentMCPInvocation, AgentMCPService
from ..models import Agent
from . import permission_service, tenant_service


_TOKEN_HASH_DOMAIN = b"ontology-platform/agent-mcp-token/v1\0"


class AgentMCPError(ValueError):
    """A safe publication or invocation error."""


@dataclass(frozen=True)
class AuthenticatedAgentMCP:
    service_id: str
    tenant_id: str
    execution_user_id: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_name_key(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def token_hash(token: str) -> str:
    return hashlib.sha256(_TOKEN_HASH_DOMAIN + token.encode("utf-8")).hexdigest()


def agent_config_hash(agent: Agent) -> str:
    payload = {
        "name": agent.name,
        "description": agent.description or "",
        "scenario_id": agent.scenario_id,
        "llm_config_id": agent.llm_config_id,
        "system_prompt": agent.system_prompt or "",
        "runtime_data_source_ids": sorted(
            str(item) for item in (agent.runtime_data_source_ids or [])
        ),
        "capability_scope": agent.capability_scope,
        "temperature": agent.temperature,
        "max_tokens": agent.max_tokens,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def issue_token() -> tuple[str, str, str, str]:
    raw = f"agt_sk_{secrets.token_urlsafe(32)}"
    return raw, token_hash(raw), raw[:14], raw[-4:]


def client_config(name: str, endpoint_url: str, token: str) -> dict[str, Any]:
    return {
        "mcpServers": {
            name: {
                "type": "http",
                "url": endpoint_url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }


def validate_agent_runtime(
    db: Session,
    agent_id: str,
    *,
    writable: bool = False,
    environment: str = "dev",
) -> tuple[Agent, Any, list[str]]:
    # Import lazily to keep the MCP service independent from router import order.
    from ..routers import agents

    agent = agents._agent(db, agent_id, writable=writable)
    context = agents._authorization_context(db, agent, environment=environment)
    if context is None:
        return agent, None, ["当前环境运行定义或连接器"]
    missing = agents._agent_readiness_missing(db, agent, runtime_context=context)
    return agent, context, missing


def service_runtime_status(
    db: Session,
    service: AgentMCPService,
) -> tuple[Agent | None, Any | None, list[str], bool]:
    previous_tenant = db.info.get("tenant_id")
    previous_user = db.info.get("user_id")
    try:
        db.info["tenant_id"] = service.tenant_id
        db.info["user_id"] = service.execution_user_id or ""
        try:
            agent, context, missing = validate_agent_runtime(
                db,
                service.agent_id,
                environment=service.runtime_environment,
            )
        except HTTPException as exc:
            return None, None, [str(exc.detail)], True
        if context is None:
            return agent, None, missing, True
        definition = context.runtime_definition
        stale = (
            agent_config_hash(agent) != service.agent_config_hash
            or not definition
            or definition.definition_hash != service.definition_hash
            or definition.environment != service.runtime_environment
        )
        return agent, context, missing, stale
    finally:
        if previous_tenant is None:
            db.info.pop("tenant_id", None)
        else:
            db.info["tenant_id"] = previous_tenant
        if previous_user is None:
            db.info.pop("user_id", None)
        else:
            db.info["user_id"] = previous_user


def authenticate_token(raw_token: str) -> AuthenticatedAgentMCP | None:
    if not raw_token.startswith("agt_sk_") or len(raw_token) > 512:
        return None
    db = SessionLocal()
    try:
        now = utc_now()
        service = db.execute(
            select(AgentMCPService).where(
                AgentMCPService.token_hash == token_hash(raw_token),
                AgentMCPService.enabled.is_(True),
            )
        ).scalars().first()
        if not service or not service.execution_user_id:
            return None
        expires_at = service.expires_at
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now:
                return None
        service.last_used_at = now
        db.commit()
        return AuthenticatedAgentMCP(
            service_id=service.id,
            tenant_id=service.tenant_id,
            execution_user_id=service.execution_user_id,
        )
    finally:
        db.close()


def invoke_published_agent(
    service_id: str,
    *,
    message: str,
    conversation_id: str | None,
    inputs: dict[str, Any] | None = None,
    managed_inputs: list[dict[str, Any]] | None = None,
    capability: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    from ..routers import agents

    db = SessionLocal()
    started = perf_counter()
    invocation: AgentMCPInvocation | None = None
    try:
        service = db.get(AgentMCPService, service_id)
        if not service or not service.enabled or not service.execution_user_id:
            raise AgentMCPError("Agent MCP 服务不存在或已停用")
        db.info["tenant_id"] = service.tenant_id
        db.info["user_id"] = service.execution_user_id
        permission_service.require_principal(db)

        agent, context, missing, stale = service_runtime_status(db, service)
        if not agent or context is None:
            raise AgentMCPError("Agent 当前不可用：" + "、".join(missing))
        if missing:
            raise AgentMCPError("Agent 尚未就绪：" + "、".join(missing))
        if stale:
            raise AgentMCPError("Agent 配置或运行定义已变化，请在平台重新发布该 MCP 服务")

        if conversation_id:
            owned = db.execute(
                select(AgentMCPInvocation.id).where(
                    AgentMCPInvocation.service_id == service.id,
                    AgentMCPInvocation.conversation_id == conversation_id,
                ).limit(1)
            ).scalar_one_or_none()
            if not owned:
                raise AgentMCPError("对话不属于当前 Agent MCP 服务")

        request_id = uuid.uuid4().hex
        input_document = {
            "message": message,
            "inputs": inputs or {},
            "managed_inputs": managed_inputs or [],
            "capability": capability,
            "idempotency_key": idempotency_key,
        }
        invocation = AgentMCPInvocation(
            service_id=service.id,
            tenant_id=service.tenant_id,
            agent_id=service.agent_id,
            execution_user_id=service.execution_user_id,
            request_id=request_id,
            input_hash=hashlib.sha256(json.dumps(
                input_document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
            status="running",
        )
        db.add(invocation)
        db.commit()

        result = agents.invoke_agent_once(
            service.agent_id,
            message=message,
            conversation_id=conversation_id,
            inputs=inputs,
            managed_inputs=managed_inputs,
            capability=capability,
            idempotency_key=idempotency_key,
            environment=service.runtime_environment,
            db=db,
        )
        result.update({
            "request_id": request_id,
            "mcp_service_id": service.id,
            "mcp_service_name": service.name,
        })
        invocation.conversation_id = result.get("conversation_id") or None
        invocation.trace_id = str(result.get("trace_id") or "")[:64]
        invocation.status = "succeeded"
        invocation.latency_ms = int((perf_counter() - started) * 1000)
        invocation.tool_call_count = len(result.get("tool_calls") or [])
        invocation.result = json.loads(json.dumps(result, ensure_ascii=False, default=str))
        invocation.completed_at = utc_now()
        db.commit()
        return result
    except Exception as exc:
        if invocation is not None:
            try:
                invocation.status = "failed"
                invocation.latency_ms = int((perf_counter() - started) * 1000)
                invocation.error_code = type(exc).__name__[:80]
                invocation.error_message = str(exc)[:4000]
                invocation.completed_at = utc_now()
                db.commit()
            except Exception:
                db.rollback()
        raise
    finally:
        db.close()
