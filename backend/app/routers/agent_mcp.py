"""Manage Agent publications that are callable through the public MCP endpoint."""
from __future__ import annotations

import json
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..agent_mcp_schemas import (
    AgentMCPCandidateOut,
    AgentMCPServiceCreatedOut,
    AgentMCPServiceCreateIn,
    AgentMCPServiceOut,
    AgentMCPServiceTestOut,
    AgentMCPServiceUpdateIn,
    AgentMCPTokenRotateIn,
)
from ..config import get_settings
from ..external_api_models import AgentMCPService
from ..models import Agent
from ..services import agent_mcp_service, permission_service, tenant_service
from ..services.auth_service import get_tenant_db


router = APIRouter(prefix="/agent-mcp-services", tags=["agent-mcp"])


def _principal(db: Session):
    permission_service.require_tenant_permission(db, "manage")
    return permission_service.require_principal(db)


def _reader(db: Session):
    permission_service.require_tenant_permission(db, "read")
    return permission_service.require_principal(db)


def _endpoint_url(request: Request) -> str:
    configured = get_settings().agent_mcp_public_url.strip()
    return configured.rstrip("/") if configured else f"{str(request.base_url).rstrip('/')}/mcp"


def _owned_service(db: Session, service_id: str) -> AgentMCPService:
    service = db.execute(
        select(AgentMCPService).where(
            AgentMCPService.id == service_id,
            AgentMCPService.tenant_id == tenant_service.current_tenant_id(db),
            AgentMCPService.deleted_at.is_(None),
        )
    ).scalars().first()
    if not service:
        raise HTTPException(404, "Agent MCP 服务不存在")
    return service


def _out(db: Session, service: AgentMCPService, endpoint_url: str) -> AgentMCPServiceOut:
    agent, context, missing, stale = agent_mcp_service.service_runtime_status(db, service)
    stored_agent = agent or db.get(Agent, service.agent_id)
    scenario_name = ""
    definition = context.runtime_definition if context is not None else None
    if context is not None and context.scenario is not None:
        scenario_name = context.scenario.name
    return AgentMCPServiceOut(
        id=service.id,
        name=service.name,
        agent_id=service.agent_id,
        agent_name=stored_agent.name if stored_agent else "已删除 Agent",
        scenario_name=scenario_name,
        enabled=service.enabled,
        ready=bool(agent and context and not missing and not stale),
        stale=stale,
        missing=missing,
        endpoint_url=endpoint_url,
        key_prefix=service.key_prefix,
        token_hint=service.token_hint,
        expires_at=service.expires_at,
        last_used_at=service.last_used_at,
        runtime_environment=str(getattr(definition, "environment", "") or service.runtime_environment),
        definition_hash=str(getattr(definition, "definition_hash", "") or service.definition_hash),
        created_at=service.created_at,
        updated_at=service.updated_at,
    )


def _created_out(
    db: Session,
    service: AgentMCPService,
    *,
    endpoint_url: str,
    raw_token: str,
) -> AgentMCPServiceCreatedOut:
    config = agent_mcp_service.client_config(service.name, endpoint_url, raw_token)
    return AgentMCPServiceCreatedOut(
        **_out(db, service, endpoint_url).model_dump(),
        token=raw_token,
        config=config,
        config_json=json.dumps(config, ensure_ascii=False, indent=2),
    )


@router.get("/candidates", response_model=list[AgentMCPCandidateOut])
def list_candidates(db: Session = Depends(get_tenant_db)) -> list[AgentMCPCandidateOut]:
    _reader(db)
    tenant_id = tenant_service.current_tenant_id(db)
    rows: list[AgentMCPCandidateOut] = []
    for agent in db.execute(
        select(Agent).where(Agent.tenant_id == tenant_id).order_by(Agent.name, Agent.id)
    ).scalars().all():
        try:
            resolved, context, missing = agent_mcp_service.validate_agent_runtime(db, agent.id)
        except HTTPException:
            continue
        rows.append(AgentMCPCandidateOut(
            id=resolved.id,
            name=resolved.name,
            scenario_name=context.scenario.name if context and context.scenario else "",
            ready=bool(context and not missing),
            missing=missing,
        ))
    return rows


@router.get("", response_model=list[AgentMCPServiceOut])
def list_services(
    request: Request,
    db: Session = Depends(get_tenant_db),
) -> list[AgentMCPServiceOut]:
    _reader(db)
    endpoint_url = _endpoint_url(request)
    services = db.execute(
        select(AgentMCPService)
        .where(
            AgentMCPService.tenant_id == tenant_service.current_tenant_id(db),
            AgentMCPService.deleted_at.is_(None),
        )
        .order_by(AgentMCPService.created_at.desc(), AgentMCPService.id.desc())
    ).scalars().all()
    return [_out(db, service, endpoint_url) for service in services]


