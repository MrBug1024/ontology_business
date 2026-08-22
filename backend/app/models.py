"""ORM models for the ontology business agent platform."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    event,
    Float,
    ForeignKey,
    Index,
    inspect,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import get_settings
from .database import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _assistant_attachment_expiry() -> datetime:
    from datetime import timedelta

    return _now() + timedelta(hours=24)


def _runtime_environment_default() -> str:
    """Keep direct ORM-created runs aligned with this server deployment."""
    return get_settings().runtime_environment


# ──────────────────────────────────────────────
# 租户、用户与认证
# ──────────────────────────────────────────────
class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    users: Mapped[list["User"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    organization: Mapped["Organization | None"] = relationship(
        back_populates="tenant", uselist=False, cascade="all, delete-orphan"
    )
    mapping_refresh_jobs: Mapped[list["DataMappingRefreshJob"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending / active / disabled
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    tenant: Mapped[Tenant] = relationship(back_populates="users")
    sessions: Mapped[list["AuthSession"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    organization_memberships: Mapped[list["OrganizationMember"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    authorization_grants: Mapped[list["AuthorizationGrant"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="AuthorizationGrant.user_id",
    )


class Organization(Base):
    """租户内的权限主体容器。

    现有 ``Tenant`` 仍是数据隔离边界；一对一的 Organization 只承载成员、角色和
    授权规则，避免把 RBAC 状态散落在各业务对象上。
    """

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), unique=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    tenant: Mapped[Tenant] = relationship(back_populates="organization")
    roles: Mapped[list["OrganizationRole"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    members: Mapped[list["OrganizationMember"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    grants: Mapped[list["AuthorizationGrant"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )


class OrganizationRole(Base):
    """组织内可持久化角色；系统角色以稳定 key 驱动默认授权矩阵。"""

    __tablename__ = "organization_roles"
    __table_args__ = (
        UniqueConstraint("organization_id", "key", name="uq_organization_role_key"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(100), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    is_system: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    organization: Mapped[Organization] = relationship(back_populates="roles")
    members: Mapped[list["OrganizationMember"]] = relationship(back_populates="role")
    grants: Mapped[list["AuthorizationGrant"]] = relationship(back_populates="role")


class OrganizationMember(Base):
    """用户在当前租户组织中的唯一成员身份。"""

    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_organization_member_user"),
        Index("ix_organization_members_org_role", "organization_id", "role_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role_id: Mapped[str] = mapped_column(
        ForeignKey("organization_roles.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    organization: Mapped[Organization] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="organization_memberships")
    role: Mapped[OrganizationRole] = relationship(back_populates="members")


class AuthorizationGrant(Base):
    """对象、属性、Action 与工作流的精确 allow/deny 授权。

    每条规则只指向一个角色或一个用户（由服务层校验）；``deny`` 比默认角色权限和
    ``allow`` 优先，保证敏感字段或受限对象可以可靠收窄访问面。
    """

    __tablename__ = "authorization_grants"
    __table_args__ = (
        Index(
            "ix_authorization_grants_lookup",
            "organization_id",
            "resource_type",
            "resource_id",
            "verb",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    role_id: Mapped[str | None] = mapped_column(
        ForeignKey("organization_roles.id", ondelete="CASCADE"), nullable=True, index=True
    )
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    verb: Mapped[str] = mapped_column(String(30), nullable=False)
    effect: Mapped[str] = mapped_column(String(10), default="allow")
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    organization: Mapped[Organization] = relationship(back_populates="grants")
    role: Mapped[OrganizationRole | None] = relationship(back_populates="grants")
    user: Mapped[User | None] = relationship(
        back_populates="authorization_grants", foreign_keys=[user_id]
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    user: Mapped[User] = relationship(back_populates="sessions")


class EmailVerificationCode(Base):
    __tablename__ = "email_verification_codes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    purpose: Mapped[str] = mapped_column(String(30), default="register")  # register / password_reset
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ──────────────────────────────────────────────
# 业务场景 & 本体
# ──────────────────────────────────────────────
class BusinessScenario(Base):
    __tablename__ = "business_scenarios"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=True
    )
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    industry: Mapped[str] = mapped_column(String(100), default="")
    namespace: Mapped[str] = mapped_column(String(180), default="default")
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft / active
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    entities: Mapped[list[OntologyEntity]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    relations: Mapped[list[OntologyRelation]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    data_sources: Mapped[list[DataSource]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    instances: Mapped[list[OntologyInstance]] = relationship(
        cascade="all, delete-orphan"
    )
    relation_instances: Mapped[list[RelationInstance]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    data_mappings: Mapped[list[DataMapping]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    function_definitions: Mapped[list["FunctionDefinition"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    actions: Mapped[list["OntologyAction"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    rules: Mapped[list["OntologyRule"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    events: Mapped[list["OntologyEvent"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    workflows: Mapped[list["OntologyWorkflow"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    execution_logs: Mapped[list["ActionExecutionLog"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    workflow_runs: Mapped[list["WorkflowRun"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    event_envelopes: Mapped[list["EventEnvelope"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    ontology_branches: Mapped[list["OntologyBranch"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    ontology_snapshots: Mapped[list["OntologySnapshot"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    ontology_proposals: Mapped[list["OntologyProposal"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    ontology_releases: Mapped[list["OntologyRelease"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    ontology_rollbacks: Mapped[list["OntologyRollback"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    mapping_refresh_jobs: Mapped[list["DataMappingRefreshJob"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    incident_cases: Mapped[list["IncidentCase"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    advanced_assets: Mapped[list["OntologyAdvancedAsset"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    tenant: Mapped[Tenant | None] = relationship()


class OntologyEntity(Base):
    """本体中的实体类型（Object Type），例如对象、事项、资源等领域概念。"""

    __tablename__ = "ontology_entities"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    namespace: Mapped[str] = mapped_column(String(180), default="default")
    description: Mapped[str] = mapped_column(Text, default="")
    icon: Mapped[str] = mapped_column(String(50), default="box")
    color: Mapped[str] = mapped_column(String(20), default="#4f46e5")
    is_abstract: Mapped[bool] = mapped_column(Boolean, default=False)
    state_property: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    scenario: Mapped[BusinessScenario] = relationship(back_populates="entities")
    properties: Mapped[list[OntologyProperty]] = relationship(
        back_populates="entity", cascade="all, delete-orphan"
    )
    instances: Mapped[list["OntologyInstance"]] = relationship(
        back_populates="entity", cascade="all, delete-orphan"
    )
    data_mappings: Mapped[list["DataMapping"]] = relationship(
        back_populates="entity", cascade="all, delete-orphan"
    )
    actions: Mapped[list["OntologyAction"]] = relationship(
        back_populates="entity", cascade="all, delete-orphan"
    )
    rules: Mapped[list["OntologyRule"]] = relationship(
        back_populates="entity", cascade="all, delete-orphan"
    )


class OntologyProperty(Base):
    """实体属性（Property）。"""

    __tablename__ = "ontology_properties"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    entity_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_entities.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    data_type: Mapped[str] = mapped_column(String(50), default="string")
    description: Mapped[str] = mapped_column(Text, default="")
    is_key: Mapped[bool] = mapped_column(Boolean, default=False)
    is_required: Mapped[bool] = mapped_column(Boolean, default=False)
    is_enum: Mapped[bool] = mapped_column(Boolean, default=False)
    enum_values: Mapped[list] = mapped_column(JSON, default=list)
    # JSON preserves typed defaults (number/boolean/object/list) instead of
    # forcing everything through a string that can bypass runtime type checks.
    default_value: Mapped[object] = mapped_column(JSON, default="")
    constraints: Mapped[dict] = mapped_column(JSON, default=dict)
    # 敏感属性默认仅 owner/admin 可见；其他成员需通过 AuthorizationGrant 显式授权。
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)

    entity: Mapped[OntologyEntity] = relationship(back_populates="properties")


class OntologyRelation(Base):
    """实体间关系（Link Type）。"""

    __tablename__ = "ontology_relations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    namespace: Mapped[str] = mapped_column(String(180), default="default")
    source_entity_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_entities.id", ondelete="CASCADE"), index=True
    )
    target_entity_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_entities.id", ondelete="CASCADE"), index=True
    )
    relation_type: Mapped[str] = mapped_column(String(10), default="1:N")  # 1:1 / 1:N / N:M
    description: Mapped[str] = mapped_column(Text, default="")

    scenario: Mapped[BusinessScenario] = relationship(back_populates="relations")
    source_entity: Mapped[OntologyEntity] = relationship(foreign_keys=[source_entity_id])
    target_entity: Mapped[OntologyEntity] = relationship(foreign_keys=[target_entity_id])
    relation_instances: Mapped[list["RelationInstance"]] = relationship(
        back_populates="relation", cascade="all, delete-orphan"
    )


class OntologyInstance(Base):
    """实体实例（Object）：本体中某个实体类型的一条真实业务记录。"""

    __tablename__ = "ontology_instances"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    entity_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_entities.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)  # 展示名
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)  # 属性值
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual / imported
    source_ref: Mapped[str] = mapped_column(String(500), default="")  # 来源引用（表.行 等）
    # 内部血缘快照（mapping_id/data_source_id/table/key 等）；对外 DTO 保持兼容。
    source_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(120), default="", index=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    quality: Mapped[dict] = mapped_column(JSON, default=dict)
    # tenant: 遵循场景角色矩阵；restricted: 非 owner/admin 必须获得对象级显式授权。
    access_scope: Mapped[str] = mapped_column(String(20), default="tenant")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    scenario: Mapped[BusinessScenario] = relationship(overlaps="instances")
    entity: Mapped[OntologyEntity] = relationship()
    source_instances: Mapped[list["RelationInstance"]] = relationship(
        "RelationInstance", foreign_keys="RelationInstance.source_instance_id"
    )
    target_instances: Mapped[list["RelationInstance"]] = relationship(
        "RelationInstance", foreign_keys="RelationInstance.target_instance_id"
    )


class RelationInstance(Base):
    """关系实例（Link）：两个实例之间的一条真实关系记录。"""

    __tablename__ = "relation_instances"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    relation_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_relations.id", ondelete="CASCADE"), index=True
    )
    source_instance_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_instances.id", ondelete="CASCADE"), index=True
    )
    target_instance_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_instances.id", ondelete="CASCADE"), index=True
    )
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    scenario: Mapped[BusinessScenario] = relationship(back_populates="relation_instances")
    relation: Mapped[OntologyRelation] = relationship()
    source_instance: Mapped[OntologyInstance] = relationship(
        foreign_keys=[source_instance_id], overlaps="source_instances"
    )
    target_instance: Mapped[OntologyInstance] = relationship(
        foreign_keys=[target_instance_id], overlaps="target_instances"
    )


class DataMapping(Base):
    """数据映射：本体实体/属性 ↔ 数据源表/列（Palantir 式语义层绑定）。"""

    __tablename__ = "data_mappings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    entity_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_entities.id", ondelete="CASCADE"), index=True
    )
    data_source_id: Mapped[str] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE"), index=True
    )
    # 物理数据源仅用于开发环境兼容与定义预览。非开发环境刷新由逻辑绑定键
    # 解析到该环境已发布、已验签的连接器，不能据此直接选中 dev 数据源。
    data_source_binding_key: Mapped[str] = mapped_column(String(180), default="")
    data_source_binding_ref: Mapped[dict] = mapped_column(JSON, default=dict)
    table_name: Mapped[str] = mapped_column(String(300), default="")
    column_map: Mapped[dict] = mapped_column(JSON, default=dict)  # {本体属性名: 表列名}
    transform_rules: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="unknown")  # unknown / ready / ok / error
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_row_count: Mapped[int] = mapped_column(Integer, default=0)
    last_imported_count: Mapped[int] = mapped_column(Integer, default=0)
    # 按部署环境保存刷新状态；旧顶层字段继续承载 dev 的兼容视图，避免共享
    # 数据库中的 staging/prod worker 覆盖开发环境的可见状态。
    environment_status: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    scenario: Mapped[BusinessScenario] = relationship(back_populates="data_mappings")
    entity: Mapped[OntologyEntity] = relationship()
    data_source: Mapped[DataSource] = relationship()


# ──────────────────────────────────────────────
# 数据源
# ──────────────────────────────────────────────
class DataSource(Base):
    """数据源：关系型数据库（mysql/postgres/sqlite）或文件桶（file_bucket）。"""

    __tablename__ = "data_sources"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=True
    )
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    scenario_id: Mapped[str | None] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[str] = mapped_column(String(30), nullable=False)  # mysql / postgres / sqlite / file_bucket
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    # A durable, monotonic revision of all runtime-relevant configuration.
    # Releases record this value instead of storing an unsafe configuration
    # fingerprint that could reveal credentials.
    connector_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(20), default="unknown")  # unknown / ok / error
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    scenario: Mapped[BusinessScenario | None] = relationship(back_populates="data_sources")
    files: Mapped[list[BucketFile]] = relationship(
        back_populates="data_source", cascade="all, delete-orphan"
    )
    tenant: Mapped[Tenant | None] = relationship()

    __mapper_args__ = {
        # The revision is advanced by the listener below.  Keeping it as an
        # optimistic version column prevents two concurrent configuration
        # updates from silently producing the same revision.
        "version_id_col": connector_revision,
        "version_id_generator": False,
    }


class BucketFile(Base):
    """文件桶中的业务文件、解析结果和可追溯检索索引状态。"""

    __tablename__ = "bucket_files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    data_source_id: Mapped[str] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    size: Mapped[int] = mapped_column(Integer, default=0)
    mime: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending / parsed / error
    error: Mapped[str] = mapped_column(Text, default="")
    parsed_text: Mapped[str] = mapped_column(Text, default="")
    # P1 RAG 索引：解析文本变化后按内容哈希增量重建分块向量。
    index_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending / queued / indexed / partial / error
    index_error: Mapped[str] = mapped_column(Text, default="")
    index_version: Mapped[str] = mapped_column(String(80), default="")
    indexed_content_hash: Mapped[str] = mapped_column(String(64), default="")
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    data_source: Mapped[DataSource] = relationship(back_populates="files")
    document_chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="bucket_file", cascade="all, delete-orphan"
    )


class DocumentChunk(Base):
    """已解析文档的可引用分块与向量表示。

    原始文本仍保留在 ``BucketFile``；这里保存稳定的字符偏移，令 AI 回答、
    搜索预览与审计记录能引用到同一份资料的精确片段。
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        Index("ix_document_chunks_source_file", "data_source_id", "bucket_file_id"),
        Index("uq_document_chunks_file_ordinal", "bucket_file_id", "ordinal", unique=True),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    bucket_file_id: Mapped[str] = mapped_column(
        ForeignKey("bucket_files.id", ondelete="CASCADE"), index=True
    )
    data_source_id: Mapped[str] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    # JSON 保证 SQLite / Postgres 均可用；服务层统一按余弦相似度检索。
    embedding: Mapped[list] = mapped_column(JSON, default=list)
    embedding_model: Mapped[str] = mapped_column(String(120), default="local-semantic-hash-192-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    bucket_file: Mapped[BucketFile] = relationship(back_populates="document_chunks")
    data_source: Mapped[DataSource] = relationship()


class DataMappingRefreshJob(Base):
    """持久化的数据映射单批刷新任务。

    映射定义在 HTTP 请求中只负责入队；实际读取外部数据源、写入对象实例和关系
    由 worker 执行。``mapping_id`` 不设外键，避免编辑映射（当前为删除旧映射再新建）
    时丢失可追溯的已取消任务记录。
    """

    __tablename__ = "data_mapping_refresh_jobs"
    __table_args__ = (
        Index("ix_mapping_refresh_jobs_dispatch", "environment", "status", "available_at"),
        Index("ix_mapping_refresh_jobs_mapping_created", "mapping_id", "created_at"),
        UniqueConstraint("tenant_id", "active_key", name="uq_mapping_refresh_jobs_active_key"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True, nullable=False
    )
    mapping_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    requested_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    environment: Mapped[str] = mapped_column(String(20), default=_runtime_environment_default, index=True)
    # 同一映射同一环境只保留一个活跃任务；终态会清空，允许下一次刷新。
    active_key: Mapped[str | None] = mapped_column(String(260), nullable=True)
    # Immutable, credential-free mapping definition captured when the job is
    # queued.  A staging/prod worker must never reread mutable live mapping
    # fields after a release has been selected.
    mapping_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    mapping_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    # Release provenance for a frozen mapping job.  ``dev`` jobs explicitly
    # retain ``live`` as their source while still carrying mapping_snapshot so
    # queue/retry work cannot drift with later edits.
    definition_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_snapshots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    release_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_releases.id", ondelete="SET NULL"), nullable=True, index=True
    )
    definition_hash: Mapped[str] = mapped_column(String(64), default="")
    definition_source: Mapped[str] = mapped_column(String(20), default="live")
    limit: Mapped[int] = mapped_column(Integer, default=50)
    # queued / running / retry_waiting / succeeded / failed / timed_out / cancelled
    status: Mapped[str] = mapped_column(String(24), default="queued")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=300)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rows_scanned: Mapped[int] = mapped_column(Integer, default=0)
    instances_created: Mapped[int] = mapped_column(Integer, default=0)
    instances_updated: Mapped[int] = mapped_column(Integer, default=0)
    relations_created: Mapped[int] = mapped_column(Integer, default=0)
    # 仅存逻辑键、目标 ID、适配器等无密钥连接器审计事实。
    connector_audit: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    tenant: Mapped[Tenant] = relationship(back_populates="mapping_refresh_jobs")
    scenario: Mapped[BusinessScenario] = relationship(back_populates="mapping_refresh_jobs")


