"""MCP 配置路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import MCPConfig
from ..schemas import MCPConfigIn, MCPConfigOut, MCPToolInfo, Msg
from ..services import mcp_service, tenant_service
from ..services.auth_service import get_tenant_db

router = APIRouter(prefix="/mcp", tags=["mcp"])

_SECRET_PARTS = ("password", "api_key", "token", "secret", "authorization", "key")


def _public_map(values: dict | None) -> dict:
    """MCP 环境变量和请求头可能包含密钥，接口只返回键名和空值。"""
    result = {}
    for key, value in (values or {}).items():
        result[key] = "" if any(part in key.lower() for part in _SECRET_PARTS) else value
    return result


def _merge_map(old: dict | None, new: dict | None) -> dict:
    """编辑时空敏感值表示保持旧值，避免前端必须回读密钥。"""
    result = dict(new or {})
    for key, value in (old or {}).items():
        if key not in result:
            result[key] = value
        elif any(part in key.lower() for part in _SECRET_PARTS) and not result[key]:
            result[key] = value
    return result


def _out(c: MCPConfig) -> MCPConfigOut:
    return MCPConfigOut(
        id=c.id,
        name=c.name,
        transport=c.transport,
        command=c.command,
        args=c.args or [],
        url=c.url,
        env=_public_map(c.env),
        headers=_public_map(c.headers),
        enabled=c.enabled,
        created_at=c.created_at,
    )


@router.get("", response_model=list[MCPConfigOut])
def list_mcp(db: Session = Depends(get_tenant_db)):
    return [_out(c) for c in db.execute(select(MCPConfig).where(tenant_service.visible_clause(MCPConfig, db))).scalars().all()]


@router.post("", response_model=MCPConfigOut)
def create_mcp(payload: MCPConfigIn, db: Session = Depends(get_tenant_db)):
    c = MCPConfig(tenant_id=tenant_service.current_tenant_id(db), **payload.model_dump())
    db.add(c)
    db.commit()
    db.refresh(c)
    return _out(c)


@router.put("/{mcp_id}", response_model=MCPConfigOut)
def update_mcp(mcp_id: str, payload: MCPConfigIn, db: Session = Depends(get_tenant_db)):
    c = tenant_service.require_owned(db, MCPConfig, mcp_id, "MCP 不存在")
    values = payload.model_dump()
    values["env"] = _merge_map(c.env, values.get("env"))
    values["headers"] = _merge_map(c.headers, values.get("headers"))
    for k, v in values.items():
        setattr(c, k, v)
    db.commit()
    db.refresh(c)
    return _out(c)


@router.delete("/{mcp_id}", response_model=Msg)
def delete_mcp(mcp_id: str, db: Session = Depends(get_tenant_db)):
    c = tenant_service.require_owned(db, MCPConfig, mcp_id, "MCP 不存在")
    db.delete(c)
    db.commit()
    return Msg(message="已删除")


@router.post("/{mcp_id}/test", response_model=Msg)
def test_mcp(mcp_id: str, db: Session = Depends(get_tenant_db)):
    c = tenant_service.require_visible(db, MCPConfig, mcp_id, "MCP 不存在")
    ok, msg = mcp_service.test_connection(c)
    return Msg(ok=ok, message=msg)


@router.get("/{mcp_id}/tools", response_model=list[MCPToolInfo])
def mcp_tools(mcp_id: str, db: Session = Depends(get_tenant_db)):
    c = tenant_service.require_visible(db, MCPConfig, mcp_id, "MCP 不存在")
    try:
        return mcp_service.list_tools(c)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"获取工具失败: {exc}")