@router.post("", response_model=AgentMCPServiceCreatedOut, status_code=status.HTTP_201_CREATED)
def create_service(
    payload: AgentMCPServiceCreateIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
) -> AgentMCPServiceCreatedOut:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    principal = _principal(db)
    agent, context, missing = agent_mcp_service.validate_agent_runtime(
        db, payload.agent_id, writable=True
    )
    if context is None or missing:
        raise HTTPException(409, "Agent 尚未就绪，请先完成：" + "、".join(missing))
    definition = context.runtime_definition
    if definition is None:
        raise HTTPException(409, "Agent 当前运行定义不可用")
    raw_token, hashed, prefix, hint = agent_mcp_service.issue_token()
    service = AgentMCPService(
        tenant_id=principal.tenant_id,
        agent_id=agent.id,
        created_by_user_id=principal.user_id,
        execution_user_id=principal.user_id,
        name=payload.name,
        name_key=agent_mcp_service.normalize_name_key(payload.name),
        token_hash=hashed,
        key_prefix=prefix,
        token_hint=hint,
        enabled=True,
        expires_at=agent_mcp_service.utc_now() + timedelta(days=payload.expires_in_days),
        agent_config_hash=agent_mcp_service.agent_config_hash(agent),
        definition_snapshot_id=definition.snapshot_id,
        release_id=definition.release_id,
        definition_hash=definition.definition_hash,
        runtime_environment=definition.environment,
    )
    db.add(service)
    try:
        db.commit()
        db.refresh(service)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Agent MCP 服务名称已存在") from exc
    return _created_out(db, service, endpoint_url=_endpoint_url(request), raw_token=raw_token)


@router.put("/{service_id}", response_model=AgentMCPServiceOut)
def update_service(
    service_id: str,
    payload: AgentMCPServiceUpdateIn,
    request: Request,
    db: Session = Depends(get_tenant_db),
) -> AgentMCPServiceOut:
    _principal(db)
    service = _owned_service(db, service_id)
    if payload.enabled:
        _agent, _context, missing, _stale = agent_mcp_service.service_runtime_status(db, service)
        if missing:
            raise HTTPException(409, "Agent 尚未就绪：" + "、".join(missing))
    service.enabled = payload.enabled
    db.commit()
    db.refresh(service)
    return _out(db, service, _endpoint_url(request))


@router.post("/{service_id}/rotate-token", response_model=AgentMCPServiceCreatedOut)
def rotate_token(
    service_id: str,
    payload: AgentMCPTokenRotateIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
) -> AgentMCPServiceCreatedOut:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    _principal(db)
    service = _owned_service(db, service_id)
    agent, context, missing = agent_mcp_service.validate_agent_runtime(
        db, service.agent_id, writable=True
    )
    if context is None or missing:
        raise HTTPException(409, "Agent 尚未就绪，请先完成：" + "、".join(missing))
    definition = context.runtime_definition
    if definition is None:
        raise HTTPException(409, "Agent 当前运行定义不可用")
    raw_token, hashed, prefix, hint = agent_mcp_service.issue_token()
    service.token_hash = hashed
    service.key_prefix = prefix
    service.token_hint = hint
    service.expires_at = agent_mcp_service.utc_now() + timedelta(days=payload.expires_in_days)
    service.agent_config_hash = agent_mcp_service.agent_config_hash(agent)
    service.definition_snapshot_id = definition.snapshot_id
    service.release_id = definition.release_id
    service.definition_hash = definition.definition_hash
    service.runtime_environment = definition.environment
    service.enabled = True
    db.commit()
    db.refresh(service)
    return _created_out(db, service, endpoint_url=_endpoint_url(request), raw_token=raw_token)


@router.post("/{service_id}/test", response_model=AgentMCPServiceTestOut)
def test_service(
    service_id: str,
    db: Session = Depends(get_tenant_db),
) -> AgentMCPServiceTestOut:
    _principal(db)
    service = _owned_service(db, service_id)
    agent, context, missing, _stale = agent_mcp_service.service_runtime_status(db, service)
    if not agent or context is None or missing:
        raise HTTPException(409, "Agent 尚未就绪：" + "、".join(missing))
    definition = context.runtime_definition
    return AgentMCPServiceTestOut(
        ok=True,
        message="当前 Agent 可对话，第三方可以通过 invoke_agent 调用完整 Agent 能力",
        agent_name=agent.name,
        runtime_environment=str(getattr(definition, "environment", "") or service.runtime_environment),
        definition_hash=str(getattr(definition, "definition_hash", "") or service.definition_hash),
    )


@router.delete("/{service_id}")
def delete_service(service_id: str, db: Session = Depends(get_tenant_db)) -> dict[str, str]:
    _principal(db)
    service = _owned_service(db, service_id)
    service.enabled = False
    service.expires_at = agent_mcp_service.utc_now()
    service.deleted_at = agent_mcp_service.utc_now()
    service.token_hash = agent_mcp_service.token_hash(
        f"deleted:{service.id}:{service.deleted_at.isoformat()}"
    )
    service.name_key = f"deleted:{service.id}"
    db.commit()
    return {"message": "已删除"}
