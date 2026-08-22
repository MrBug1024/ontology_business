"""Skill 路由：扫描、同步和启用技能。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Skill
from ..schemas import Msg, SkillOut, SkillToggle
from ..services import permission_service, skill_service, tenant_service
from ..services.auth_service import get_tenant_db

router = APIRouter(prefix="/skills", tags=["skills"])


def _out(s: Skill) -> SkillOut:
    return SkillOut(
        id=s.id,
        name=s.name,
        description=s.description,
        source=s.source,
        enabled=s.enabled,
        metadata=s.meta or {},
        created_at=s.created_at,
    )


@router.get("", response_model=list[SkillOut])
def list_skills(db: Session = Depends(get_tenant_db)):
    # Keep discovery a true read.  Scanning can create/update catalog rows and
    # is intentionally available only through the protected ``/rescan`` route.
    return [_out(s) for s in db.execute(
        select(Skill).where(tenant_service.visible_clause(Skill, db)).order_by(Skill.name)
    ).scalars().all()]


@router.post("/rescan", response_model=Msg)
def rescan(db: Session = Depends(get_tenant_db)):
    permission_service.require_tenant_permission(db, "manage")
    skill_service.sync_skills_to_db(db)
    return Msg(message="技能扫描完成")


@router.put("/{skill_id}", response_model=SkillOut)
def update_skill(skill_id: str, payload: SkillToggle, db: Session = Depends(get_tenant_db)):
    permission_service.require_tenant_permission(db, "manage")
    s = tenant_service.require_owned(db, Skill, skill_id, "技能不存在")
    s.enabled = payload.enabled
    db.commit()
    db.refresh(s)
    return _out(s)