class DocumentIndexJob(Base):
    """持久化的文档解析/索引任务。

    文件上传 API 只负责可靠落盘和入队；实际解析、分块与向量生成由 P1 worker
    执行，使重启、重试和超时对文档处理与工作流运行同样生效。
    """

    __tablename__ = "document_index_jobs"
    __table_args__ = (
        Index("ix_document_index_jobs_dispatch", "status", "available_at"),
        Index("ix_document_index_jobs_file_created", "bucket_file_id", "created_at"),
        # 活跃任务使用同一个 logical key，终态任务清空该字段。这样在 SQLite
        # 及其他支持唯一索引的数据库中都能防止同一文件重复入队。
        UniqueConstraint("tenant_id", "active_key", name="uq_document_index_jobs_active_key"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    data_source_id: Mapped[str] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE"), index=True, nullable=False
    )
    bucket_file_id: Mapped[str] = mapped_column(
        ForeignKey("bucket_files.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # True: 从存储文件重新解析后索引；False: 使用当前 parsed_text 重建索引。
    parse_document: Mapped[bool] = mapped_column(Boolean, default=True)
    force: Mapped[bool] = mapped_column(Boolean, default=False)
    # queued / running / retry_waiting 时为 bucket_file_id；终态置为 NULL，借助
    # SQL UNIQUE 对多个 NULL 的语义允许之后再次为同一文件建索引。
    active_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # queued / running / retry_waiting / succeeded / failed / timed_out
    status: Mapped[str] = mapped_column(String(24), default="queued")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=300)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    tenant: Mapped[Tenant] = relationship()
    data_source: Mapped[DataSource] = relationship()
    bucket_file: Mapped[BucketFile] = relationship()


# ──────────────────────────────────────────────
# LLM 配置
# ──────────────────────────────────────────────
class LLMConfig(Base):
    """可路由的模型部署配置。

    ``api_key`` 仅保存在服务端；运行指标通过 ``LLMInvocationTrace`` 保存，
    但绝不保存完整提示词、回复或凭据。
    """

    __tablename__ = "llm_configs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=True
    )
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), default="openai")  # openai 兼容协议
    base_url: Mapped[str] = mapped_column(String(500), default="")
    api_key: Mapped[str] = mapped_column(String(500), default="")
    # See ``DataSource.connector_revision``.  API keys are deliberately not
    # copied to a release audit; changing one still advances this revision.
    connector_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    model: Mapped[str] = mapped_column(String(200), default="")
    temperature: Mapped[float] = mapped_column(Float, default=0.2)
    max_tokens: Mapped[int] = mapped_column(Integer, default=4096)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # chat / embedding / vision / tool。旧配置默认保留 chat + tool，避免升级后
    # 已绑定 Agent 的工具调用被意外禁用。
    capabilities: Mapped[list] = mapped_column(JSON, default=lambda: ["chat", "tool"])
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # 数字越小路由优先级越高；同优先级时优先默认模型。
    routing_priority: Mapped[int] = mapped_column(Integer, default=100, index=True)
    # 以每百万 token 计价，0 表示尚未配置计费。预算为 0 时表示不设上限。
    input_cost_per_million: Mapped[float] = mapped_column(Float, default=0.0)
    output_cost_per_million: Mapped[float] = mapped_column(Float, default=0.0)
    budget_limit: Mapped[float] = mapped_column(Float, default=0.0)
    cost_currency: Mapped[str] = mapped_column(String(12), default="USD")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    tenant: Mapped[Tenant | None] = relationship()
    traces: Mapped[list["LLMInvocationTrace"]] = relationship(back_populates="llm_config")
    evaluations: Mapped[list["LLMEvaluationRecord"]] = relationship(back_populates="llm_config")

    __mapper_args__ = {
        "version_id_col": connector_revision,
        "version_id_generator": False,
    }


