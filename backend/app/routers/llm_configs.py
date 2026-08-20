"""LLM 配置路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import LLMConfig
from ..schemas import LLMConfigIn, LLMConfigOut, Msg
from ..services import llm_service, tenant_service
from ..services.auth_service import get_tenant_db

router = APIRouter(prefix="/llm-configs", tags=["llm-configs"])


def _out(c: LLMConfig) -> LLMConfigOut:
    return LLMConfigOut(
        id=c.id,
        name=c.name,
        provider=c.provider,
        base_url=c.base_url,
        # API 只返回空值；编辑时留空表示保留已有密钥。
        api_key="",
        model=c.model,
        temperature=c.temperature,
        max_tokens=c.max_tokens,
        is_default=c.is_default,
        created_at=c.created_at,
    )


@router.get("", response_model=list[LLMConfigOut])
def list_llm(db: Session = Depends(get_tenant_db)):
    stmt = select(LLMConfig).where(tenant_service.visible_clause(LLMConfig, db))
    return [_out(c) for c in db.execute(stmt).scalars().all()]


@router.post("", response_model=LLMConfigOut)
def create_llm(payload: LLMConfigIn, db: Session = Depends(get_tenant_db)):
    if payload.is_default:
        for c in db.execute(
            select(LLMConfig).where(LLMConfig.tenant_id == tenant_service.current_tenant_id(db))
        ).scalars().all():
            c.is_default = False
    c = LLMConfig(tenant_id=tenant_service.current_tenant_id(db), **payload.model_dump())
    db.add(c)
    db.commit()
    db.refresh(c)
    return _out(c)


@router.put("/{cfg_id}", response_model=LLMConfigOut)
def update_llm(cfg_id: str, payload: LLMConfigIn, db: Session = Depends(get_tenant_db)):
    c = tenant_service.require_owned(db, LLMConfig, cfg_id, "配置不存在")
    if payload.is_default:
        for other in db.execute(
            select(LLMConfig).where(LLMConfig.tenant_id == tenant_service.current_tenant_id(db))
        ).scalars().all():
            other.is_default = False
    values = payload.model_dump()
    if not values.get("api_key"):
        values["api_key"] = c.api_key
    for k, v in values.items():
        setattr(c, k, v)
    db.commit()
    db.refresh(c)
    return _out(c)


@router.delete("/{cfg_id}", response_model=Msg)
def delete_llm(cfg_id: str, db: Session = Depends(get_tenant_db)):
    c = tenant_service.require_owned(db, LLMConfig, cfg_id, "配置不存在")
    db.delete(c)
    db.commit()
    return Msg(message="已删除")


@router.post("/{cfg_id}/test", response_model=Msg)
def test_llm(cfg_id: str, db: Session = Depends(get_tenant_db)):
    c = tenant_service.require_visible(db, LLMConfig, cfg_id, "配置不存在")
    ok, msg = llm_service.test_connection(c)
    return Msg(ok=ok, message=msg)
