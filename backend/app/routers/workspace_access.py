"""Authenticated workspace adapters; account endpoints do not require membership."""
from __future__ import annotations

from typing import Annotated
from pydantic import Field

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..access_schemas import InvitationOut, InvitationPage, InviteIn, MemberPage, MemberRoleIn, RevisionIn, SwitchWorkspaceIn, WorkspaceOut
from ..database import get_db
from ..models import AuthSession, User
from ..schemas import Msg
from ..services import auth_service, workspace_service, workspace_invitation_service
from ..services.access_governance_service import audit, lock_governance
from ..services.auth_request_security import consume_limit

router = APIRouter(tags=["workspace-access"])


@router.get("/workspaces", response_model=list[WorkspaceOut])
def workspaces(user: User = Depends(auth_service.get_current_account), db: Session = Depends(get_db)):
    return workspace_service.list_workspaces(db, user)


@router.post("/workspaces/switch", response_model=Msg)
def switch_workspace(payload: SwitchWorkspaceIn, request: Request, response: Response,
                     user: User = Depends(auth_service.get_current_account), db: Session = Depends(get_db)):
    lock_governance(db)
    if user.status != "active" or not workspace_service.membership(db, user.id, payload.tenant_id):
        raise HTTPException(404, "工作区不存在或没有访问权限")
    session = db.scalar(select(AuthSession).where(AuthSession.id == request.state.auth_session_id).with_for_update())
    if not session:
        raise HTTPException(401, "登录已失效，请重新登录")
    db.execute(delete(AuthSession).where(AuthSession.id == session.id))
    audit(db, actor_id=user.id, target_id=payload.tenant_id, tenant_id=payload.tenant_id, action="workspace.switched")
    auth_service.set_session_cookie(response, user, db, active_tenant_id=payload.tenant_id)
    return Msg(message="已切换工作区")


@router.get("/workspace/members", response_model=MemberPage)
def members(offset: int = Query(0, ge=0, le=100000), limit: int = Query(20, ge=1, le=100),
            search: str = Query("", max_length=160),
            user_ids: list[Annotated[str, Field(min_length=1, max_length=32)]] | None = Query(None, max_length=100),
            db: Session = Depends(auth_service.get_tenant_db)):
    return workspace_service.list_members(db, offset, limit, search=search, user_ids=user_ids)


@router.patch("/workspace/members/{member_id}", response_model=Msg)
def member_role(member_id: str, payload: MemberRoleIn, db: Session = Depends(auth_service.get_tenant_db)):
    workspace_service.update_member(db, member_id, payload.expected_revision, payload.role)
    db.commit()
    return Msg(message="成员角色已更新，旧会话已撤销")


@router.post("/workspace/members/{member_id}/remove", response_model=Msg)
def remove_member(member_id: str, payload: RevisionIn, db: Session = Depends(auth_service.get_tenant_db)):
    workspace_service.update_member(db, member_id, payload.expected_revision, None)
    db.commit()
    return Msg(message="成员已移出工作区")


@router.get("/workspace/invitations", response_model=InvitationPage)
def workspace_invitations(offset: int = Query(0, ge=0, le=100000), limit: int = Query(20, ge=1, le=100),
                          user: User = Depends(auth_service.get_current_user), db: Session = Depends(auth_service.get_tenant_db)):
    return workspace_invitation_service.list_invitations(db, user, inbox=False, offset=offset, limit=limit)


@router.post("/workspace/invitations", response_model=InvitationOut)
def invite(payload: InviteIn, db: Session = Depends(auth_service.get_tenant_db)):
    principal = workspace_service.require_manager(db)
    consume_limit(db, f"workspace-invite:{principal.tenant_id}:{principal.user_id}", maximum=30, seconds=3600)
    result = workspace_invitation_service.create_invitation(db, payload)
    db.commit()
    return result


@router.post("/workspace/invitations/{invitation_id}/resend", response_model=Msg)
def resend(invitation_id: str, payload: RevisionIn, db: Session = Depends(auth_service.get_tenant_db)):
    principal = workspace_service.require_manager(db)
    consume_limit(db, f"workspace-invite:{principal.tenant_id}:{principal.user_id}", maximum=30, seconds=3600)
    workspace_invitation_service.manage_invitation(db, invitation_id, payload.expected_revision, True)
    db.commit()
    return Msg(message="邀请已重新创建，邮件等待投递")


@router.post("/workspace/invitations/{invitation_id}/revoke", response_model=Msg)
def revoke(invitation_id: str, payload: RevisionIn, db: Session = Depends(auth_service.get_tenant_db)):
    workspace_invitation_service.manage_invitation(db, invitation_id, payload.expected_revision, False)
    db.commit()
    return Msg(message="邀请已撤销")


@router.get("/invitations", response_model=InvitationPage)
def inbox(offset: int = Query(0, ge=0, le=100000), limit: int = Query(20, ge=1, le=100),
          user: User = Depends(auth_service.get_current_account), db: Session = Depends(get_db)):
    return workspace_invitation_service.list_invitations(db, user, inbox=True, offset=offset, limit=limit)


@router.post("/invitations/{invitation_id}/accept", response_model=Msg)
def accept(invitation_id: str, payload: RevisionIn, user: User = Depends(auth_service.get_current_account), db: Session = Depends(get_db)):
    workspace_invitation_service.respond_invitation(db, user.id, invitation_id, payload.expected_revision, True)
    db.commit()
    return Msg(message="已加入工作区，可通过顶部工作区菜单切换")


@router.post("/invitations/{invitation_id}/decline", response_model=Msg)
def decline(invitation_id: str, payload: RevisionIn, user: User = Depends(auth_service.get_current_account), db: Session = Depends(get_db)):
    workspace_invitation_service.respond_invitation(db, user.id, invitation_id, payload.expected_revision, False)
    db.commit()
    return Msg(message="已拒绝邀请")