class LLMInvocationTrace(Base):
    """一次真实模型调用的最小可审计元数据。

    不持久化 prompt、completion、工具参数或 API key，防止把业务敏感内容复制到
    运营日志中。provider/model 为调用时快照，避免配置后来修改影响历史报表。
    """

    __tablename__ = "llm_invocation_traces"
    __table_args__ = (
        Index("ix_llm_traces_config_created", "llm_config_id", "created_at"),
        Index("ix_llm_traces_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # 调用者租户；公共模型由其他租户调用时也可按调用租户归集成本。
    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), index=True, nullable=True
    )
    llm_config_id: Mapped[str | None] = mapped_column(
        ForeignKey("llm_configs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    provider: Mapped[str] = mapped_column(String(50), default="")
    model: Mapped[str] = mapped_column(String(200), default="")
    capability: Mapped[str] = mapped_column(String(20), default="chat")
    operation: Mapped[str] = mapped_column(String(30), default="chat")  # chat / chat_stream / test
    status: Mapped[str] = mapped_column(String(20), default="succeeded")  # succeeded / failed / cancelled
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(12), default="USD")
    tool_count: Mapped[int] = mapped_column(Integer, default=0)
    # 仅保存稳定 ID，不保存 prompt/回复/参数；可把一次模型调用审计回链到具体
    # Agent、会话、场景和请求，而不会复制业务敏感内容。
    correlation_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    agent_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    scenario_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 错误已在服务层去敏与截断，不能写入完整 provider 响应。
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    llm_config: Mapped[LLMConfig | None] = relationship(back_populates="traces")
    tenant: Mapped[Tenant | None] = relationship()


class LLMEvaluationRecord(Base):
    """人工或外部评测导入的基础指标记录，不保存评测原始提示词。"""

    __tablename__ = "llm_evaluation_records"
    __table_args__ = (
        Index("ix_llm_evaluations_config_created", "llm_config_id", "created_at"),
        Index("ix_llm_evaluations_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), index=True, nullable=True
    )
    llm_config_id: Mapped[str | None] = mapped_column(
        ForeignKey("llm_configs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(200), default="基础评测")
    capability: Mapped[str] = mapped_column(String(20), default="chat")
    passed: Mapped[bool] = mapped_column(Boolean, default=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(12), default="USD")
    # 仅保存非敏感评价摘要/标签；API 会限制长度。
    notes: Mapped[str] = mapped_column(Text, default="")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    llm_config: Mapped[LLMConfig | None] = relationship(back_populates="evaluations")
    tenant: Mapped[Tenant | None] = relationship()


# ──────────────────────────────────────────────
# Skill & MCP
# ──────────────────────────────────────────────
class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=True
    )
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="builtin")  # builtin / uploaded
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    tenant: Mapped[Tenant | None] = relationship()


class MCPConfig(Base):
    __tablename__ = "mcp_configs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=True
    )
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    transport: Mapped[str] = mapped_column(String(20), default="stdio")  # stdio / sse / streamable_http
    command: Mapped[str] = mapped_column(String(500), default="")
    args: Mapped[list] = mapped_column(JSON, default=list)
    url: Mapped[str] = mapped_column(String(500), default="")
    env: Mapped[dict] = mapped_column(JSON, default=dict)
    headers: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    connector_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    tenant: Mapped[Tenant | None] = relationship()

    __mapper_args__ = {
        "version_id_col": connector_revision,
        "version_id_generator": False,
    }


# Connector configurations are runtime authority.  A public-shape signature
# intentionally does not contain credentials or endpoint values, so it cannot
# alone identify a safe target after an edit.  The version below is the durable
# opaque pin used by bindings and release audit records.  It is changed only by
# actual connector configuration updates, never by health/status bookkeeping.
_CONNECTOR_REVISION_FIELDS: dict[type, tuple[str, ...]] = {
    DataSource: (
        "tenant_id", "is_public", "scenario_id", "name", "type", "config",
    ),
    MCPConfig: (
        "tenant_id", "is_public", "name", "transport", "command", "args",
        "url", "env", "headers", "enabled",
    ),
    LLMConfig: (
        "tenant_id", "is_public", "name", "provider", "base_url", "api_key",
        "model", "temperature", "max_tokens", "is_default", "capabilities",
        "enabled", "routing_priority", "input_cost_per_million",
        "output_cost_per_million", "budget_limit", "cost_currency",
    ),
}


def _positive_connector_revision(value: object) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _advance_connector_revision(_mapper, _connection, target) -> None:
    """Advance an opaque runtime pin without trusting caller-supplied values."""
    state = inspect(target)
    revision = state.attrs.connector_revision
    previous = _positive_connector_revision(
        revision.history.deleted[0] if revision.history.deleted else getattr(target, "connector_revision", 1)
    )
    changed = any(
        state.attrs[field].history.has_changes()
        for field in _CONNECTOR_REVISION_FIELDS[type(target)]
    )
    if changed:
        # Do not let an ORM caller manually roll the revision back (or skip a
        # value) while changing a target configuration.
        target.connector_revision = previous + 1
    elif revision.history.has_changes():
        # Revision is managed only here; a standalone assignment must not
        # create a fake version or restore an old release-compatible number.
        target.connector_revision = previous


for _connector_model in _CONNECTOR_REVISION_FIELDS:
    event.listen(_connector_model, "before_update", _advance_connector_revision)


# ──────────────────────────────────────────────
# Agent & 对话
# ──────────────────────────────────────────────
class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    scenario_id: Mapped[str | None] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="SET NULL"), index=True
    )
    llm_config_id: Mapped[str | None] = mapped_column(
        ForeignKey("llm_configs.id", ondelete="SET NULL"), index=True
    )
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    skill_ids: Mapped[list] = mapped_column(JSON, default=list)
    mcp_ids: Mapped[list] = mapped_column(JSON, default=list)
    data_source_ids: Mapped[list] = mapped_column(JSON, default=list)
    temperature: Mapped[float] = mapped_column(Float, default=0.2)
    max_tokens: Mapped[int] = mapped_column(Integer, default=4096)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    scenario: Mapped[BusinessScenario | None] = relationship()
    llm_config: Mapped[LLMConfig | None] = relationship()
    conversations: Mapped[list[Conversation]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )
    tenant: Mapped[Tenant | None] = relationship()


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    # Agent definitions are collaborative tenant resources, while a chat
    # transcript is a user's private working context.  Nullable supports a
    # fail-closed migration for legacy rows whose creator cannot be proven.
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    title: Mapped[str] = mapped_column(String(300), default="新对话")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    agent: Mapped[Agent] = relationship(back_populates="conversations")
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="Message.created_at"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user / assistant / tool
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list] = mapped_column(JSON, default=list)
    tool_results: Mapped[list] = mapped_column(JSON, default=list)
    # Agent 检索到的稳定资料引用。保留 file/chunk/字符偏移，令历史消息仍可追溯原文。
    citations: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


