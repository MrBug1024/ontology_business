"""Restore and revalidate the durable document task's original actor."""
from __future__ import annotations

from contextlib import contextmanager

from fastapi import HTTPException
from sqlalchemy import select

from ..models import ActionExecutionLog, Agent, BusinessScenario, DataSource
from . import agent_scope_access_service, catalog_service, permission_service


@contextmanager
def execution_principal(db, job):
    previous = {key: db.info.get(key) for key in ("tenant_id", "user_id")}
    try:
        restore_job_principal(db, job)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                db.info.pop(key, None)
            else:
                db.info[key] = value
        permission_service.refresh_request_authorization(db)


def restore_job_principal(db, job):
    db.info["tenant_id"] = job.tenant_id
    db.info["user_id"] = job.requested_by_user_id
    permission_service.refresh_request_authorization(db)
    return permission_service.require_principal(db)


def require_source_write(db, source_id, *, file=None):
    permission_service.refresh_request_authorization(db)
    principal = permission_service.require_principal(db)
    source = db.scalar(select(DataSource).where(
        DataSource.id == source_id, DataSource.tenant_id == principal.tenant_id,
        DataSource.type == "file_bucket",
    ).execution_options(populate_existing=True))
    if source is None:
        raise HTTPException(404, "资料不存在")
    if source.owner_agent_id:
        agent = db.scalar(select(Agent).where(
            Agent.id == source.owner_agent_id, Agent.tenant_id == principal.tenant_id,
        ).execution_options(populate_existing=True))
        if agent is None or (source.scenario_id and source.scenario_id != agent.scenario_id):
            raise HTTPException(404, "资料不存在")
        agent_scope_access_service.require_agent_permission(db, agent, "write")
    elif source.scenario_id:
        scenario = db.scalar(select(BusinessScenario).where(
            BusinessScenario.id == source.scenario_id,
            BusinessScenario.tenant_id == principal.tenant_id,
        ).execution_options(populate_existing=True))
        if scenario is None:
            raise HTTPException(404, "资料不存在")
        permission_service.require_scenario_permission(db, scenario, "write")
    elif source.resource_scope == "modeling":
        permission_service.require_tenant_permission(db, "write")
    elif file is not None and source.id == catalog_service.external_upload_source_id(principal.tenant_id):
        receipt = db.scalar(select(ActionExecutionLog).join(BusinessScenario).where(
            ActionExecutionLog.id == file.generated_by_action_log_id,
            ActionExecutionLog.actor_user_id == principal.user_id,
            BusinessScenario.tenant_id == principal.tenant_id,
            ActionExecutionLog.mode == "execute", ActionExecutionLog.status == "success",
        )) if file.generated_by_action_log_id else None
        result = receipt.result if receipt is not None and isinstance(receipt.result, dict) else {}
        artifact = result.get("artifact") or {}
        if result.get("status") != "generated" or artifact.get("id") != file.id:
            raise HTTPException(404, "资料不存在")
        permission_service.require_scenario_permission(db, receipt.scenario, "write")
    else:
        raise HTTPException(404, "资料不存在")
    return source
