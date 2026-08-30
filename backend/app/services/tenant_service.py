"""租户访问边界的统一查询与资源校验。"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import BusinessScenario


def current_tenant_id(db: Session) -> str:
    value = db.info.get("tenant_id")
    if not value:
        raise HTTPException(401, "缺少租户上下文")
    return str(value)


def visible_clause(model, db: Session):
    return or_(model.tenant_id == current_tenant_id(db), model.is_public.is_(True))


def get_visible(db: Session, model, resource_id: str):
    return db.execute(
        select(model).where(model.id == resource_id, visible_clause(model, db))
    ).scalars().first()


def require_visible(db: Session, model, resource_id: str, message: str = "资源不存在"):
    resource = get_visible(db, model, resource_id)
    if not resource:
        raise HTTPException(404, message)
    return resource


def require_owned(db: Session, model, resource_id: str, message: str = "资源不存在"):
    resource = db.execute(
        select(model).where(model.id == resource_id, model.tenant_id == current_tenant_id(db))
    ).scalars().first()
    if not resource:
        raise HTTPException(404, message)
    return resource


def require_scenario(db: Session, scenario_id: str, writable: bool = False) -> BusinessScenario:
    scenario = get_visible(db, BusinessScenario, scenario_id)
    if not scenario:
        raise HTTPException(404, "业务场景不存在")
    if writable and scenario.tenant_id != current_tenant_id(db):
        raise HTTPException(403, "公共业务场景只读")
    if writable and scenario.status == "retired":
        raise HTTPException(409, "业务场景已退役，只允许读取历史定义与审计记录")
    return scenario