# ──────────────────────────────────────────────
# 全局 AI 助手：上下文会话、临时附件与变更审计
# ──────────────────────────────────────────────
class AssistantThread(Base):
    """带业务上下文范围的助手会话。助手只在当前租户和上下文范围内可见。"""

    __tablename__ = "assistant_threads"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    # 助手会话属于创建者，而不只是属于同一租户。否则同租户任意成员可枚举、
    # 读取或删除其他人的上下文与提案。
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    scenario_id: Mapped[str | None] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 由场景 ID + 页面路径组成，防止同一场景不同工作区串用会话。
    scope_key: Mapped[str] = mapped_column(String(700), default="global", index=True)
    title: Mapped[str] = mapped_column(String(300), default="新的助手任务")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    messages: Mapped[list["AssistantMessage"]] = relationship(
        back_populates="thread", cascade="all, delete-orphan", order_by="AssistantMessage.created_at"
    )


class AssistantMessage(Base):
    """助手会话消息，同时保存上下文和 AI 生成的待确认变更。"""

    __tablename__ = "assistant_messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_threads.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user / assistant / system
    content: Mapped[str] = mapped_column(Text, default="")
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    proposal: Mapped[dict] = mapped_column(JSON, default=dict)
    thinking: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    thread: Mapped[AssistantThread] = relationship(back_populates="messages")


