"""Bounded public contracts for account and workspace access."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

WorkspaceRole = Literal['owner', 'admin', 'operator', 'viewer']
SystemRole = Literal['user', 'superadmin']


class AccessDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class RevisionIn(AccessDTO):
    expected_revision: int = Field(ge=1)


class InviteIn(AccessDTO):
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(default="", max_length=120)
    role: WorkspaceRole = "operator"


class MemberRoleIn(RevisionIn):
    role: WorkspaceRole


class SwitchWorkspaceIn(AccessDTO):
    tenant_id: str = Field(min_length=1, max_length=32)


class AccountUpdateIn(RevisionIn):
    system_role: SystemRole
    status: Literal['pending', 'active', 'disabled']
    reason: str = Field(min_length=1, max_length=500)


class WorkspaceOut(AccessDTO):
    tenant_id: str
    name: str
    role: WorkspaceRole


class MemberOut(AccessDTO):
    id: str
    user_id: str
    email: str
    display_name: str
    role: WorkspaceRole
    status: str
    account_status: str
    email_verified: bool
    revision: int
    created_at: datetime
    can_edit: bool


class InvitationOut(AccessDTO):
    id: str
    workspace_name: str
    email: str
    display_name: str
    role: WorkspaceRole
    status: Literal['pending', 'accepted', 'declined', 'revoked', 'expired']
    delivery_status: str
    expires_at: datetime
    revision: int


class MemberPage(AccessDTO):
    items: list[MemberOut]
    total: int
    can_manage: bool
    role: WorkspaceRole


class InvitationPage(AccessDTO):
    items: list[InvitationOut]
    total: int


class AccountOut(AccessDTO):
    id: str
    email: str
    display_name: str
    system_role: SystemRole
    status: Literal['pending', 'active', 'disabled']
    email_verified: bool
    revision: int
    created_at: datetime
    last_login_at: datetime | None
    workspace_count: int


class AccountPage(AccessDTO):
    items: list[AccountOut]
    total: int


class AuditOut(AccessDTO):
    id: str
    actor_name: str
    action: str
    before_value: str
    after_value: str
    reason: str
    created_at: datetime
