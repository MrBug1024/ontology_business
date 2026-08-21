"""组织成员、角色与细粒度授权管理 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..database import get_db
from ..models import (
    AuthorizationGrant,
    OntologyAction,
    OntologyEntity,
    OntologyInstance,
    OntologyProperty,
    OntologyWorkflow,
    OrganizationMember,
    OrganizationRole,
    User,
)
from ..schemas import (
    Msg,
    OrganizationMemberIn,
    OrganizationMemberOut,
    OrganizationOut,
    OrganizationRoleOut,
    PermissionGrantIn,
    PermissionGrantOut,
    PermissionResourceOut,
)
from ..services import permission_service, tenant_service
from ..services.auth_service import get_current_user


router = APIRouter(
    prefix="/permissions",
    tags=["permissions"],
    dependencies=[Depends(get_current_user)],
)


def _organization(db: Session, *, manage: bool = False):
    if manage:
        permission_service.require_tenant_permission(db, "manage")
    return permission_service.organization_for_principal(db)


def _member_out(member: OrganizationMember) -> OrganizationMemberOut:
    role = member.role
    user = member.user
    return OrganizationMemberOut(
        id=member.id,
        user_id=member.user_id,
        email=user.email if user else "",
        display_name=user.display_name if user else "",
        role_id=member.role_id,
        role_key=role.key if role else "",
        role_name=role.name if role else "",
        status=member.status,
        created_at=member.created_at,
    )


def _grant_out(grant: AuthorizationGrant) -> PermissionGrantOut:
    return PermissionGrantOut(
        id=grant.id,
        organization_id=grant.organization_id,
        role_id=grant.role_id,
        role_key=grant.role.key if grant.role else "",
        user_id=grant.user_id,
        resource_type=grant.resource_type,
        resource_id=grant.resource_id,
        verb=grant.verb,
        effect=grant.effect,
        created_by_user_id=grant.created_by_user_id,
        created_at=grant.created_at,
    )


@router.get("/organization", response_model=OrganizationOut)
def get_organization(db: Session = Depends(get_db)):
    return _organization(db)


@router.get("/roles", response_model=list[OrganizationRoleOut])
def list_roles(db: Session = Depends(get_db)):
    organization = _organization(db)
    return db.execute(
        select(OrganizationRole)
        .where(OrganizationRole.organization_id == organization.id)
        .order_by(OrganizationRole.created_at.asc())
    ).scalars().all()


@router.get("/members", response_model=list[OrganizationMemberOut])
def list_members(db: Session = Depends(get_db)):
    organization = _organization(db, manage=True)
    return [_member_out(member) for member in permission_service.member_rows(db, organization.id)]


@router.post("/members", response_model=OrganizationMemberOut)
def add_or_update_member(payload: OrganizationMemberIn, db: Session = Depends(get_db)):
    organization = _organization(db, manage=True)
    current = permission_service.require_principal(db)
    target = db.get(User, payload.user_id)
    if not target or target.tenant_id != organization.tenant_id:
        raise HTTPException(status_code=403, detail="不能向组织添加其他租户的用户")
    member = db.execute(
        select(OrganizationMember)
        .options(joinedload(OrganizationMember.role), joinedload(OrganizationMember.user))
        .where(
            OrganizationMember.organization_id == organization.id,
            OrganizationMember.user_id == payload.user_id,
        )
    ).scalars().first()
    if member and member.role and member.role.key == "owner" and payload.role_key != "owner":
        if current.role_key != "owner":
            raise HTTPException(status_code=403, detail="只有所有者可以调整所有者角色")
        if permission_service.owner_count(db, organization.id) <= 1:
            raise HTTPException(status_code=409, detail="组织至少需要保留一位所有者")
    # 非 owner 不能把自己提升/降级为 owner，避免 admin 接管组织所有权。
    if payload.role_key == "owner" and current.role_key != "owner":
        raise HTTPException(status_code=403, detail="只有所有者可以授予所有者角色")
    member = permission_service.assign_member_role(
        db, organization, user_id=payload.user_id, role_key=payload.role_key
    )
    db.commit()
    db.refresh(member)
    # refresh 不保证角色/用户关系仍在本地，显式载入输出数据。
    member = db.execute(
        select(OrganizationMember)
        .options(joinedload(OrganizationMember.role), joinedload(OrganizationMember.user))
        .where(OrganizationMember.id == member.id)
    ).scalars().one()
    return _member_out(member)


@router.delete("/members/{member_id}", response_model=Msg)
def remove_member(member_id: str, db: Session = Depends(get_db)):
    organization = _organization(db, manage=True)
    current = permission_service.require_principal(db)
    member = db.execute(
        select(OrganizationMember)
        .options(joinedload(OrganizationMember.role))
        .where(
            OrganizationMember.id == member_id,
            OrganizationMember.organization_id == organization.id,
        )
    ).scalars().first()
    if not member:
        raise HTTPException(status_code=404, detail="成员不存在")
    if member.role and member.role.key == "owner":
        if current.role_key != "owner":
            raise HTTPException(status_code=403, detail="只有所有者可以移除所有者")
        if permission_service.owner_count(db, organization.id) <= 1:
            raise HTTPException(status_code=409, detail="组织至少需要保留一位所有者")
    # Preserve a removal tombstone instead of deleting the row.  Organization
    # bootstrap/login compatibility must never mistake a deliberately removed
    # same-tenant User for a legacy user and recreate it as an administrator.
    member.status = "removed"
    db.commit()
    return Msg(message="成员已移除")


@router.get("/grants", response_model=list[PermissionGrantOut])
def list_grants(db: Session = Depends(get_db)):
    organization = _organization(db, manage=True)
    grants = db.execute(
        select(AuthorizationGrant)
        .options(joinedload(AuthorizationGrant.role), joinedload(AuthorizationGrant.user))
        .where(AuthorizationGrant.organization_id == organization.id)
        .order_by(AuthorizationGrant.created_at.desc())
    ).scalars().all()
    return [_grant_out(grant) for grant in grants]


@router.post("/grants", response_model=PermissionGrantOut)
def create_grant(payload: PermissionGrantIn, db: Session = Depends(get_db)):
    organization = _organization(db, manage=True)
    if bool(payload.role_key) == bool(payload.user_id):
        raise HTTPException(status_code=400, detail="授权主体必须且只能选择一个角色或用户")
    permission_service.validate_grant_resource(
        db,
        organization,
        resource_type=payload.resource_type,
        resource_id=payload.resource_id,
    )
    role_id: str | None = None
    user_id: str | None = None
    if payload.role_key:
        role_id = permission_service.role_for_organization(
            db, organization.id, payload.role_key
        ).id
    elif payload.user_id:
        member = db.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization.id,
                OrganizationMember.user_id == payload.user_id,
                OrganizationMember.status == "active",
            )
        ).scalars().first()
        if not member:
            raise HTTPException(status_code=403, detail="授权用户不是当前组织的有效成员")
        user_id = payload.user_id

    grant = db.execute(
        select(AuthorizationGrant).where(
            AuthorizationGrant.organization_id == organization.id,
            AuthorizationGrant.role_id == role_id,
            AuthorizationGrant.user_id == user_id,
            AuthorizationGrant.resource_type == payload.resource_type,
            AuthorizationGrant.resource_id == payload.resource_id,
            AuthorizationGrant.verb == payload.verb,
        )
    ).scalars().first()
    if grant:
        grant.effect = payload.effect
    else:
        grant = AuthorizationGrant(
            organization_id=organization.id,
            role_id=role_id,
            user_id=user_id,
            resource_type=payload.resource_type,
            resource_id=payload.resource_id,
            verb=payload.verb,
            effect=payload.effect,
            created_by_user_id=permission_service.require_principal(db).user_id,
        )
        db.add(grant)
    db.commit()
    db.refresh(grant)
    grant = db.execute(
        select(AuthorizationGrant)
        .options(joinedload(AuthorizationGrant.role), joinedload(AuthorizationGrant.user))
        .where(AuthorizationGrant.id == grant.id)
    ).scalars().one()
    return _grant_out(grant)


@router.delete("/grants/{grant_id}", response_model=Msg)
def delete_grant(grant_id: str, db: Session = Depends(get_db)):
    organization = _organization(db, manage=True)
    grant = db.execute(
        select(AuthorizationGrant).where(
            AuthorizationGrant.id == grant_id,
            AuthorizationGrant.organization_id == organization.id,
        )
    ).scalars().first()
    if not grant:
        raise HTTPException(status_code=404, detail="授权规则不存在")
    db.delete(grant)
    db.commit()
    return Msg(message="授权规则已删除")


@router.get("/resources/{scenario_id}", response_model=list[PermissionResourceOut])
def list_permission_resources(scenario_id: str, db: Session = Depends(get_db)):
    """返回可被授权的稳定 ID，尤其让管理端能够选择属性级授权目标。"""
    scenario = tenant_service.require_scenario(db, scenario_id, writable=True)
    permission_service.require_scenario_permission(db, scenario, "manage")
    resources: list[PermissionResourceOut] = [
        PermissionResourceOut(
            resource_type="scenario",
            id=scenario.id,
            name=scenario.name,
            scenario_id=scenario.id,
        )
    ]
    entities = db.execute(
        select(OntologyEntity).where(OntologyEntity.scenario_id == scenario.id)
    ).scalars().all()
    entity_ids = [entity.id for entity in entities]
    for prop in db.execute(
        select(OntologyProperty).where(OntologyProperty.entity_id.in_(entity_ids or ["-"]))
    ).scalars().all():
        entity = prop.entity
        resources.append(
            PermissionResourceOut(
                resource_type="property",
                id=prop.id,
                name=f"{entity.name if entity else ''}.{prop.name}",
                scenario_id=scenario.id,
                entity_id=prop.entity_id,
                is_sensitive=bool(prop.is_sensitive),
            )
        )
    for instance in db.execute(
        select(OntologyInstance).where(OntologyInstance.scenario_id == scenario.id)
    ).scalars().all():
        resources.append(
            PermissionResourceOut(
                resource_type="object",
                id=instance.id,
                name=instance.name,
                scenario_id=scenario.id,
                entity_id=instance.entity_id,
                access_scope=instance.access_scope or "tenant",
            )
        )
    for action in db.execute(
        select(OntologyAction).where(OntologyAction.scenario_id == scenario.id)
    ).scalars().all():
        resources.append(
            PermissionResourceOut(
                resource_type="action",
                id=action.id,
                name=action.name,
                scenario_id=scenario.id,
                entity_id=action.entity_id,
                access_scope=action.access_scope or "tenant",
            )
        )
    for workflow in db.execute(
        select(OntologyWorkflow).where(OntologyWorkflow.scenario_id == scenario.id)
    ).scalars().all():
        resources.append(
            PermissionResourceOut(
                resource_type="workflow",
                id=workflow.id,
                name=workflow.name,
                scenario_id=scenario.id,
                access_scope=workflow.access_scope or "tenant",
            )
        )
    return resources
