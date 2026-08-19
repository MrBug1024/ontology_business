"""Skill 路由：扫描/同步/启用/执行技能。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Skill
from ..schemas import Msg, SkillOut, SkillToggle
from ..services import skill_service

router = APIRouter(prefix="/skills", tags=["skills"])


def _out(s: Skill) -> SkillOut:
    return SkillOut(
        id=s.id,
        name=s.name,
        description=s.description,
        path=s.path,
        source=s.source,
        enabled=s.enabled,
        metadata=s.meta or {},
        created_at=s.created_at,
    )


@router.get("", response_model=list[SkillOut])
def list_skills(db: Session = Depends(get_db)):
    skill_service.sync_skills_to_db(db)
    return [_out(s) for s in db.execute(select(Skill).order_by(Skill.name)).scalars().all()]


@router.post("/rescan", response_model=Msg)
def rescan(db: Session = Depends(get_db)):
    skill_service.sync_skills_to_db(db)
    return Msg(message="技能扫描完成")


@router.put("/{skill_id}", response_model=SkillOut)
def update_skill(skill_id: str, payload: SkillToggle, db: Session = Depends(get_db)):
    s = db.get(Skill, skill_id)
    if not s:
        raise HTTPException(404, "技能不存在")
    s.enabled = payload.enabled
    db.commit()
    db.refresh(s)
    return _out(s)


@router.post("/{skill_id}/execute")
def execute_skill(skill_id: str, payload: dict, db: Session = Depends(get_db)):
    s = db.get(Skill, skill_id)
    if not s:
        raise HTTPException(404, "技能不存在")
    args = payload.get("args", [])
    return skill_service.execute_skill(s, args)
