"""P1 组织、角色与细粒度授权服务。

``Tenant`` 继续是数据隔离的第一道边界；本模块只在已知的租户和用户主体上作
RBAC + ACL 判定。没有主体、成员关系或资源归属时一律拒绝，避免后台任务或直接
服务调用在没有身份的情况下意外获得执行权。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from typing import Generator, Iterable

from fastapi import HTTPException
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session, joinedload

from ..models import (
    AuthorizationGrant,
    BusinessScenario,
    OntologyAction,
    OntologyEntity,
    OntologyInstance,
    OntologyProperty,
    OntologyWorkflow,
    Organization,
    OrganizationMember,
    OrganizationRole,
    Tenant,
    User,
)


SYSTEM_ROLES: dict[str, tuple[str, str]] = {
    "owner": ("所有者", "拥有组织、角色和所有业务资源的完全控制权"),
    "admin": ("管理员", "管理成员、授权与业务资源"),
    "operator": ("操作员", "读写常规对象并执行已授权的 Action/工作流"),
    "viewer": ("查看者", "只读常规业务资源"),
}

VALID_RESOURCE_TYPES = {"scenario", "object", "property", "action", "workflow"}
VALID_VERBS = {"read", "write", "execute", "approve", "manage"}

# 默认矩阵仅覆盖普通（tenant scope）资源。owner/admin 为全量许可，受限资源仍会
# 被 deny 和 access_scope 规则收窄。
_ROLE_PERMISSIONS: dict[str, set[tuple[str, str]]] = {
    "operator": {
        ("scenario", "read"),
        ("scenario", "write"),
        ("object", "read"),
        ("object", "write"),
        ("property", "read"),
        ("action", "read"),
        ("action", "execute"),
        ("workflow", "read"),
        ("workflow", "execute"),
    },
    "viewer": {
        ("scenario", "read"),
        ("object", "read"),
        ("property", "read"),
        ("action", "read"),
        ("workflow", "read"),
    },
}


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    user_id: str
    organization_id: str
    member_id: str
    role_id: str
    role_key: str

    @property
    def role_keys(self) -> tuple[str, ...]:
        return (self.role_key,)

    @property
    def privileged(self) -> bool:
        return self.role_key in {"owner", "admin"}


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    reason: str
    role_key: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"allowed": self.allowed, "reason": self.reason, "role": self.role_key}


def _context_value(db: Session, key: str) -> str:
    return str(db.info.get(key) or "").strip()


def _request_permission_cache(db: Session) -> dict[tuple[object, ...], object]:
    """Reuse immutable permission reads within one request/session.

    Runtime list endpoints may authorize dozens of rows at once. The policy is
    unchanged, but resolving the same principal, scenario grant, and property
    grant repeatedly turns a bounded page into an N+1 query cascade.
    """
    cache = db.info.get("permission_cache")
    if not isinstance(cache, dict):
        cache = {}
        db.info["permission_cache"] = cache
    return cache


def _resolve_principal(db: Session) -> tuple[Principal | None, str, int]:
    """返回已验证主体；第三个值是错误时应返回的 HTTP 状态。"""
    tenant_id = _context_value(db, "tenant_id")
    user_id = _context_value(db, "user_id")
    cache = _request_permission_cache(db)
    principal_key = ("principal", tenant_id, user_id)
    if principal_key in cache:
        return cache[principal_key]  # type: ignore[return-value]
    if not tenant_id or not user_id:
        result = (None, "缺少经过认证的租户与用户上下文", 401)
        cache[principal_key] = result
        return result

    # ``clear_request_permission_cache`` is used after an owner-mutation lock
    # wait.  Refresh the user as well as its membership so a concurrently
    # disabled actor cannot keep using its pre-wait active status.
    user = db.get(User, user_id, populate_existing=True)
    if not user or user.status != "active" or user.tenant_id != tenant_id:
        result = (None, "当前用户不属于请求租户或已失效", 403)
        cache[principal_key] = result
        return result

    member = db.execute(
        select(OrganizationMember)
        .join(OrganizationMember.organization)
        .options(joinedload(OrganizationMember.role))
        .where(
            OrganizationMember.user_id == user_id,
            OrganizationMember.status == "active",
            Organization.tenant_id == tenant_id,
        )
        .limit(1)
        # Owner-changing endpoints may wait on the organization advisory lock.
        # When they resume, reload this session's existing member object instead
        # of authorizing with the role it held before the wait.
        .execution_options(populate_existing=True)
    ).scalars().first()
    if not member or not member.role:
        result = (None, "当前用户没有有效的组织成员身份", 403)
        cache[principal_key] = result
        return result
    result = (
        Principal(
            tenant_id=tenant_id,
            user_id=user_id,
            organization_id=member.organization_id,
            member_id=member.id,
            role_id=member.role_id,
            role_key=member.role.key,
        ),
        "",
        200,
    )
    cache[principal_key] = result
    return result


def require_principal(db: Session) -> Principal:
    principal, reason, status_code = _resolve_principal(db)
    if not principal:
        raise HTTPException(status_code=status_code, detail=reason)
    return principal


def _role_allows(principal: Principal, resource_type: str, verb: str) -> bool:
    if principal.role_key in {"owner", "admin"}:
        return True
    return (resource_type, verb) in _ROLE_PERMISSIONS.get(principal.role_key, set())


def _grant_effect(
    db: Session,
    principal: Principal,
    resource_type: str,
    resource_id: str,
    verb: str,
) -> str | None:
    """取得精确 ACL 的效果，``deny`` 始终优先于 ``allow``。"""
    cache = _request_permission_cache(db)
    grant_key = (
        "grant",
        principal.organization_id,
        principal.role_id,
        principal.user_id,
        resource_type,
        resource_id,
        verb,
    )
    if grant_key in cache:
        return cache[grant_key]  # type: ignore[return-value]
    grants = db.execute(
        select(AuthorizationGrant.effect).where(
            AuthorizationGrant.organization_id == principal.organization_id,
            AuthorizationGrant.resource_type == resource_type,
            AuthorizationGrant.resource_id.in_((resource_id, "*")),
            AuthorizationGrant.verb.in_((verb, "*")),
            or_(
                AuthorizationGrant.role_id == principal.role_id,
                AuthorizationGrant.user_id == principal.user_id,
            ),
        )
    ).scalars().all()
    normalized = {str(effect or "").lower() for effect in grants}
    if "deny" in normalized:
        cache[grant_key] = "deny"
        return "deny"
    if "allow" in normalized:
        cache[grant_key] = "allow"
        return "allow"
    cache[grant_key] = None
    return None


def _same_tenant(principal: Principal, scenario: BusinessScenario) -> bool:
    return bool(scenario.tenant_id) and str(scenario.tenant_id) == principal.tenant_id


def check_scenario(
    db: Session,
    scenario: BusinessScenario,
    verb: str = "read",
) -> PermissionDecision:
    principal, missing_reason, _ = _resolve_principal(db)
    if not principal:
        return PermissionDecision(False, missing_reason)
    if verb not in VALID_VERBS:
        return PermissionDecision(False, "不支持的权限动作", principal.role_key)

    if not _same_tenant(principal, scenario):
        if scenario.is_public and verb == "read":
            return PermissionDecision(True, "公共场景只读访问", principal.role_key)
        return PermissionDecision(False, "资源不属于当前租户", principal.role_key)

    effect = _grant_effect(db, principal, "scenario", scenario.id, verb)
    if effect == "deny":
        return PermissionDecision(False, "场景 ACL 显式拒绝", principal.role_key)
    if effect == "allow":
        return PermissionDecision(True, "场景 ACL 显式允许", principal.role_key)
    if _role_allows(principal, "scenario", verb):
        return PermissionDecision(True, "组织角色默认允许", principal.role_key)
    return PermissionDecision(False, "组织角色没有场景权限", principal.role_key)


def require_scenario_permission(
    db: Session,
    scenario: BusinessScenario,
    verb: str = "read",
    *,
    message: str = "没有该业务场景的权限",
) -> None:
    require_principal(db)
    decision = check_scenario(db, scenario, verb)
    if not decision.allowed:
        raise HTTPException(status_code=403, detail=message)


def check_tenant_permission(db: Session, verb: str = "manage") -> PermissionDecision:
    principal, missing_reason, _ = _resolve_principal(db)
    if not principal:
        return PermissionDecision(False, missing_reason)
    if verb == "manage":
        allowed = principal.role_key in {"owner", "admin"}
    elif verb == "write":
        allowed = principal.role_key in {"owner", "admin", "operator"}
    else:
        allowed = _role_allows(principal, "scenario", verb)
    return PermissionDecision(
        allowed,
        "组织角色默认允许" if allowed else "组织角色没有租户管理权限",
        principal.role_key,
    )


def require_tenant_permission(db: Session, verb: str = "manage") -> None:
    require_principal(db)
    decision = check_tenant_permission(db, verb)
    if not decision.allowed:
        raise HTTPException(status_code=403, detail="没有组织管理权限")


def _check_resource(
    db: Session,
    scenario: BusinessScenario,
    *,
    resource_type: str,
    resource_id: str,
    verb: str,
    access_scope: str = "tenant",
) -> PermissionDecision:
    principal, missing_reason, _ = _resolve_principal(db)
    if not principal:
        return PermissionDecision(False, missing_reason)
    if resource_type not in VALID_RESOURCE_TYPES or verb not in VALID_VERBS:
        return PermissionDecision(False, "不支持的资源或权限动作", principal.role_key)

    # 先处理同一租户内的 deny；所有下级资源都会受场景 deny 约束。
    if _same_tenant(principal, scenario):
        scenario_effect = _grant_effect(db, principal, "scenario", scenario.id, verb)
        resource_effect = _grant_effect(db, principal, resource_type, resource_id, verb)
        if scenario_effect == "deny" or resource_effect == "deny":
            return PermissionDecision(False, "ACL 显式拒绝", principal.role_key)
    else:
        scenario_effect = None
        resource_effect = None
        # 公共资源只保留读取语义；受限资源绝不能因公共场景而泄露。
        if not (scenario.is_public and verb == "read"):
            return PermissionDecision(False, "资源不属于当前租户", principal.role_key)
        if access_scope != "tenant":
            return PermissionDecision(False, "受限资源需要显式组织授权", principal.role_key)
        return PermissionDecision(True, "公共资源只读访问", principal.role_key)

    # 非 tenant 值按受限处理，避免未来新增 scope 时默认放行。
    if access_scope != "tenant":
        if principal.privileged:
            return PermissionDecision(True, "组织管理员访问受限资源", principal.role_key)
        if resource_effect == "allow":
            return PermissionDecision(True, "资源 ACL 显式允许", principal.role_key)
        return PermissionDecision(False, "受限资源需要显式授权", principal.role_key)

    if resource_effect == "allow":
        return PermissionDecision(True, "资源 ACL 显式允许", principal.role_key)
    if scenario_effect == "allow":
        return PermissionDecision(True, "场景 ACL 显式允许", principal.role_key)
    if _role_allows(principal, resource_type, verb):
        return PermissionDecision(True, "组织角色默认允许", principal.role_key)
    return PermissionDecision(False, "组织角色没有资源权限", principal.role_key)


def check_object(db: Session, instance: OntologyInstance, verb: str = "read") -> PermissionDecision:
    scenario = instance.scenario or db.get(BusinessScenario, instance.scenario_id)
    if not scenario:
        return PermissionDecision(False, "对象所属场景不存在")
    return _check_resource(
        db,
        scenario,
        resource_type="object",
        resource_id=instance.id,
        verb=verb,
        access_scope=instance.access_scope or "tenant",
    )


def require_object_permission(
    db: Session,
    instance: OntologyInstance,
    verb: str = "read",
) -> None:
    require_principal(db)
    if not check_object(db, instance, verb).allowed:
        raise HTTPException(status_code=403, detail="没有该对象的权限")


def check_property(db: Session, prop: OntologyProperty, verb: str = "read") -> PermissionDecision:
    entity = prop.entity or db.get(OntologyEntity, prop.entity_id)
    scenario = entity.scenario if entity else None
    if not scenario and entity:
        scenario = db.get(BusinessScenario, entity.scenario_id)
    if not entity or not scenario:
        return PermissionDecision(False, "属性所属场景不存在")
    # 敏感属性在默认矩阵之上再收窄，只有 owner/admin 或精确 allow 可读。
    access_scope = "restricted" if prop.is_sensitive else "tenant"
    return _check_resource(
        db,
        scenario,
        resource_type="property",
        resource_id=prop.id,
        verb=verb,
        access_scope=access_scope,
    )


def can_read_property(db: Session, prop: OntologyProperty) -> bool:
    return check_property(db, prop, "read").allowed


def require_property_permission(
    db: Session,
    prop: OntologyProperty,
    verb: str = "read",
) -> None:
    """Require a concrete property permission before exposing or mutating it."""
    require_principal(db)
    if not check_property(db, prop, verb).allowed:
        raise HTTPException(status_code=403, detail="没有该属性的权限")


def require_instance_attribute_write_permissions(
    db: Session,
    entity: OntologyEntity,
    attributes: dict | None,
) -> None:
    """Reject undefined attributes and require write access for every persisted field.

    Ontology instances store their values in JSON, so object-level write permission by
    itself is insufficient: without this guard a caller could create a new hidden field
    or overwrite a sensitive one.  Attribute names must correspond to the entity's
    current ontology definition; legacy values remain readable only when a matching
    property is still authorized.
    """
    values = dict(attributes or {})
    if not values:
        return
    properties = db.execute(
        select(OntologyProperty).where(OntologyProperty.entity_id == entity.id)
    ).scalars().all()
    by_name = {prop.name: prop for prop in properties}
    unknown = sorted(str(name) for name in values if name not in by_name)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"属性未在实体定义中声明: {', '.join(unknown)}",
        )
    for name in values:
        require_property_permission(db, by_name[name], "write")


def filter_instance_attributes(db: Session, instance: OntologyInstance) -> dict:
    """返回当前主体可序列化的对象属性；未知字段一律不暴露。"""
    values = dict(instance.attributes or {})
    if not values:
        return values
    # Object search eagerly loads the entity properties for the whole page.
    # Reuse that relationship instead of querying the same property definition
    # once per runtime object.
    if instance.entity is not None:
        properties = list(instance.entity.properties or [])
    else:
        properties = db.execute(
            select(OntologyProperty).where(OntologyProperty.entity_id == instance.entity_id)
        ).scalars().all()
    by_name = {prop.name: prop for prop in properties}
    return {
        name: value
        for name, value in values.items()
        if by_name.get(name) and can_read_property(db, by_name[name])
    }


def check_action(
    db: Session,
    action: OntologyAction,
    verb: str = "execute",
) -> PermissionDecision:
    scenario = action.scenario or db.get(BusinessScenario, action.scenario_id)
    if not scenario:
        return PermissionDecision(False, "操作所属场景不存在")
    return _check_resource(
        db,
        scenario,
        resource_type="action",
        resource_id=action.id,
        verb=verb,
        access_scope=action.access_scope or "tenant",
    )


def require_action_permission(db: Session, action: OntologyAction, verb: str = "execute") -> None:
    require_principal(db)
    if not check_action(db, action, verb).allowed:
        raise HTTPException(status_code=403, detail="没有执行该操作的权限")


def check_workflow(
    db: Session,
    workflow: OntologyWorkflow,
    verb: str = "execute",
) -> PermissionDecision:
    scenario = workflow.scenario or db.get(BusinessScenario, workflow.scenario_id)
    if not scenario:
        return PermissionDecision(False, "工作流所属场景不存在")
    return _check_resource(
        db,
        scenario,
        resource_type="workflow",
        resource_id=workflow.id,
        verb=verb,
        access_scope=workflow.access_scope or "tenant",
    )


def require_workflow_permission(db: Session, workflow: OntologyWorkflow, verb: str = "execute") -> None:
    require_principal(db)
    if not check_workflow(db, workflow, verb).allowed:
        raise HTTPException(status_code=403, detail="没有执行该工作流的权限")


def ensure_organization(
    db: Session,
    tenant_id: str,
    *,
    owner_user_id: str | None = None,
) -> Organization:
    """创建/回填组织、系统角色及现有用户的安全默认成员身份。"""
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise ValueError("租户不存在")
    organization = db.execute(
        select(Organization).where(Organization.tenant_id == tenant_id)
    ).scalars().first()
    created_organization = organization is None
    if not organization:
        organization = Organization(tenant_id=tenant_id, name=tenant.name)
        db.add(organization)
        db.flush()

    roles = {
        role.key: role
        for role in db.execute(
            select(OrganizationRole).where(OrganizationRole.organization_id == organization.id)
        ).scalars().all()
    }
    for key, (name, description) in SYSTEM_ROLES.items():
        if key not in roles:
            role = OrganizationRole(
                organization_id=organization.id,
                key=key,
                name=name,
                description=description,
                is_system=True,
            )
            db.add(role)
            roles[key] = role
    db.flush()

    # Membership bootstrapping is deliberately a one-time organization-creation
    # migration.  Re-running it on every login/startup would silently restore a
    # removed member as admin merely because their User row still exists.
    users = db.execute(
        select(User)
        .where(
            User.tenant_id == tenant_id,
            or_(User.status == "active", User.id == owner_user_id),
        )
        .order_by(User.created_at.asc(), User.id.asc())
    ).scalars().all()
    member_user_ids = set(
        db.execute(
            select(OrganizationMember.user_id).where(
                OrganizationMember.organization_id == organization.id
            )
        ).scalars().all()
    )
    if created_organization:
        valid_owner_id = next((user.id for user in users if user.id == owner_user_id), None)
        default_owner_id = valid_owner_id or (users[0].id if users else None)
        for user in users:
            if user.id in member_user_ids:
                continue
            db.add(
                OrganizationMember(
                    organization_id=organization.id,
                    user_id=user.id,
                    role_id=roles["owner" if user.id == default_owner_id else "admin"].id,
                    status="active",
                )
            )
    db.flush()
    return organization


def ensure_user_membership(db: Session, user: User) -> bool:
    """返回用户是否已有有效成员身份，不在登录时隐式恢复被移除成员。"""
    existing = db.execute(
        select(OrganizationMember)
        .join(OrganizationMember.organization)
        .where(
            Organization.tenant_id == user.tenant_id,
            OrganizationMember.user_id == user.id,
        )
        .limit(1)
    ).scalars().first()
    if existing:
        return existing.status == "active"
    # Organization creation/backfill is handled by ensure_organization at
    # startup/registration.  A missing row in an established organization is a
    # deliberate removal until an administrator explicitly adds the user again.
    return False


def bootstrap_authorization(db: Session) -> None:
    """应用启动时回填所有已有租户，避免升级后第一位登录者被锁在组织外。"""
    tenant_ids = db.execute(select(Tenant.id)).scalars().all()
    for tenant_id in tenant_ids:
        ensure_organization(db, tenant_id)


def organization_for_principal(db: Session) -> Organization:
    principal = require_principal(db)
    organization = db.get(Organization, principal.organization_id)
    if not organization:
        raise HTTPException(status_code=403, detail="组织不存在或已失效")
    return organization


def role_for_organization(db: Session, organization_id: str, role_key: str) -> OrganizationRole:
    role = db.execute(
        select(OrganizationRole).where(
            OrganizationRole.organization_id == organization_id,
            OrganizationRole.key == role_key,
        )
    ).scalars().first()
    if not role:
        raise HTTPException(status_code=400, detail="角色不存在")
    return role


def assign_member_role(
    db: Session,
    organization: Organization,
    *,
    user_id: str,
    role_key: str,
) -> OrganizationMember:
    user = db.get(User, user_id)
    if not user or user.tenant_id != organization.tenant_id:
        raise HTTPException(status_code=403, detail="不能向组织添加其他租户的用户")
    role = role_for_organization(db, organization.id, role_key)
    # Keep service callers on the same critical section as the HTTP endpoints.
    # This helper is also used by administrative/bootstrap integrations and
    # must not become a future bypass around the last-active-owner invariant.
    lock_organization_owner_changes(db, organization.id)
    member = db.execute(
        select(OrganizationMember)
        .options(joinedload(OrganizationMember.role), joinedload(OrganizationMember.user))
        .where(
            OrganizationMember.organization_id == organization.id,
            OrganizationMember.user_id == user_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    ).scalars().first()
    if member:
        current_role_key = str(member.role.key if member.role else "").strip().lower()
        removes_active_owner = (
            member.status == "active"
            and bool(member.user and member.user.status == "active")
            and current_role_key == "owner"
            and role.key != "owner"
        )
        if removes_active_owner and owner_count(db, organization.id) <= 1:
            raise HTTPException(status_code=409, detail="组织至少需要保留一名启用的所有者")
        member.role_id = role.id
        member.status = "active"
    else:
        member = OrganizationMember(
            organization_id=organization.id,
            user_id=user_id,
            role_id=role.id,
            status="active",
        )
        db.add(member)
    db.flush()
    return member


def owner_count(db: Session, organization_id: str) -> int:
    return len(
        db.execute(
            select(OrganizationMember.id)
            .join(OrganizationMember.role)
            .join(OrganizationMember.user)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.status == "active",
                OrganizationRole.key == "owner",
                User.status == "active",
            )
        ).scalars().all()
    )


def lock_organization_owner_changes(db: Session, organization_id: str) -> None:
    """Serialize mutations which could leave an organization without an owner.

    PostgreSQL is the only supported runtime database. A transaction-scoped
    advisory lock gives every owner-removal path one organization-wide critical
    section without depending on which member row happens to be edited. The
    row-lock fallback keeps SQLite-backed unit tests meaningful.
    """
    normalized_id = str(organization_id or "").strip()
    if not normalized_id:
        raise ValueError("组织标识不能为空")
    dialect_name = str(db.get_bind().dialect.name or "").lower()
    if dialect_name == "postgresql":
        digest = hashlib.sha256(
            f"ontology-organization-owner-v1:{normalized_id}".encode("utf-8")
        ).digest()
        lock_key = int.from_bytes(digest[:8], byteorder="big", signed=True)
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )
        return
    db.execute(
        select(OrganizationMember.id)
        .where(OrganizationMember.organization_id == normalized_id)
        .order_by(OrganizationMember.id)
        .with_for_update()
    ).scalars().all()


def clear_request_permission_cache(db: Session) -> None:
    """Force authorization to be read again after a membership lock wait."""
    db.info.pop("permission_cache", None)


def validate_grant_resource(
    db: Session,
    organization: Organization,
    *,
    resource_type: str,
    resource_id: str,
) -> BusinessScenario:
    """确认管理 API 授权的资源属于当前组织的租户。"""
    if resource_type not in VALID_RESOURCE_TYPES or resource_id == "*":
        raise HTTPException(status_code=400, detail="授权资源类型或资源标识无效")
    model = {
        "scenario": BusinessScenario,
        "object": OntologyInstance,
        "property": OntologyProperty,
        "action": OntologyAction,
        "workflow": OntologyWorkflow,
    }[resource_type]
    resource = db.get(model, resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="授权资源不存在")
    if resource_type == "scenario":
        scenario = resource
    elif resource_type == "property":
        entity = resource.entity or db.get(OntologyEntity, resource.entity_id)
        scenario = entity.scenario if entity else None
        if not scenario and entity:
            scenario = db.get(BusinessScenario, entity.scenario_id)
    else:
        scenario = resource.scenario or db.get(BusinessScenario, resource.scenario_id)
    if not scenario or scenario.tenant_id != organization.tenant_id:
        raise HTTPException(status_code=403, detail="不能授权其他租户的资源")
    return scenario


def _eligible_member(
    db: Session,
    organization_id: str,
    user_id: str | None,
) -> OrganizationMember | None:
    if not user_id:
        return None
    return db.execute(
        select(OrganizationMember)
        .join(OrganizationMember.user)
        .where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == user_id,
            OrganizationMember.status == "active",
            User.status == "active",
        )
        .limit(1)
    ).scalars().first()


@contextmanager
def execution_principal(
    db: Session,
    scenario: BusinessScenario,
    *,
    requested_user_id: str | None = None,
) -> Generator[str, None, None]:
    """为无 HTTP 会话的 worker 注入可审计的执行主体。

    优先采用创建任务的仍有效成员；定时/事件任务没有创建者时选择组织 owner。没有
    tenant、组织或有效成员时不降级为匿名执行，而是 fail-closed。
    """
    if not scenario.tenant_id:
        raise HTTPException(status_code=403, detail="工作流所属场景缺少租户归属")
    organization = db.execute(
        select(Organization).where(Organization.tenant_id == scenario.tenant_id)
    ).scalars().first()
    if not organization:
        raise HTTPException(status_code=403, detail="工作流所属组织不存在")
    member = _eligible_member(db, organization.id, requested_user_id)
    if requested_user_id and not member:
        # A user-triggered durable task must never silently escalate to the
        # owner after its requester was disabled or removed from the org.  Only
        # scheduler/event work (which has no requester) may use the owner
        # fallback below.
        raise HTTPException(status_code=403, detail="任务发起人已失效或不再属于当前组织")
    if not member:
        member = db.execute(
            select(OrganizationMember)
            .join(OrganizationMember.role)
            .join(OrganizationMember.user)
            .where(
                OrganizationMember.organization_id == organization.id,
                OrganizationMember.status == "active",
                OrganizationRole.key == "owner",
                User.status == "active",
            )
            .order_by(OrganizationMember.created_at.asc())
            .limit(1)
        ).scalars().first()
    if not member:
        raise HTTPException(status_code=403, detail="没有可用于执行工作流的组织主体")

    prior_tenant = db.info.get("tenant_id")
    prior_user = db.info.get("user_id")
    db.info["tenant_id"] = str(scenario.tenant_id)
    db.info["user_id"] = str(member.user_id)
    try:
        yield str(member.user_id)
    finally:
        if prior_tenant is None:
            db.info.pop("tenant_id", None)
        else:
            db.info["tenant_id"] = prior_tenant
        if prior_user is None:
            db.info.pop("user_id", None)
        else:
            db.info["user_id"] = prior_user


def member_rows(db: Session, organization_id: str) -> Iterable[OrganizationMember]:
    return db.execute(
        select(OrganizationMember)
        .options(joinedload(OrganizationMember.role), joinedload(OrganizationMember.user))
        .where(OrganizationMember.organization_id == organization_id)
        .order_by(OrganizationMember.created_at.asc())
    ).scalars().all()
