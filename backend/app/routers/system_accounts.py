"""System-wide account administration, authenticated outside workspace scope."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..access_models import AccessAuditEvent
from ..access_schemas import AccountPage, AccountUpdateIn, AuditOut
from ..database import get_db
from ..models import User
from ..schemas import Msg
from ..services import auth_service, system_account_service

router = APIRouter(prefix="/system/accounts", tags=["system-accounts"])


@router.get("", response_model=AccountPage)
def accounts(search: str = Query("", max_length=120), status: Literal["", "pending", "active", "disabled"] = "",
             offset: int = Query(0, ge=0, le=100000), limit: int = Query(20, ge=1, le=100),
             user: User = Depends(auth_service.get_current_account), db: Session = Depends(get_db)):
    return system_account_service.list_accounts(db, user.id, search.strip(), status, offset, limit)


@router.patch("/{user_id}", response_model=Msg)
def update_account(user_id: str, payload: AccountUpdateIn, user: User = Depends(auth_service.get_current_account), db: Session = Depends(get_db)):
    system_account_service.update_account(db, user.id, user_id, payload)
    db.commit()
    return Msg(message="账户权限已更新，旧凭据已撤销")


@router.get("/{user_id}/audit", response_model=list[AuditOut])
def account_audit(user_id: str, offset: int = Query(0, ge=0, le=100000), limit: int = Query(20, ge=1, le=100),
                  user: User = Depends(auth_service.get_current_account), db: Session = Depends(get_db)):
    system_account_service.require_superadmin(db, user.id)
    rows = db.execute(select(AccessAuditEvent, User.display_name).join(User, User.id == AccessAuditEvent.actor_user_id)
        .where(AccessAuditEvent.target_id == user_id, AccessAuditEvent.tenant_id.is_(None))
        .order_by(AccessAuditEvent.created_at.desc(), AccessAuditEvent.id).offset(offset).limit(limit)).all()
    return [AuditOut(id=e.id, actor_name=name, action=e.action, before_value=e.before_value,
        after_value=e.after_value, reason=e.reason, created_at=e.created_at) for e, name in rows]
