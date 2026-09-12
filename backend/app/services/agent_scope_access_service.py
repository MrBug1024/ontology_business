"""Shared authorization for resources owned by an optional Agent scope."""
from __future__ import annotations

from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Agent, BusinessScenario
from . import permission_service, tenant_service


class AgentScopeNotFoundError(LookupError):
    """The requested Agent is absent from the authenticated tenant."""


def require_agent_permission(
    db: Session,
    agent: Agent,
    verb: Literal["read", "write"],
    *,
    message: str = "没有该 Agent 所属业务场景的权限",
) -> BusinessScenario | None:
    """Apply the Agent's scenario ACL, falling back only for legacy Agents."""

    principal = permission_service.require_principal(db)
    if str(agent.tenant_id or "") != principal.tenant_id:
        raise AgentScopeNotFoundError("Agent 不存在")
    if agent.scenario_id:
        scenario = tenant_service.require_scenario(db, agent.scenario_id)
        permission_service.require_scenario_permission(
            db,
            scenario,
            verb,
            message=message,
        )
        return scenario
    permission_service.require_tenant_permission(db, verb)
    return None


def require_optional_agent_permission(
    db: Session,
    agent_id: str | None,
    verb: Literal["read", "write"],
    *,
    lock: bool = False,
    message: str = "没有该 Agent 所属业务场景的权限",
) -> Agent | None:
    """Authorize an Agent-scoped resource or the unscoped tenant namespace."""

    normalized_agent_id = str(agent_id or "").strip()
    if not normalized_agent_id:
        permission_service.require_tenant_permission(db, verb)
        return None
    principal = permission_service.require_principal(db)
    statement = (
        select(Agent)
        .where(
            Agent.id == normalized_agent_id,
            Agent.tenant_id == principal.tenant_id,
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    agent = db.scalar(statement)
    if agent is None:
        raise AgentScopeNotFoundError("Agent 不存在")
    require_agent_permission(db, agent, verb, message=message)
    return agent
