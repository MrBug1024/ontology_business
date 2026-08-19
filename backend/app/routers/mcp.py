"""MCP 配置路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import MCPConfig
from ..schemas import MCPConfigIn, MCPConfigOut, MCPToolInfo, Msg
from ..services import mcp_service

router = APIRouter(prefix="/mcp", tags=["mcp"])


def _out(c: MCPConfig) -> MCPConfigOut:
    return MCPConfigOut(
        id=c.id,
        name=c.name,
        transport=c.transport,
        command=c.command,
        args=c.args or [],
        url=c.url,
        env=c.env or {},
        headers=c.headers or {},
        enabled=c.enabled,
        created_at=c.created_at,
    )


@router.get("", response_model=list[MCPConfigOut])
def list_mcp(db: Session = Depends(get_db)):
    return [_out(c) for c in db.execute(select(MCPConfig)).scalars().all()]


@router.post("", response_model=MCPConfigOut)
def create_mcp(payload: MCPConfigIn, db: Session = Depends(get_db)):
    c = MCPConfig(**payload.model_dump())
    db.add(c)
    db.commit()
    db.refresh(c)
    return _out(c)


@router.put("/{mcp_id}", response_model=MCPConfigOut)
def update_mcp(mcp_id: str, payload: MCPConfigIn, db: Session = Depends(get_db)):
    c = db.get(MCPConfig, mcp_id)
    if not c:
        raise HTTPException(404, "MCP 不存在")
    for k, v in payload.model_dump().items():
        setattr(c, k, v)
    db.commit()
    db.refresh(c)
    return _out(c)


@router.delete("/{mcp_id}", response_model=Msg)
def delete_mcp(mcp_id: str, db: Session = Depends(get_db)):
    c = db.get(MCPConfig, mcp_id)
    if not c:
        raise HTTPException(404, "MCP 不存在")
    db.delete(c)
    db.commit()
    return Msg(message="已删除")


@router.post("/{mcp_id}/test", response_model=Msg)
def test_mcp(mcp_id: str, db: Session = Depends(get_db)):
    c = db.get(MCPConfig, mcp_id)
    if not c:
        raise HTTPException(404, "MCP 不存在")
    ok, msg = mcp_service.test_connection(c)
    return Msg(ok=ok, message=msg)


@router.get("/{mcp_id}/tools", response_model=list[MCPToolInfo])
def mcp_tools(mcp_id: str, db: Session = Depends(get_db)):
    c = db.get(MCPConfig, mcp_id)
    if not c:
        raise HTTPException(404, "MCP 不存在")
    try:
        return mcp_service.list_tools(c)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"获取工具失败: {exc}")