class AssistantProposalApplication(Base):
    """Atomic, replayable claim for applying one assistant Change Set.

    ``proposal_id`` is generated server-side and globally unique.  Making it
    the primary key turns two concurrent confirmations into one transaction
    that owns the write and one unique-key replay/conflict, including on
    SQLite where ``SELECT FOR UPDATE`` is unavailable.
    """

    __tablename__ = "assistant_proposal_applications"

    proposal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_threads.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_messages.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="applying")
    applied_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AssistantAttachment(Base):
    """助手临时附件：只保存解析后的文本，用户确认后再提升为正式数据源。"""

    __tablename__ = "assistant_attachments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    # 临时附件也属于上传者，不能因为同租户而被其他会话任意引用或读取。
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("assistant_threads.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    mime: Mapped[str] = mapped_column(String(200), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending / parsed / error
    parsed_text: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_assistant_attachment_expiry, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AssistantAuditLog(Base):
    """记录助手生成或应用变更时的用户、上下文和结果。"""

    __tablename__ = "assistant_audit_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    scenario_id: Mapped[str | None] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="SET NULL"), index=True, nullable=True
    )
    thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("assistant_threads.id", ondelete="SET NULL"), index=True, nullable=True
    )
    operation: Mapped[str] = mapped_column(String(50), default="chat")
    status: Mapped[str] = mapped_column(String(20), default="success")
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ──────────────────────────────────────────────
# 本体扩展：操作 / 规则 / 事件 / 工作流
# （元模型层：平台只提供框架，业务语义由用户定义）
# ──────────────────────────────────────────────
class FunctionDefinition(Base):
    """A governed typed function contract with a closed-list built-in runtime.

    The runtime configuration is data-only and validated by
    ``function_definition_service``.  There is still no handler, URL, command,
    script, connector, or arbitrary implementation field.
    """

    __tablename__ = "function_definitions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    output_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    # Presentation metadata only.  Authorization still comes from the owning
    # scenario and is never inferred from this field.
    visibility: Mapped[str] = mapped_column(String(20), default="scenario")
    # contract / weighted_score / threshold / geo_distance /
    # timeseries_aggregate
    runtime_kind: Mapped[str] = mapped_column(String(40), default="contract")
    runtime_config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    scenario: Mapped[BusinessScenario] = relationship(back_populates="function_definitions")


