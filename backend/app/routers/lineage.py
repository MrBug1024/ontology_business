"""P1 端到端血缘只读接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..services import lineage_service, permission_service, tenant_service
from ..services.auth_service import get_tenant_db


router = APIRouter(prefix="/lineage", tags=["lineage"])


@router.get("/scenarios/{scenario_id}")
def scenario_lineage(
    scenario_id: str,
    limit: int = Query(default=300, ge=1, le=500),
    db: Session = Depends(get_tenant_db),
):
    """返回当前租户可读取场景的安全血缘图。

    公共场景只公开建模与只读业务资源；运行轨迹可能包含 AI 回答、执行时序和
    外部结果状态，因此不把运营血缘跨租户公开。
    """
    scenario = tenant_service.require_scenario(db, scenario_id)
    permission_service.require_scenario_permission(db, scenario, "read")
    if scenario.tenant_id != tenant_service.current_tenant_id(db):
        raise HTTPException(status_code=403, detail="公共业务场景不公开运营血缘")
    return lineage_service.build_scenario_lineage(db, scenario_id, limit=limit)