class OntologyAdvancedAsset(Base):
    """Governed P2 data/model asset attached to an ontology scenario.

    The platform stores a portable descriptor and bounded records here.  It
    intentionally does not contain executable code or arbitrary connection
    credentials; external access still goes through the connector binding and
    release gates already used by the rest of the platform.
    """

    __tablename__ = "ontology_advanced_assets"
    __table_args__ = (
        UniqueConstraint("scenario_id", "name", name="uq_advanced_asset_scenario_name"),
        Index("ix_advanced_assets_tenant_scenario_kind", "tenant_id", "scenario_id", "kind"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # geospatial / timeseries / media / realtime / ml_model / simulation /
    # optimization.  Runtime kind is a closed server-side allowlist.
    kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    schema: Mapped[dict] = mapped_column(JSON, default=dict)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    # draft / ready / disabled
    status: Mapped[str] = mapped_column(String(20), default="draft")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    tenant: Mapped[Tenant] = relationship()
    scenario: Mapped[BusinessScenario] = relationship(back_populates="advanced_assets")
    records: Mapped[list["OntologyAdvancedRecord"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan", order_by="OntologyAdvancedRecord.sequence"
    )
    runs: Mapped[list["OntologyAdvancedRun"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan", order_by="OntologyAdvancedRun.created_at.desc()"
    )


class OntologyAdvancedRecord(Base):
    """A normalized record for spatial, temporal, media and realtime assets."""

    __tablename__ = "ontology_advanced_records"
    __table_args__ = (
        Index("ix_advanced_records_asset_event_time", "asset_id", "event_time"),
        Index("ix_advanced_records_asset_sequence", "asset_id", "sequence"),
        Index("ix_advanced_records_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_advanced_assets.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, default=0, index=True)
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(120), default="")
    geometry: Mapped[dict] = mapped_column(JSON, default=dict)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    source_ref: Mapped[str] = mapped_column(String(300), default="")
    content_type: Mapped[str] = mapped_column(String(160), default="")
    storage_path: Mapped[str] = mapped_column(String(700), default="")
    checksum: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    tenant: Mapped[Tenant] = relationship()
    asset: Mapped[OntologyAdvancedAsset] = relationship(back_populates="records")


class OntologyAdvancedRun(Base):
    """An auditable execution of a built-in model/simulation/optimization."""

    __tablename__ = "ontology_advanced_runs"
    __table_args__ = (
        Index("ix_advanced_runs_asset_created", "asset_id", "created_at"),
        Index("ix_advanced_runs_function_created", "function_id", "created_at"),
        UniqueConstraint(
            "tenant_id", "idempotency_scope", "idempotency_key",
            name="uq_advanced_runs_idempotency",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_advanced_assets.id", ondelete="CASCADE"), nullable=True, index=True
    )
    function_id: Mapped[str | None] = mapped_column(
        ForeignKey("function_definitions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    run_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="succeeded")
    input_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    output_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    idempotency_scope: Mapped[str] = mapped_column(String(80), default="")
    idempotency_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    tenant: Mapped[Tenant] = relationship()
    asset: Mapped[OntologyAdvancedAsset | None] = relationship(back_populates="runs")
    function: Mapped[FunctionDefinition | None] = relationship()


class OntologyModelFeedback(Base):
    """Human or external evaluation attached to a governed model run."""

    __tablename__ = "ontology_model_feedback"
    __table_args__ = (
        Index("ix_model_feedback_asset_created", "asset_id", "created_at"),
        Index("ix_model_feedback_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_advanced_assets.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_advanced_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    label: Mapped[str] = mapped_column(String(160), default="")
    expected_output: Mapped[dict] = mapped_column(JSON, default=dict)
    actual_output: Mapped[dict] = mapped_column(JSON, default=dict)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    tenant: Mapped[Tenant] = relationship()
    asset: Mapped[OntologyAdvancedAsset] = relationship()


class OntologyAction(Base):
    """实体操作（Action）：定义某个实体类型可执行业务行为。

    类 Palantir Ontology 的 Action 概念：
    - 输入参数（input_schema）
    - 执行方式（executor_type: sql / skill / mcp / http / script）
    - 执行配置（executor_config）
    - 前置条件 / 后置效果（描述性，供 LLM 理解）
    """

    __tablename__ = "ontology_actions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    entity_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_entities.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    # 输入参数 JSON Schema（OpenAI function calling 格式）
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    # 执行方式: sql / skill / mcp / http / script
    executor_type: Mapped[str] = mapped_column(String(30), default="sql")
    # 执行配置（按 executor_type 不同结构不同）
    executor_config: Mapped[dict] = mapped_column(JSON, default=dict)
    # 前置条件（描述性文本，供 LLM 判断）
    precondition: Mapped[str] = mapped_column(Text, default="")
    # 后置效果（描述性文本）
    postcondition: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 执行安全策略：当前权限边界以场景所有权为准，确认和幂等由服务端强制执行。
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, default=True)
    idempotency_required: Mapped[bool] = mapped_column(Boolean, default=True)
    permission_scope: Mapped[str] = mapped_column(String(30), default="scenario")
    # Action 级 ACL 的开关。受限操作只能由 owner/admin 或显式 grant 执行。
    access_scope: Mapped[str] = mapped_column(String(20), default="tenant")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    scenario: Mapped[BusinessScenario] = relationship()
    entity: Mapped[OntologyEntity] = relationship()


class OntologyRule(Base):
    """业务规则（Rule）：定义复杂条件表达式，用于判定/分类/触发。

    规则条件用 JSON 表达式描述（支持 AND/OR/NOT 嵌套 + 比较运算符），
    可绑定到实体，执行时由规则引擎解析。
    """

    __tablename__ = "ontology_rules"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    entity_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_entities.id", ondelete="SET NULL"), index=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    # 规则条件表达式（JSON）：
    # {"op":"and","conditions":[{"field":"数量","op":">","value":2}, ...]}
    condition: Mapped[dict] = mapped_column(JSON, default=dict)
    # 规则命中后的动作（描述性 + 可选触发的 action_id 列表）
    action_on_match: Mapped[str] = mapped_column(Text, default="")
    trigger_action_ids: Mapped[list] = mapped_column(JSON, default=list)
    # 严重级别: info / warning / critical
    severity: Mapped[str] = mapped_column(String(20), default="info")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    scenario: Mapped[BusinessScenario] = relationship()
    entity: Mapped[OntologyEntity | None] = relationship()


class OntologyEvent(Base):
    """业务事件（Event）：定义可被触发/订阅的事件类型。

    事件是异步协同的基础：操作执行后可发布事件，
    工作流/规则可订阅事件并触发后续动作。
    """

    __tablename__ = "ontology_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    # 事件携带数据的 JSON Schema
    payload_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    # 触发来源描述（哪个操作/规则会发布此事件）
    trigger_source: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    scenario: Mapped[BusinessScenario] = relationship()


class OntologyWorkflow(Base):
    """工作流（Workflow）：多步骤流程编排。

    步骤（steps）为有序列表，每步可引用 action / rule / event，
    支持条件分支与顺序执行。
    """

    __tablename__ = "ontology_workflows"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    # 触发方式: manual / scheduled / event
    trigger_type: Mapped[str] = mapped_column(String(30), default="manual")
    # 触发配置（定时表达式 / 事件名等）
    trigger_config: Mapped[dict] = mapped_column(JSON, default=dict)
    # 步骤列表（有序，旧版线性格式，保留兼容）：
    # [{"step":1,"type":"action","action_id":"...","params":{...}},
    #  {"step":2,"type":"rule","rule_id":"..."},
    #  {"step":3,"type":"event","event_id":"..."}]
    steps: Mapped[list] = mapped_column(JSON, default=list)
    # 可视化编排（DAG，VueFlow 格式）：
    # nodes: [{"id":"n1","type":"action","name":"...","position":{"x":0,"y":0},"data":{...}}]
    # edges: [{"id":"e1","source":"start","target":"n1","label":"true|false|""}]
    nodes: Mapped[list] = mapped_column(JSON, default=list)
    edges: Mapped[list] = mapped_column(JSON, default=list)
    # 生命周期：draft 草稿 / active 启用 / disabled 停用。
    status: Mapped[str] = mapped_column(String(20), default="draft")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 工作流可独立于场景角色矩阵收窄执行权。
    access_scope: Mapped[str] = mapped_column(String(20), default="tenant")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    scenario: Mapped[BusinessScenario] = relationship()
    runs: Mapped[list["WorkflowRun"]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan"
    )


# ──────────────────────────────────────────────
# P2 本体发布治理：分支 / 快照 / 提案 / 评审 / 发布 / 回滚
# ──────────────────────────────────────────────
class OntologyBranch(Base):
    """业务场景的受治理本体分支。

    分支并不直接保存可变草稿；所有候选状态都落为不可变 ``OntologySnapshot``，从而
    保证提案、合并和回滚可审计且可复现。
    """

    __tablename__ = "ontology_branches"
    __table_args__ = (
        UniqueConstraint("scenario_id", "name", name="uq_ontology_branch_name"),
        Index("ix_ontology_branches_tenant_scenario", "tenant_id", "scenario_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    # active / merged / archived
    status: Mapped[str] = mapped_column(String(20), default="active")
    base_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_snapshots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    head_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_snapshots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    scenario: Mapped[BusinessScenario] = relationship(back_populates="ontology_branches")
    snapshots: Mapped[list["OntologySnapshot"]] = relationship(
        back_populates="branch",
        foreign_keys="OntologySnapshot.branch_id",
    )
    proposals: Mapped[list["OntologyProposal"]] = relationship(
        back_populates="branch", cascade="all, delete-orphan"
    )
    releases: Mapped[list["OntologyRelease"]] = relationship(back_populates="branch")
    rollbacks: Mapped[list["OntologyRollback"]] = relationship(back_populates="branch")


class OntologySnapshot(Base):
    """不可变的本体定义快照；不包含运行时对象实例或执行日志。"""

    __tablename__ = "ontology_snapshots"
    __table_args__ = (
        Index("ix_ontology_snapshots_scenario_created", "scenario_id", "created_at"),
        Index("ix_ontology_snapshots_branch_created", "branch_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    branch_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_branches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    parent_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_snapshots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # baseline / proposal / pre_merge / merge / pre_rollback / rollback
    kind: Mapped[str] = mapped_column(String(30), default="baseline")
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    scenario: Mapped[BusinessScenario] = relationship(back_populates="ontology_snapshots")
    branch: Mapped[OntologyBranch | None] = relationship(
        back_populates="snapshots", foreign_keys=[branch_id]
    )


class OntologyProposal(Base):
    """由分支快照构成的待评审本体变更提案。"""

    __tablename__ = "ontology_proposals"
    __table_args__ = (
        Index("ix_ontology_proposals_scenario_status", "scenario_id", "status"),
        Index("ix_ontology_proposals_branch_created", "branch_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    branch_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_branches.id", ondelete="CASCADE"), index=True
    )
    base_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_snapshots.id", ondelete="RESTRICT"), index=True
    )
    proposed_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_snapshots.id", ondelete="RESTRICT"), index=True
    )
    pre_merge_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_snapshots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    merged_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_snapshots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    # draft / submitted / approved / rejected / merged / withdrawn
    status: Mapped[str] = mapped_column(String(20), default="submitted")
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    merged_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    scenario: Mapped[BusinessScenario] = relationship(back_populates="ontology_proposals")
    branch: Mapped[OntologyBranch] = relationship(back_populates="proposals")
    reviews: Mapped[list["OntologyReview"]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan", order_by="OntologyReview.created_at"
    )


class OntologyReview(Base):
    """一次不可变评审决定；提案状态由最新明确决定驱动。"""

    __tablename__ = "ontology_reviews"
    __table_args__ = (
        Index("ix_ontology_reviews_proposal_created", "proposal_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    proposal_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_proposals.id", ondelete="CASCADE"), index=True
    )
    reviewer_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # approve / reject
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    proposal: Mapped[OntologyProposal] = relationship(back_populates="reviews")


class OntologyRelease(Base):
    """向 dev/staging/prod 环境推广一个已治理快照的不可变发布记录。"""

    __tablename__ = "ontology_releases"
    __table_args__ = (
        Index("ix_ontology_releases_scenario_environment", "scenario_id", "environment", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    branch_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_branches.id", ondelete="RESTRICT"), index=True
    )
    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_snapshots.id", ondelete="RESTRICT"), index=True
    )
    proposal_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_proposals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # dev / staging / prod
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    # released / superseded / rolled_back
    status: Mapped[str] = mapped_column(String(20), default="released")
    notes: Mapped[str] = mapped_column(Text, default="")
    # Immutable, credential-free evidence of the bindings that passed the
    # environment gate at release time.  Runtime connector config never enters
    # this JSON document.
    connector_audit: Mapped[list] = mapped_column(JSON, default=list)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    scenario: Mapped[BusinessScenario] = relationship(back_populates="ontology_releases")
    branch: Mapped[OntologyBranch] = relationship(back_populates="releases")


class OntologyRollback(Base):
    """显式确认后的回滚审计：从当前状态恢复到一份已存在的治理快照。"""

    __tablename__ = "ontology_rollbacks"
    __table_args__ = (
        Index("ix_ontology_rollbacks_scenario_created", "scenario_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    branch_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_branches.id", ondelete="RESTRICT"), index=True
    )
    from_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_snapshots.id", ondelete="RESTRICT"), index=True
    )
    target_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_snapshots.id", ondelete="RESTRICT"), index=True
    )
    result_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_snapshots.id", ondelete="RESTRICT"), index=True
    )
    environment: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    connector_audit: Mapped[list] = mapped_column(JSON, default=list)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    scenario: Mapped[BusinessScenario] = relationship(back_populates="ontology_rollbacks")
    branch: Mapped[OntologyBranch] = relationship(back_populates="rollbacks")


class ConnectorBinding(Base):
    """A scenario/environment-specific reference to a reusable connector.

    Connector credentials remain in ``DataSource``, ``MCPConfig`` or
    ``LLMConfig``.  This table only stores the safe, auditable association that
    lets an imported package resolve a portable external reference in a target
    environment.  The connector id is intentionally polymorphic: the kind
    controls which source table is resolved by the service layer.
    """

    __tablename__ = "connector_bindings"
    __table_args__ = (
        UniqueConstraint(
            "scenario_id", "environment", "binding_key",
            name="uq_connector_bindings_scenario_environment_key",
        ),
        Index("ix_connector_bindings_connector", "connector_kind", "connector_id"),
        Index("ix_connector_bindings_scenario_environment", "scenario_id", "environment"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    # dev / staging / prod; validated centrally in connector_service.
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    # Stable portable-reference key.  It never contains a credential or source id.
    binding_key: Mapped[str] = mapped_column(String(180), nullable=False)
    reference_label: Mapped[str] = mapped_column(String(300), default="")
    # data_source / mcp / llm
    connector_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    connector_id: Mapped[str] = mapped_column(String(32), nullable=False)
    # unknown / healthy / unhealthy.  A binding becomes stale when its connector
    # is edited, disabled or removed, and must be explicitly checked again.
    health_status: Mapped[str] = mapped_column(String(20), default="unknown")
    health_message: Mapped[str] = mapped_column(Text, default="")
    connector_signature: Mapped[str] = mapped_column(String(64), default="")
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    scenario: Mapped[BusinessScenario] = relationship()
    tenant: Mapped[Tenant] = relationship()


class WorkflowRun(Base):
    """持久化的工作流运行实例，也是 P1 异步任务队列的业务记录。

    队列状态和业务运行状态合并在同一记录中，重启后仍可恢复等待审批、
    重试或尚未调度的任务。执行器的细粒度审计仍由 ActionExecutionLog 保存。
    """

    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index("ix_workflow_runs_dispatch", "status", "available_at"),
        Index("ix_workflow_runs_scenario_created", "scenario_id", "created_at"),
        Index("ix_workflow_runs_release", "release_id", "definition_snapshot_id"),
        Index("uq_workflow_runs_dedupe", "workflow_id", "dedupe_key", unique=True),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_workflows.id", ondelete="CASCADE"), index=True
    )
    # manual / scheduled / event / approval / retry
    trigger_source: Mapped[str] = mapped_column(String(30), default="manual")
    event_envelope_id: Mapped[str | None] = mapped_column(
        ForeignKey("event_envelopes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 事件和调度投递以此键去重；手动运行不填写，允许重复提交。
    dedupe_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    input_params: Mapped[dict] = mapped_column(JSON, default=dict)
    # Fixed when queued so approval/retry cannot silently switch environments.
    environment: Mapped[str] = mapped_column(String(20), default=_runtime_environment_default)
    # A staging/prod run pins the immutable released definition.  The live
    # workflow FK above remains for lineage compatibility and referential safety;
    # execution resolves these fields rather than reading mutable live columns.
    definition_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_snapshots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    release_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_releases.id", ondelete="SET NULL"), nullable=True, index=True
    )
    definition_hash: Mapped[str] = mapped_column(String(64), default="")
    # live / release.  The source makes historical execution provenance
    # inspectable without exposing the snapshot payload in list APIs.
    definition_source: Mapped[str] = mapped_column(String(20), default="live")
    # Stable execution lineage.  Automatic retries deliberately retain this value so
    # completed side-effecting nodes replay their durable idempotency record instead
    # of being invoked again.  A user-requested retry starts a fresh lineage.
    execution_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    # queued / running / awaiting_approval / retry_waiting / succeeded / failed /
    # timed_out / rejected / cancelled
    status: Mapped[str] = mapped_column(String(30), default="queued")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=300)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 已批准的审批节点 ID。恢复时前序 Action 通过运行 ID 保持幂等。
    approved_node_ids: Mapped[list] = mapped_column(JSON, default=list)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    scenario: Mapped[BusinessScenario] = relationship(back_populates="workflow_runs")
    workflow: Mapped[OntologyWorkflow] = relationship(back_populates="runs")
    approvals: Mapped[list["WorkflowApprovalRequest"]] = relationship(
        back_populates="workflow_run", cascade="all, delete-orphan"
    )


class WorkflowApprovalRequest(Base):
    """工作流审批节点生成的可恢复人工决策请求。"""

    __tablename__ = "workflow_approval_requests"
    __table_args__ = (
        Index("ix_workflow_approvals_pending", "status", "requested_at"),
        Index("uq_workflow_approvals_node", "workflow_run_id", "node_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    workflow_run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[str] = mapped_column(String(120), nullable=False)
    node_name: Mapped[str] = mapped_column(String(200), default="")
    instructions: Mapped[str] = mapped_column(Text, default="")
    # pending / approved / rejected / expired
    status: Mapped[str] = mapped_column(String(20), default="pending")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    comment: Mapped[str] = mapped_column(Text, default="")

    workflow_run: Mapped[WorkflowRun] = relationship(back_populates="approvals")


class EventEnvelope(Base):
    """事件总线的持久化信封；订阅工作流据此生成去重的运行任务。"""

    __tablename__ = "event_envelopes"
    __table_args__ = (
        Index("ix_event_envelopes_scenario_created", "scenario_id", "created_at"),
        Index("uq_event_envelopes_dedupe", "event_id", "dedupe_key", unique=True),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_events.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(60), default="manual")
    source_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Event delivery and its subscribers are resolved from one deployment
    # definition; otherwise an event emitted by release A could enqueue a live
    # workflow from release B after a dev merge.
    environment: Mapped[str] = mapped_column(String(20), default=_runtime_environment_default)
    definition_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_snapshots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    release_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_releases.id", ondelete="SET NULL"), nullable=True, index=True
    )
    definition_hash: Mapped[str] = mapped_column(String(64), default="")
    definition_source: Mapped[str] = mapped_column(String(20), default="live")
    dedupe_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    scenario: Mapped[BusinessScenario] = relationship(back_populates="event_envelopes")
    event: Mapped[OntologyEvent] = relationship()


class ActionExecutionLog(Base):
    """操作执行日志：记录每次 Action/Workflow 的执行轨迹（可追溯）。"""

    __tablename__ = "action_execution_logs"
    __table_args__ = (
        Index(
            "uq_action_execution_logs_idempotency",
            "scenario_id",
            "target_type",
            "target_id",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "uq_action_execution_logs_parent_preview",
            "parent_action_log_id",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    # 执行对象类型: action / workflow / rule
    target_type: Mapped[str] = mapped_column(String(30), default="action")
    target_id: Mapped[str] = mapped_column(String(32), index=True)
    target_name: Mapped[str] = mapped_column(String(200), default="")
    # 输入参数
    input_params: Mapped[dict] = mapped_column(JSON, default=dict)
    # 执行状态: running / success / failed / confirmation_required / dry_run
    status: Mapped[str] = mapped_column(String(20), default="running")
    # execute / dry_run / confirmation
    mode: Mapped[str] = mapped_column(String(20), default="execute")
    # 同一个业务请求的幂等键；预演和确认提醒不要求填写。
    idempotency_key: Mapped[str | None] = mapped_column(String(120), index=True, nullable=True)
    # Provenance for an execution that came from a frozen staging/prod release.
    # ``environment`` also scopes idempotency so equal caller keys cannot replay
    # a result produced in another deployment environment.
    environment: Mapped[str] = mapped_column(String(20), default=_runtime_environment_default)
    definition_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_snapshots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    release_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_releases.id", ondelete="SET NULL"), nullable=True, index=True
    )
    definition_hash: Mapped[str] = mapped_column(String(64), default="")
    definition_source: Mapped[str] = mapped_column(String(20), default="live")
    # 执行结果
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    # Credential-free runtime connector evidence (environment/key/target only).
    connector_audit: Mapped[list] = mapped_column(JSON, default=list)
    # Complete decision-chain provenance.  These columns deliberately remain
    # nullable for legacy/background records that do not carry a verifiable
    # identity; migration code never guesses an actor, Agent or model.
    actor_type: Mapped[str] = mapped_column(String(20), default="unknown")
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    llm_config_id: Mapped[str | None] = mapped_column(
        ForeignKey("llm_configs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    model_name: Mapped[str] = mapped_column(String(240), default="")
    permission_decision: Mapped[dict] = mapped_column(JSON, default=dict)
    # Safe identifiers/hashes describing the data plane used by the run.  Raw
    # rows, prompts, credentials and connection configuration never enter it.
    data_context: Mapped[dict] = mapped_column(JSON, default=dict)
    # Correlates an AI-requested dry-run with the later user-confirmed effect.
    # Agent and global-assistant messages live in separate tables, hence the
    # two explicit nullable references instead of one ambiguous string.
    correlation_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    parent_action_log_id: Mapped[str | None] = mapped_column(
        ForeignKey("action_execution_logs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    assistant_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("assistant_messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 错误信息
    error: Mapped[str] = mapped_column(Text, default="")
    # 执行耗时（毫秒）
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    scenario: Mapped[BusinessScenario] = relationship()


# ──────────────────────────────────────────────
# P1 运营闭环：事件 / Case（异常事项）及其不可变历史
# ──────────────────────────────────────────────
class IncidentCase(Base):
    """场景范围内可分派、确认和闭环的运营异常事项。

    事件、规则或人工发现的问题最终都应落到同一种 durable Case 记录；状态变化
    只通过服务层写入 ``IncidentCaseHistory``，避免任务中心只展示一次性执行结果。
    """

    __tablename__ = "incident_cases"
    __table_args__ = (
        Index("ix_incident_cases_tenant_scenario_status", "tenant_id", "scenario_id", "status"),
        Index("ix_incident_cases_scenario_updated", "scenario_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    # low / medium / high / critical
    severity: Mapped[str] = mapped_column(String(20), default="medium", index=True)
    # open / acknowledged / resolved
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    # manual / rule / workflow / agent / import 等可审计来源，不承载凭据。
    source: Mapped[str] = mapped_column(String(60), default="manual")
    source_ref: Mapped[str] = mapped_column(String(180), default="")
    # 不设对象外键，确保对象生命周期不会抹掉已闭环 Case 的来源证据。
    related_object_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    assignee_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    acknowledged_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    scenario: Mapped[BusinessScenario] = relationship(back_populates="incident_cases")
    histories: Mapped[list["IncidentCaseHistory"]] = relationship(
        back_populates="incident_case",
        cascade="all, delete-orphan",
        order_by="IncidentCaseHistory.created_at",
    )


class IncidentCaseHistory(Base):
    """Case 状态/内容变化的追加式审计记录。"""

    __tablename__ = "incident_case_history"
    __table_args__ = (
        Index("ix_incident_case_history_case_created", "incident_case_id", "created_at"),
        Index("ix_incident_case_history_tenant_scenario", "tenant_id", "scenario_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    incident_case_id: Mapped[str] = mapped_column(
        ForeignKey("incident_cases.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    # created / updated / acknowledged / resolved
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    from_status: Mapped[str] = mapped_column(String(20), default="")
    to_status: Mapped[str] = mapped_column(String(20), default="")
    changes: Mapped[dict] = mapped_column(JSON, default=dict)
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    incident_case: Mapped[IncidentCase] = relationship(back_populates="histories")
