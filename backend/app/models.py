"""ORM models for the ontology business agent platform."""
from __future__ import annotations

import uuid
import unicodedata
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    event,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    inspect,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import get_settings
from .database import Base, orm_datetime as DateTime


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_document_type():
    """Use PostgreSQL JSONB for document-shaped values."""
    return JSON().with_variant(postgresql.JSONB(none_as_null=True), "postgresql")


def _sha256_check(column_name: str) -> str:
    """Return the PostgreSQL check for an exact lowercase SHA-256 value."""
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column_name}) = 64 AND {column_name} = lower({column_name}) "
        f"AND {remainder} = ''"
    )


def _assistant_attachment_expiry() -> datetime:
    from datetime import timedelta

    return _now() + timedelta(hours=24)


def _runtime_environment_default() -> str:
    """Keep direct ORM-created runs aligned with this server deployment."""
    return get_settings().runtime_environment


def normalize_mcp_name_key(value: str) -> str:
    """Return the database identity used for tenant-local MCP names."""
    return unicodedata.normalize("NFKC", str(value or "").strip()).casefold()


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
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_scenarios_id_tenant"),
    )

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
        back_populates="scenario", passive_deletes=True
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
    relation_data_mappings: Mapped[list["RelationDataMapping"]] = relationship(
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
    scenario_model_draft_resources: Mapped[list["ScenarioModelDraftResource"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    tenant: Mapped[Tenant | None] = relationship()


class OntologyEntity(Base):
    """本体中的实体类型（Object Type），例如对象、事项、资源等领域概念。"""

    __tablename__ = "ontology_entities"
    __table_args__ = (
        UniqueConstraint("id", "scenario_id", name="uq_entities_id_scenario"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Immutable, machine-facing identity.  ``name`` remains editable display
    # metadata; compiled definitions and integrations must not key off it.
    api_name: Mapped[str] = mapped_column(
        String(100), default=lambda: f"entity_{_uuid()[:12]}", index=True
    )
    # Object types are retired non-destructively.  Deprecated definitions and
    # their historical facts remain in storage, but authoring/read runtimes
    # exclude them from the active ontology surface.
    lifecycle_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", index=True
    )
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
    __table_args__ = (
        UniqueConstraint("id", "entity_id", name="uq_properties_id_entity"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    entity_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_entities.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    api_name: Mapped[str] = mapped_column(
        String(100), default=lambda: f"property_{_uuid()[:12]}", index=True
    )
    data_type: Mapped[str] = mapped_column(String(50), default="string")
    description: Mapped[str] = mapped_column(Text, default="")
    is_key: Mapped[bool] = mapped_column(Boolean, default=False)
    # Human-readable object label (Palantir-style title key).  Identity and
    # display are deliberately separate: a stable code can be the primary key
    # while a business name is the title shown in graphs and Agent answers.
    is_title: Mapped[bool] = mapped_column(Boolean, default=False)
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
    __table_args__ = (
        UniqueConstraint(
            "id",
            "scenario_id",
            "source_entity_id",
            "target_entity_id",
            name="uq_relations_id_scope",
        ),
        UniqueConstraint("id", "scenario_id", name="uq_relations_id_scenario"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    api_name: Mapped[str] = mapped_column(
        String(100), default=lambda: f"relation_{_uuid()[:12]}", index=True
    )
    namespace: Mapped[str] = mapped_column(String(180), default="default")
    source_entity_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_entities.id", ondelete="CASCADE"), index=True
    )
    target_entity_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_entities.id", ondelete="CASCADE"), index=True
    )
    # A link is navigable from both endpoints.  Display labels can evolve,
    # while the two API names remain stable integration contracts.
    source_display_name: Mapped[str] = mapped_column(String(200), default="")
    source_api_name: Mapped[str] = mapped_column(String(100), default="")
    target_display_name: Mapped[str] = mapped_column(String(200), default="")
    target_api_name: Mapped[str] = mapped_column(String(100), default="")
    # ``none`` is explicit for legacy/manual links with no declared physical
    # backing. Other values describe the canonical link storage strategy.
    storage_kind: Mapped[str] = mapped_column(String(32), default="none")
    relation_type: Mapped[str] = mapped_column(String(10), default="1:N")  # 1:1 / 1:N / N:1 / N:M
    # Closed, server-normalised relation axioms/cardinality constraints.  The
    # UI edits these through named fields; arbitrary JSON is never executed.
    constraints: Mapped[dict] = mapped_column(JSON, default=dict)
    description: Mapped[str] = mapped_column(Text, default="")

    scenario: Mapped[BusinessScenario] = relationship(back_populates="relations")
    source_entity: Mapped[OntologyEntity] = relationship(foreign_keys=[source_entity_id])
    target_entity: Mapped[OntologyEntity] = relationship(foreign_keys=[target_entity_id])
    relation_instances: Mapped[list["RelationInstance"]] = relationship(
        back_populates="relation", cascade="all, delete-orphan"
    )
    data_mapping: Mapped["RelationDataMapping | None"] = relationship(
        back_populates="relation", cascade="all, delete-orphan", uselist=False
    )


class OntologyInstance(Base):
    """实体实例（Object）：本体中某个实体类型的一条真实业务记录。"""

    __tablename__ = "ontology_instances"
    __table_args__ = (
        UniqueConstraint("id", "scenario_id", name="uq_instances_id_scenario"),
    )

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
    __table_args__ = (
        UniqueConstraint(
            "relation_id",
            "source_instance_id",
            "target_instance_id",
            name="uq_relation_instances_edge",
        ),
    )

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
    source: Mapped[str] = mapped_column(String(20), default="manual")
    source_ref: Mapped[str] = mapped_column(String(500), default="")
    source_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
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
    # New catalog-backed mappings point at a stable logical relation.  The
    # legacy table_name remains a compatibility label, not storage ownership.
    dataset_relation_id: Mapped[str | None] = mapped_column(
        ForeignKey("dataset_relations.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
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
    dataset_relation: Mapped["DatasetRelation | None"] = relationship()
    source_relation_mappings: Mapped[list["RelationDataMapping"]] = relationship(
        foreign_keys="RelationDataMapping.source_mapping_id",
        back_populates="source_mapping",
        passive_deletes=True,
    )
    target_relation_mappings: Mapped[list["RelationDataMapping"]] = relationship(
        foreign_keys="RelationDataMapping.target_mapping_id",
        back_populates="target_mapping",
        passive_deletes=True,
    )


# ──────────────────────────────────────────────
# 数据源
# ──────────────────────────────────────────────
class DataSource(Base):
    """数据源：PostgreSQL、MinIO 文件桶或 MinIO 版本化数据集。"""

    __tablename__ = "data_sources"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_data_sources_id_tenant"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=True
    )
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    scenario_id: Mapped[str | None] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="SET NULL"), index=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[str] = mapped_column(String(30), nullable=False)  # postgres / file_bucket / dataset
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
    __table_args__ = (
        UniqueConstraint(
            "id", "data_source_id", name="uq_bucket_files_id_source"
        ),
        Index(
            "uq_bucket_files_generated_action_log",
            "generated_by_action_log_id",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    data_source_id: Mapped[str] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(4096), nullable=False)
    # ``stored_path`` mirrors the stable MinIO URI for API compatibility.
    storage_provider: Mapped[str] = mapped_column(
        String(20), default="minio", nullable=False
    )
    bucket_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    object_key: Mapped[str] = mapped_column(String(2048), default="", nullable=False)
    object_version_id: Mapped[str] = mapped_column(
        String(255), default="", nullable=False
    )
    etag: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    # This is a durable object identity, never an expiring presigned URL.
    object_url: Mapped[str] = mapped_column(String(4096), default="", nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    mime: Mapped[str] = mapped_column(String(200), default="")
    # Raw-file integrity and generation lineage are durable BucketFile facts,
    # not merely transient fields in an Action response.  Legacy uploads keep
    # empty lineage values and are validated compatibly.
    content_sha256: Mapped[str] = mapped_column(String(64), default="")
    origin_template_file_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    origin_template_sha256: Mapped[str] = mapped_column(String(64), default="")
    # Catalog lineage is stored in addition to the legacy source-file lineage.
    # These identifiers intentionally do not cascade: generated deliverables
    # remain auditable even after an unreferenced catalog entry is removed.
    origin_template_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    origin_template_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    generated_by_action_log_id: Mapped[str | None] = mapped_column(
        ForeignKey("action_execution_logs.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending / parsed / error
    error: Mapped[str] = mapped_column(Text, default="")
    parsed_text: Mapped[str] = mapped_column(
        Text, default=""
    )
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
        back_populates="bucket_file",
        cascade="all, delete-orphan",
        foreign_keys="DocumentChunk.bucket_file_id",
    )


class ObjectDeletionJob(Base):
    """Transactional outbox for durable object deletion.

    The job deliberately has no foreign key to its origin row: that row and
    any owning parent may be deleted in the same transaction that creates the
    job.  Completed rows are retained as a compact deletion audit trail.
    """

    __tablename__ = "object_deletion_jobs"
    __table_args__ = (
        Index(
            "ix_object_deletion_jobs_ready",
            "status",
            "next_attempt_at",
            "created_at",
        ),
        Index(
            "ix_object_deletion_jobs_origin",
            "origin_type",
            "origin_id",
        ),
    )

    # A SHA-256 of provider + immutable object identity is both the primary
    # key and the deduplication key without requiring an oversized index.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    bucket_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    object_key: Mapped[str] = mapped_column(String(2048), default="", nullable=False)
    object_version_id: Mapped[str] = mapped_column(
        String(255), default="", nullable=False
    )
    object_url: Mapped[str] = mapped_column(String(4096), default="", nullable=False)
    origin_type: Mapped[str] = mapped_column(String(40), nullable=False)
    origin_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    scenario_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_token: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    lease_generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ArtifactTemplate(Base):
    """Tenant-owned, optionally scenario-scoped business artifact template.

    The logical template has a stable identity while every binary revision is
    immutable and represented by :class:`ArtifactTemplateVersion`.  Actions
    pin an explicit version and digest instead of following ``current_version``
    at execution time.
    """

    __tablename__ = "artifact_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_artifact_templates_tenant_key"),
        Index("ix_artifact_templates_tenant_scenario_status", "tenant_id", "scenario_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    scenario_id: Mapped[str | None] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="SET NULL"), index=True, nullable=True
    )
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    purpose: Mapped[str] = mapped_column(String(500), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    current_version_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "artifact_template_versions.id",
            name="fk_artifact_templates_current_version",
            ondelete="SET NULL",
            use_alter=True,
        ),
        nullable=True,
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    versions: Mapped[list["ArtifactTemplateVersion"]] = relationship(
        back_populates="template",
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys="ArtifactTemplateVersion.template_id",
        order_by="ArtifactTemplateVersion.version",
    )
    current_version: Mapped["ArtifactTemplateVersion | None"] = relationship(
        foreign_keys=[current_version_id], post_update=True
    )


class ArtifactTemplateVersion(Base):
    """Immutable, inspected binary revision of an artifact template."""

    __tablename__ = "artifact_template_versions"
    __table_args__ = (
        UniqueConstraint("template_id", "version", name="uq_artifact_template_versions_number"),
        UniqueConstraint("template_id", "content_sha256", name="uq_artifact_template_versions_hash"),
        Index("ix_artifact_template_versions_bucket_file", "bucket_file_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    template_id: Mapped[str] = mapped_column(
        ForeignKey("artifact_templates.id", ondelete="CASCADE"), index=True, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    bucket_file_id: Mapped[str] = mapped_column(
        ForeignKey("bucket_files.id", ondelete="RESTRICT"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    artifact_format: Mapped[str] = mapped_column(String(20), nullable=False)
    mime: Mapped[str] = mapped_column(String(200), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    placeholder_paths: Mapped[list] = mapped_column(JSON, default=list)
    template_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    version_note: Mapped[str] = mapped_column(String(500), default="")
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    template: Mapped[ArtifactTemplate] = relationship(
        back_populates="versions", foreign_keys=[template_id]
    )
    bucket_file: Mapped[BucketFile] = relationship()


class DocumentChunk(Base):
    """已解析文档的可引用分块与向量表示。

    原始文本仍保留在 ``BucketFile``；这里保存稳定的字符偏移，令 AI 回答、
    搜索预览与审计记录能引用到同一份资料的精确片段。
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint(
            "id", "data_source_id", name="uq_document_chunks_id_source"
        ),
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
    # JSONB stores the embedding; the service layer performs cosine similarity.
    embedding: Mapped[list] = mapped_column(JSON, default=list)
    embedding_model: Mapped[str] = mapped_column(String(120), default="local-semantic-hash-192-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    bucket_file: Mapped[BucketFile] = relationship(back_populates="document_chunks")
    data_source: Mapped[DataSource] = relationship()


class RelationDataMapping(Base):
    """Explicit Link Type data binding; SQL and arbitrary JSON are not accepted."""

    __tablename__ = "relation_data_mappings"
    __table_args__ = (
        UniqueConstraint("relation_id", name="uq_relation_data_mappings_relation"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    relation_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_relations.id", ondelete="CASCADE"), index=True
    )
    source_mapping_id: Mapped[str] = mapped_column(
        ForeignKey("data_mappings.id", ondelete="CASCADE"), index=True
    )
    target_mapping_id: Mapped[str] = mapped_column(
        ForeignKey("data_mappings.id", ondelete="CASCADE"), index=True
    )
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    # For FK modes the server derives these three fields from the carrier
    # object mapping. For join_table the user selects them from inspected
    # connector metadata and the server validates them before persistence.
    data_source_id: Mapped[str] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE"), index=True
    )
    data_source_binding_key: Mapped[str] = mapped_column(String(180), default="")
    data_source_binding_ref: Mapped[dict] = mapped_column(JSON, default=dict)
    dataset_relation_id: Mapped[str | None] = mapped_column(
        ForeignKey("dataset_relations.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    table_name: Mapped[str] = mapped_column(String(300), default="")
    foreign_key_column: Mapped[str] = mapped_column(String(300), default="")
    source_key_column: Mapped[str] = mapped_column(String(300), default="")
    target_key_column: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(20), default="unknown")
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_link_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    scenario: Mapped[BusinessScenario] = relationship(back_populates="relation_data_mappings")
    relation: Mapped[OntologyRelation] = relationship(back_populates="data_mapping")
    source_mapping: Mapped[DataMapping] = relationship(
        foreign_keys=[source_mapping_id], back_populates="source_relation_mappings"
    )
    target_mapping: Mapped[DataMapping] = relationship(
        foreign_keys=[target_mapping_id], back_populates="target_relation_mappings"
    )
    data_source: Mapped[DataSource] = relationship()
    dataset_relation: Mapped["DatasetRelation | None"] = relationship()


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
    relation_mapping_fingerprint: Mapped[str] = mapped_column(String(64), default="")
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
        # 活跃任务使用同一个 logical key，终态任务清空该字段。
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
    __table_args__ = (
        UniqueConstraint("tenant_id", "name_key", name="uq_mcp_configs_tenant_name_key"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=True
    )
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # A materialized, Unicode-normalized identity lets PostgreSQL close the
    # create/import check-then-write race.
    name_key: Mapped[str] = mapped_column(String(600), nullable=False, default="")
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


def _sync_mcp_name_key(_mapper, _connection, target: MCPConfig) -> None:
    target.name_key = normalize_mcp_name_key(target.name)


event.listen(MCPConfig, "before_insert", _sync_mcp_name_key)
event.listen(MCPConfig, "before_update", _sync_mcp_name_key)


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
    data_source_ids: Mapped[list] = mapped_column(JSON, default=list)
    # Legacy NULL remains representable for old databases but is interpreted as
    # explicit-empty. Only an explicitly configured scope may grant tools.
    capability_scope: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
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
    # A dry-run tool result may be persisted before its SSE answer finishes.
    # Confirmation is allowed only after this flag becomes true, preventing a
    # later stream flush from overwriting the confirmed artifact metadata.
    stream_finalized: Mapped[bool] = mapped_column(Boolean, default=True)
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


class AssistantRouteDecision(Base):
    """Single-flight semantic decision for one explicit assistant send."""

    __tablename__ = "assistant_route_decisions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "created_by_user_id",
            "request_id",
            name="uq_assistant_route_decisions_request",
        ),
        Index(
            "ix_assistant_route_decisions_status_lease",
            "status",
            "lease_expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    created_by_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario_id: Mapped[str | None] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # The claim exists before a new thread does, so this cannot be a foreign
    # key. The endpoint creates or reloads this exact owned thread afterward.
    thread_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="planning", index=True)
    route_plan: Mapped[dict] = mapped_column(JSON, default=dict)
    lease_token: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
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
    PostgreSQL row locks provide the serialization boundary.
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


class AssistantCompilationJob(Base):
    """Durable, replayable ownership for one compound model compilation.

    The fingerprint is a hash of the authorised principal and every input that
    can change compiler output.  Its unique constraint is the cross-worker
    single-flight boundary: a duplicate request can observe/replay this row but
    cannot start another provider call chain.
    """

    __tablename__ = "assistant_compilation_jobs"
    __table_args__ = (
        UniqueConstraint(
            "request_fingerprint",
            name="uq_assistant_compilation_jobs_fingerprint",
        ),
        Index(
            "ix_assistant_compilation_jobs_scope_status",
            "tenant_id",
            "created_by_user_id",
            "scenario_id",
            "status",
        ),
        Index(
            "ix_assistant_compilation_jobs_status_lease_expiry",
            "status",
            "lease_expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    scenario_id: Mapped[str | None] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("assistant_threads.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    message_id: Mapped[str | None] = mapped_column(
        ForeignKey("assistant_messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Hashes remain the replay identity.  The exact restart input is persisted
    # separately and is never part of public job projections; service access is
    # restricted to this owner or to the current fenced execution lease.
    message_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attachment_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    llm_config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping_context_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_policy_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    compiler_version: Mapped[str] = mapped_column(String(80), nullable=False)
    scenario_baseline: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_input: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # running / succeeded / failed.  Failed rows remain terminal; a retry must
    # change a fingerprint input instead of silently spending the same budget.
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    llm_call_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    llm_calls_used: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class ScenarioModelDraftResource(Base):
    """An inert, editable resource candidate produced by model compilation.

    Invalid or incomplete generated definitions cannot safely be inserted into
    the live ontology tables: several of those tables are read directly by the
    runtime and release snapshot services, while others require foreign keys
    that an incomplete candidate cannot satisfy.  This staging table therefore
    stores the candidate as JSON and deliberately has no relationship to a
    runtime definition row.
    """

    __tablename__ = "scenario_model_draft_resources"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "scenario_id",
            "proposal_id",
            "resource_identity",
            name="uq_scenario_model_draft_resource_identity",
        ),
        Index(
            "ix_scenario_model_drafts_scenario_status",
            "tenant_id",
            "scenario_id",
            "draft_status",
            "updated_at",
        ),
        Index(
            "ix_scenario_model_drafts_lineage_started_at",
            "lineage_started_at",
        ),
        Index(
            "ix_scenario_model_drafts_predecessor",
            "tenant_id",
            "scenario_id",
            "predecessor_draft_id",
        ),
        Index(
            "ix_scenario_model_drafts_superseded_by_proposal_id",
            "superseded_by_proposal_id",
        ),
        CheckConstraint(
            "enabled = false AND publishable = false",
            name="ck_scenario_model_drafts_inert",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Source identifiers are provenance only.  They intentionally remain after
    # an assistant thread is deleted so the scene-level draft does not vanish.
    source_thread_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    source_message_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    compilation_job_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    proposal_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Compilation start time, rather than finalize time, orders concurrent
    # successor runs.  An older job that finishes late must not hide a newer
    # working lineage.
    lineage_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )
    predecessor_draft_id: Mapped[str] = mapped_column(String(32), default="")
    predecessor_revision: Mapped[int] = mapped_column(Integer, default=-1)
    superseded_by_proposal_id: Mapped[str] = mapped_column(
        String(64), default=""
    )
    task_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    resource_kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    resource_key: Mapped[str] = mapped_column(String(500), nullable=False)
    resource_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(300), default="")
    # source_payload is immutable compiler provenance; payload is the editable
    # working copy.  Replays may enrich issues but never overwrite user edits.
    source_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    validation_issues: Mapped[list] = mapped_column(JSON, default=list)
    source_refs: Mapped[list] = mapped_column(JSON, default=list)
    materialization_source: Mapped[str] = mapped_column(String(30), default="compiler")
    # ready_for_review / needs_attention / needs_validation / deferred /
    # applied / resolved / superseded
    draft_status: Mapped[str] = mapped_column(String(30), default="needs_attention", index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    publishable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resolved_resource_id: Mapped[str] = mapped_column(String(64), default="")
    revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    scenario: Mapped[BusinessScenario] = relationship(
        back_populates="scenario_model_draft_resources"
    )


class AssistantAttachment(Base):
    """助手临时附件：原始字节在 MinIO，解析文本用于限时会话上下文。"""

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
    # Hash the uploaded bytes before parsing so retries key off the actual
    # attachment content without copying the binary into the job ledger.
    content_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    # Every upload records a stable managed MinIO identity.
    storage_provider: Mapped[str] = mapped_column(
        String(20), default="minio", nullable=False
    )
    bucket_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    object_key: Mapped[str] = mapped_column(String(2048), default="", nullable=False)
    object_version_id: Mapped[str] = mapped_column(
        String(255), default="", nullable=False
    )
    etag: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    object_url: Mapped[str] = mapped_column(String(4096), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending / parsed / error
    parsed_text: Mapped[str] = mapped_column(
        Text, default=""
    )
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


class FunctionRun(Base):
    """An auditable execution of a governed built-in function runtime."""

    __tablename__ = "function_runs"
    __table_args__ = (
        Index("ix_function_runs_function_created", "function_id", "created_at"),
        UniqueConstraint(
            "tenant_id", "idempotency_scope", "idempotency_key",
            name="uq_function_runs_idempotency",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
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
    function: Mapped[FunctionDefinition | None] = relationship()


class OntologyAction(Base):
    """实体操作（Action）：定义某个实体类型可执行业务行为。

    类 Palantir Ontology 的 Action 概念：
    - 输入参数（input_schema）
    - 执行方式（executor_type: sql / skill / mcp / http / script / template）
    - 执行配置（executor_config）
    - 前置条件 / 后置效果（结构化规则门禁；自然语言遗留值不可执行）
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
    # 执行方式: sql / skill / mcp / http / script / template
    executor_type: Mapped[str] = mapped_column(String(30), default="sql")
    # 执行配置（按 executor_type 不同结构不同）
    executor_config: Mapped[dict] = mapped_column(JSON, default=dict)
    # 可执行前置条件：空值或规则 DSL 的 JSON 字符串。遗留自然语言仅可展示，
    # capability readiness 会阻止其被当作可执行门禁。
    precondition: Mapped[str] = mapped_column(Text, default="")
    # 可验证后置条件使用同一规则 DSL；执行器结果无法验证时 fail closed。
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
        ForeignKey(
            "ontology_snapshots.id",
            name="fk_ontology_branches_base_snapshot",
            ondelete="SET NULL",
            use_alter=True,
        ),
        nullable=True,
        index=True,
    )
    head_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "ontology_snapshots.id",
            name="fk_ontology_branches_head_snapshot",
            ondelete="SET NULL",
            use_alter=True,
        ),
        nullable=True,
        index=True,
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
        UniqueConstraint(
            "id", "tenant_id", "scenario_id", name="uq_snapshots_id_tenant_scenario"
        ),
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
        UniqueConstraint(
            "id",
            "tenant_id",
            "scenario_id",
            "snapshot_id",
            name="uq_releases_id_tenant_scenario_snapshot",
        ),
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
        ForeignKey(
            "event_envelopes.id",
            name="fk_workflow_runs_event_envelope",
            ondelete="SET NULL",
            use_alter=True,
        ),
        nullable=True,
        index=True,
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
        UniqueConstraint(
            "id", "scenario_id", name="uq_action_logs_id_scenario"
        ),
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
    # 执行状态: running / success / failed / confirmation_required / dry_run /
    # awaiting_approval.  Keep headroom beyond the longest canonical value so
    # PostgreSQL enforces the audit row contract at the database boundary.
    status: Mapped[str] = mapped_column(String(32), default="running")
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


# ------------------------------------------------------------
# Tenant-owned data catalog and immutable dataset versions
# ------------------------------------------------------------
class PlatformMigrationRun(Base):
    """Durable, credential-free state for one recoverable platform migration."""

    __tablename__ = "platform_migration_runs"
    __table_args__ = (
        UniqueConstraint(
            "migration_name",
            "plan_digest",
            name="uq_platform_migration_runs_plan",
        ),
        CheckConstraint(
            "status IN ('running', 'failed', 'verified', 'cutover')",
            name="ck_platform_migration_runs_status",
        ),
        CheckConstraint(
            "current_phase IN ('plan', 'bootstrap', 'archive', 'import', "
            "'verify', 'cutover')",
            name="ck_platform_migration_runs_phase",
        ),
        CheckConstraint(_sha256_check("plan_digest"), name="ck_platform_runs_plan_sha"),
        CheckConstraint(
            _sha256_check("source_fingerprint"), name="ck_platform_runs_source_sha"
        ),
        Index("ix_platform_migration_runs_status", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    migration_name: Mapped[str] = mapped_column(String(120), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    current_phase: Mapped[str] = mapped_column(String(24), nullable=False)
    manifest: Mapped[dict] = mapped_column(
        _json_document_type(), default=dict, nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)

    checkpoints: Mapped[list["PlatformMigrationCheckpoint"]] = relationship(
        back_populates="migration_run",
        order_by="PlatformMigrationCheckpoint.completed_at",
    )


class PlatformMigrationCheckpoint(Base):
    """Idempotent content checkpoint for a migration stage and logical item."""

    __tablename__ = "platform_migration_checkpoints"
    __table_args__ = (
        CheckConstraint(
            "status IN ('complete', 'verified')",
            name="ck_platform_migration_checkpoints_status",
        ),
        CheckConstraint(
            "row_count IS NULL OR row_count >= 0",
            name="ck_platform_migration_checkpoints_rows",
        ),
        CheckConstraint(
            _sha256_check("payload_sha256"), name="ck_platform_checkpoints_payload_sha"
        ),
        Index(
            "ix_platform_migration_checkpoints_status",
            "run_id",
            "status",
            "completed_at",
        ),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("platform_migration_runs.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    stage: Mapped[str] = mapped_column(String(40), primary_key=True)
    item_key: Mapped[str] = mapped_column(String(300), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payload: Mapped[dict] = mapped_column(
        _json_document_type(), default=dict, nullable=False
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    migration_run: Mapped[PlatformMigrationRun] = relationship(
        back_populates="checkpoints"
    )


class DataAsset(Base):
    """Stable, tenant-owned identity for an external or generated data asset.

    Assets deliberately have no scenario foreign key. Scenarios consume
    datasets through explicit bindings and can be retired without destroying
    source evidence.
    """

    __tablename__ = "data_assets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_data_assets_tenant_key"),
        UniqueConstraint("id", "tenant_id", name="uq_data_assets_id_tenant"),
        CheckConstraint(
            "kind IN ('file', 'stream', 'api', 'database', 'generated', 'other')",
            name="ck_data_assets_kind",
        ),
        CheckConstraint(
            "lifecycle_status IN ('active', 'retired')",
            name="ck_data_assets_lifecycle",
        ),
        Index("ix_data_assets_tenant_lifecycle", "tenant_id", "lifecycle_status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(180), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    kind: Mapped[str] = mapped_column(String(30), default="file", nullable=False)
    media_type: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(
        String(20), default="active", nullable=False
    )
    labels: Mapped[dict] = mapped_column(_json_document_type(), default=dict, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    versions: Mapped[list["DataAssetVersion"]] = relationship(
        back_populates="asset",
        order_by="DataAssetVersion.version_number",
        foreign_keys="DataAssetVersion.asset_id",
    )


class DataAssetVersion(Base):
    """Immutable content version of a catalog asset."""

    __tablename__ = "data_asset_versions"
    __table_args__ = (
        UniqueConstraint("asset_id", "version_number", name="uq_asset_versions_number"),
        UniqueConstraint("id", "tenant_id", name="uq_asset_versions_id_tenant"),
        ForeignKeyConstraint(
            ["asset_id", "tenant_id"],
            ["data_assets.id", "data_assets.tenant_id"],
            name="fk_asset_versions_asset_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["bucket_file_id", "bucket_data_source_id"],
            ["bucket_files.id", "bucket_files.data_source_id"],
            name="fk_asset_versions_file_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["bucket_data_source_id", "tenant_id"],
            ["data_sources.id", "data_sources.tenant_id"],
            name="fk_asset_versions_source_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version_number > 0", name="ck_asset_versions_number"),
        CheckConstraint("byte_size >= 0", name="ck_asset_versions_size"),
        CheckConstraint(
            "provenance_kind IN ('upload', 'connector', 'import', "
            "'reconstruction', 'generated')",
            name="ck_asset_versions_provenance",
        ),
        CheckConstraint(
            "status IN ('pending', 'ready', 'failed', 'retired')",
            name="ck_asset_versions_status",
        ),
        CheckConstraint(
            _sha256_check("content_sha256"), name="ck_asset_versions_content_sha"
        ),
        Index("ix_asset_versions_asset_status", "asset_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("data_assets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bucket_file_id: Mapped[str] = mapped_column(
        ForeignKey("bucket_files.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    bucket_data_source_id: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )
    provenance_kind: Mapped[str] = mapped_column(
        String(30), default="upload", nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    byte_size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    source_locator: Mapped[dict] = mapped_column(
        _json_document_type(), default=dict, nullable=False
    )
    version_document: Mapped[dict] = mapped_column(
        _json_document_type(), default=dict, nullable=False
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    asset: Mapped[DataAsset] = relationship(
        back_populates="versions", foreign_keys=[asset_id]
    )
    bucket_file: Mapped[BucketFile] = relationship(foreign_keys=[bucket_file_id])


class LogicalDataset(Base):
    """Tenant-owned logical data product independent of any business scene."""

    __tablename__ = "logical_datasets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_logical_datasets_tenant_key"),
        UniqueConstraint("id", "tenant_id", name="uq_logical_datasets_id_tenant"),
        CheckConstraint(
            "lifecycle_status IN ('active', 'retired')",
            name="ck_logical_datasets_lifecycle",
        ),
        Index("ix_logical_datasets_tenant_lifecycle", "tenant_id", "lifecycle_status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(180), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(
        String(20), default="active", nullable=False
    )
    labels: Mapped[dict] = mapped_column(_json_document_type(), default=dict, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    schemas: Mapped[list["DatasetSchema"]] = relationship(
        back_populates="dataset",
        order_by="DatasetSchema.schema_version",
        foreign_keys="DatasetSchema.dataset_id",
    )
    versions: Mapped[list["DatasetVersion"]] = relationship(
        back_populates="dataset",
        order_by="DatasetVersion.version_number",
        foreign_keys="DatasetVersion.dataset_id",
    )
    heads: Mapped[list["DatasetHead"]] = relationship(
        back_populates="dataset", foreign_keys="DatasetHead.dataset_id"
    )


class DatasetSchema(Base):
    """Immutable schema contract for one logical dataset."""

    __tablename__ = "dataset_schemas"
    __table_args__ = (
        UniqueConstraint("dataset_id", "schema_version", name="uq_dataset_schemas_version"),
        UniqueConstraint("dataset_id", "schema_hash", name="uq_dataset_schemas_hash"),
        UniqueConstraint("id", "dataset_id", name="uq_dataset_schemas_id_dataset"),
        UniqueConstraint(
            "id", "dataset_id", "tenant_id", name="uq_dataset_schemas_id_scope"
        ),
        ForeignKeyConstraint(
            ["dataset_id", "tenant_id"],
            ["logical_datasets.id", "logical_datasets.tenant_id"],
            name="fk_dataset_schemas_dataset_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint("schema_version > 0", name="ck_dataset_schemas_version"),
        CheckConstraint(
            _sha256_check("schema_hash"), name="ck_dataset_schemas_hash_sha"
        ),
        CheckConstraint(
            "compatibility IN ('none', 'backward', 'forward', 'full')",
            name="ck_dataset_schemas_compatibility",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dataset_id: Mapped[str] = mapped_column(
        ForeignKey("logical_datasets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    schema_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    compatibility: Mapped[str] = mapped_column(
        String(20), default="none", nullable=False
    )
    schema_document: Mapped[dict] = mapped_column(
        _json_document_type(), default=dict, nullable=False
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    dataset: Mapped[LogicalDataset] = relationship(
        back_populates="schemas", foreign_keys=[dataset_id]
    )
    relations: Mapped[list["DatasetRelation"]] = relationship(
        back_populates="schema",
        order_by="DatasetRelation.ordinal",
        foreign_keys="DatasetRelation.schema_id",
    )


class DatasetRelation(Base):
    """Named relation within a versioned dataset schema."""

    __tablename__ = "dataset_relations"
    __table_args__ = (
        UniqueConstraint("schema_id", "relation_key", name="uq_dataset_relations_key"),
        UniqueConstraint("schema_id", "ordinal", name="uq_dataset_relations_ordinal"),
        UniqueConstraint("id", "schema_id", name="uq_dataset_relations_id_schema"),
        UniqueConstraint(
            "id",
            "schema_id",
            "dataset_id",
            "tenant_id",
            name="uq_dataset_relations_id_scope",
        ),
        ForeignKeyConstraint(
            ["schema_id", "dataset_id", "tenant_id"],
            [
                "dataset_schemas.id",
                "dataset_schemas.dataset_id",
                "dataset_schemas.tenant_id",
            ],
            name="fk_dataset_relations_schema_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint("ordinal >= 0", name="ck_dataset_relations_ordinal"),
        CheckConstraint(
            "kind IN ('table', 'view', 'stream', 'document')",
            name="ck_dataset_relations_kind",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dataset_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    schema_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_schemas.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    relation_key: Mapped[str] = mapped_column(String(180), nullable=False)
    display_name: Mapped[str] = mapped_column(String(300), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default="table", nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    schema: Mapped[DatasetSchema] = relationship(
        back_populates="relations", foreign_keys=[schema_id]
    )
    fields: Mapped[list["DatasetField"]] = relationship(
        back_populates="dataset_relation",
        order_by="DatasetField.ordinal",
        foreign_keys="DatasetField.dataset_relation_id",
    )


class DatasetField(Base):
    """Stable field identity and physical/logical typing for a relation."""

    __tablename__ = "dataset_fields"
    __table_args__ = (
        UniqueConstraint(
            "dataset_relation_id", "field_key", name="uq_dataset_fields_key"
        ),
        UniqueConstraint(
            "dataset_relation_id", "ordinal", name="uq_dataset_fields_ordinal"
        ),
        UniqueConstraint(
            "id",
            "dataset_relation_id",
            "schema_id",
            "dataset_id",
            "tenant_id",
            name="uq_dataset_fields_id_scope",
        ),
        UniqueConstraint(
            "id",
            "tenant_id",
            "dataset_relation_id",
            name="uq_dataset_fields_id_tenant_relation",
        ),
        ForeignKeyConstraint(
            ["dataset_relation_id", "schema_id", "dataset_id", "tenant_id"],
            [
                "dataset_relations.id",
                "dataset_relations.schema_id",
                "dataset_relations.dataset_id",
                "dataset_relations.tenant_id",
            ],
            name="fk_dataset_fields_relation_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint("ordinal >= 0", name="ck_dataset_fields_ordinal"),
        CheckConstraint(
            "key_ordinal IS NULL OR key_ordinal >= 0",
            name="ck_dataset_fields_key_ordinal",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dataset_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    schema_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    dataset_relation_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_relations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    field_key: Mapped[str] = mapped_column(String(180), nullable=False)
    source_name: Mapped[str] = mapped_column(String(300), nullable=False)
    logical_type: Mapped[str] = mapped_column(String(80), nullable=False)
    physical_type: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    nullable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    key_ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    semantic_role: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    field_document: Mapped[dict] = mapped_column(
        _json_document_type(), default=dict, nullable=False
    )

    dataset_relation: Mapped[DatasetRelation] = relationship(
        back_populates="fields", foreign_keys=[dataset_relation_id]
    )


class DatasetVersion(Base):
    """Immutable, content-addressed materialization of a logical dataset."""

    __tablename__ = "dataset_versions"
    __table_args__ = (
        UniqueConstraint("dataset_id", "version_number", name="uq_dataset_versions_number"),
        UniqueConstraint("dataset_id", "content_hash", name="uq_dataset_versions_hash"),
        UniqueConstraint("id", "dataset_id", name="uq_dataset_versions_id_dataset"),
        UniqueConstraint("id", "schema_id", name="uq_dataset_versions_id_schema"),
        UniqueConstraint("id", "tenant_id", name="uq_dataset_versions_id_tenant"),
        UniqueConstraint(
            "id", "dataset_id", "tenant_id", name="uq_dataset_versions_id_dataset_tenant"
        ),
        UniqueConstraint(
            "id",
            "schema_id",
            "dataset_id",
            "tenant_id",
            name="uq_dataset_versions_id_scope",
        ),
        ForeignKeyConstraint(
            ["dataset_id", "tenant_id"],
            ["logical_datasets.id", "logical_datasets.tenant_id"],
            name="fk_dataset_versions_dataset_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["schema_id", "dataset_id", "tenant_id"],
            [
                "dataset_schemas.id",
                "dataset_schemas.dataset_id",
                "dataset_schemas.tenant_id",
            ],
            name="fk_dataset_versions_schema_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["schema_id", "dataset_id"],
            ["dataset_schemas.id", "dataset_schemas.dataset_id"],
            name="fk_dataset_versions_schema_dataset",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["parent_version_id", "dataset_id"],
            ["dataset_versions.id", "dataset_versions.dataset_id"],
            name="fk_dataset_versions_parent_dataset",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["manifest_bucket_file_id", "manifest_data_source_id"],
            ["bucket_files.id", "bucket_files.data_source_id"],
            name="fk_dataset_versions_manifest_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["manifest_data_source_id", "tenant_id"],
            ["data_sources.id", "data_sources.tenant_id"],
            name="fk_dataset_versions_source_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(manifest_bucket_file_id IS NULL) = (manifest_data_source_id IS NULL)",
            name="ck_dataset_versions_manifest_pair",
        ),
        CheckConstraint("version_number > 0", name="ck_dataset_versions_number"),
        CheckConstraint(
            "status IN ('assembling', 'validating', 'ready', 'failed', 'retired')",
            name="ck_dataset_versions_status",
        ),
        CheckConstraint("record_count >= 0", name="ck_dataset_versions_records"),
        CheckConstraint("fragment_count >= 0", name="ck_dataset_versions_fragments"),
        CheckConstraint("byte_size >= 0", name="ck_dataset_versions_size"),
        CheckConstraint(
            _sha256_check("content_hash"), name="ck_dataset_versions_content_sha"
        ),
        Index("ix_dataset_versions_dataset_status", "dataset_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dataset_id: Mapped[str] = mapped_column(
        ForeignKey("logical_datasets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    schema_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    parent_version_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), default="assembling", nullable=False
    )
    record_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    fragment_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    manifest_bucket_file_id: Mapped[str | None] = mapped_column(
        ForeignKey("bucket_files.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    manifest_data_source_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict] = mapped_column(
        _json_document_type(), default=dict, nullable=False
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    dataset: Mapped[LogicalDataset] = relationship(
        back_populates="versions", foreign_keys=[dataset_id]
    )
    schema: Mapped[DatasetSchema] = relationship(
        primaryjoin="DatasetVersion.schema_id == DatasetSchema.id",
        foreign_keys=[schema_id],
        overlaps="dataset,versions",
    )
    parent_version: Mapped["DatasetVersion | None"] = relationship(
        remote_side=[id], foreign_keys=[parent_version_id]
    )
    manifest_bucket_file: Mapped[BucketFile | None] = relationship(
        foreign_keys=[manifest_bucket_file_id]
    )
    fragments: Mapped[list["DatasetFragment"]] = relationship(
        primaryjoin="DatasetVersion.id == DatasetFragment.dataset_version_id",
        back_populates="dataset_version",
        order_by="DatasetFragment.ordinal",
        foreign_keys="DatasetFragment.dataset_version_id",
    )


class DatasetVersionAsset(Base):
    """Input asset versions pinned into a dataset version."""

    __tablename__ = "dataset_version_assets"
    __table_args__ = (
        UniqueConstraint(
            "dataset_version_id",
            "asset_version_id",
            "role",
            name="uq_dataset_version_assets_role",
        ),
        ForeignKeyConstraint(
            ["dataset_version_id", "dataset_id", "tenant_id"],
            [
                "dataset_versions.id",
                "dataset_versions.dataset_id",
                "dataset_versions.tenant_id",
            ],
            name="fk_version_assets_version_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["asset_version_id", "tenant_id"],
            ["data_asset_versions.id", "data_asset_versions.tenant_id"],
            name="fk_version_assets_asset_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint("ordinal >= 0", name="ck_dataset_version_assets_ordinal"),
        CheckConstraint(
            "role IN ('source', 'reference', 'rules', 'evidence', 'manifest')",
            name="ck_dataset_version_assets_role",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dataset_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    dataset_version_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    asset_version_id: Mapped[str] = mapped_column(
        ForeignKey("data_asset_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), default="source", nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    binding_document: Mapped[dict] = mapped_column(
        _json_document_type(), default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    dataset_version: Mapped[DatasetVersion] = relationship(
        foreign_keys=[dataset_version_id]
    )
    asset_version: Mapped[DataAssetVersion] = relationship(
        foreign_keys=[asset_version_id]
    )


class DatasetFragment(Base):
    """One immutable MinIO-backed fragment of a dataset relation."""

    __tablename__ = "dataset_fragments"
    __table_args__ = (
        UniqueConstraint(
            "dataset_version_id",
            "dataset_relation_id",
            "ordinal",
            name="uq_dataset_fragments_ordinal",
        ),
        UniqueConstraint(
            "id",
            "tenant_id",
            "dataset_relation_id",
            name="uq_dataset_fragments_id_tenant_relation",
        ),
        UniqueConstraint(
            "id",
            "tenant_id",
            "dataset_version_id",
            "dataset_relation_id",
            name="uq_dataset_fragments_id_evidence_scope",
        ),
        ForeignKeyConstraint(
            ["dataset_version_id", "schema_id", "dataset_id", "tenant_id"],
            [
                "dataset_versions.id",
                "dataset_versions.schema_id",
                "dataset_versions.dataset_id",
                "dataset_versions.tenant_id",
            ],
            name="fk_dataset_fragments_version_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dataset_relation_id", "schema_id", "dataset_id", "tenant_id"],
            [
                "dataset_relations.id",
                "dataset_relations.schema_id",
                "dataset_relations.dataset_id",
                "dataset_relations.tenant_id",
            ],
            name="fk_dataset_fragments_relation_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["bucket_file_id", "bucket_data_source_id"],
            ["bucket_files.id", "bucket_files.data_source_id"],
            name="fk_dataset_fragments_file_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["bucket_data_source_id", "tenant_id"],
            ["data_sources.id", "data_sources.tenant_id"],
            name="fk_dataset_fragments_source_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dataset_version_id", "schema_id"],
            ["dataset_versions.id", "dataset_versions.schema_id"],
            name="fk_dataset_fragments_version_schema",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dataset_relation_id", "schema_id"],
            ["dataset_relations.id", "dataset_relations.schema_id"],
            name="fk_dataset_fragments_relation_schema",
            ondelete="RESTRICT",
        ),
        CheckConstraint("ordinal >= 0", name="ck_dataset_fragments_ordinal"),
        CheckConstraint("row_count >= 0", name="ck_dataset_fragments_rows"),
        CheckConstraint("byte_size >= 0", name="ck_dataset_fragments_size"),
        CheckConstraint(
            "format IN ('parquet', 'arrow', 'jsonl', 'csv')",
            name="ck_dataset_fragments_format",
        ),
        CheckConstraint(
            "status IN ('pending', 'ready', 'failed', 'retired')",
            name="ck_dataset_fragments_status",
        ),
        CheckConstraint(
            _sha256_check("content_sha256"), name="ck_dataset_fragments_content_sha"
        ),
        Index(
            "ix_dataset_fragments_query",
            "dataset_version_id",
            "dataset_relation_id",
            "status",
            "ordinal",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dataset_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    dataset_version_id: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )
    dataset_relation_id: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )
    schema_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    bucket_file_id: Mapped[str] = mapped_column(
        ForeignKey("bucket_files.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    bucket_data_source_id: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    format: Mapped[str] = mapped_column(String(20), default="parquet", nullable=False)
    compression: Mapped[str] = mapped_column(String(30), default="zstd", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    row_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    statistics: Mapped[dict] = mapped_column(
        _json_document_type(), default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    dataset_version: Mapped[DatasetVersion] = relationship(
        primaryjoin="DatasetFragment.dataset_version_id == DatasetVersion.id",
        back_populates="fragments",
        foreign_keys=[dataset_version_id],
    )
    dataset_relation: Mapped[DatasetRelation] = relationship(
        primaryjoin="DatasetFragment.dataset_relation_id == DatasetRelation.id",
        foreign_keys=[dataset_relation_id],
        overlaps="dataset_version,fragments",
    )
    bucket_file: Mapped[BucketFile] = relationship(foreign_keys=[bucket_file_id])


class DatasetHead(Base):
    """Atomic environment pointer to the active immutable dataset version."""

    __tablename__ = "dataset_heads"
    __table_args__ = (
        UniqueConstraint("dataset_id", "environment", name="uq_dataset_heads_environment"),
        UniqueConstraint("id", "dataset_id", name="uq_dataset_heads_id_dataset"),
        UniqueConstraint(
            "id", "dataset_id", "tenant_id", name="uq_dataset_heads_id_scope"
        ),
        ForeignKeyConstraint(
            ["dataset_id", "tenant_id"],
            ["logical_datasets.id", "logical_datasets.tenant_id"],
            name="fk_dataset_heads_dataset_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dataset_version_id", "dataset_id", "tenant_id"],
            [
                "dataset_versions.id",
                "dataset_versions.dataset_id",
                "dataset_versions.tenant_id",
            ],
            name="fk_dataset_heads_version_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dataset_version_id", "dataset_id"],
            ["dataset_versions.id", "dataset_versions.dataset_id"],
            name="fk_dataset_heads_version_dataset",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "environment IN ('dev', 'staging', 'prod')",
            name="ck_dataset_heads_environment",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dataset_id: Mapped[str] = mapped_column(
        ForeignKey("logical_datasets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    dataset_version_id: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )
    updated_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    dataset: Mapped[LogicalDataset] = relationship(
        back_populates="heads", foreign_keys=[dataset_id]
    )
    dataset_version: Mapped[DatasetVersion] = relationship(
        primaryjoin="DatasetHead.dataset_version_id == DatasetVersion.id",
        foreign_keys=[dataset_version_id],
        overlaps="dataset,heads",
    )


class IngestionRun(Base):
    """Auditable, resumable production of one dataset version."""

    __tablename__ = "ingestion_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_ingestion_runs_idempotency"),
        UniqueConstraint("id", "tenant_id", name="uq_ingestion_runs_id_tenant"),
        ForeignKeyConstraint(
            ["dataset_id", "tenant_id"],
            ["logical_datasets.id", "logical_datasets.tenant_id"],
            name="fk_ingestion_runs_dataset_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["output_version_id", "dataset_id", "tenant_id"],
            [
                "dataset_versions.id",
                "dataset_versions.dataset_id",
                "dataset_versions.tenant_id",
            ],
            name="fk_ingestion_runs_output_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["trace_bucket_file_id", "trace_data_source_id"],
            ["bucket_files.id", "bucket_files.data_source_id"],
            name="fk_ingestion_runs_trace_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["trace_data_source_id", "tenant_id"],
            ["data_sources.id", "data_sources.tenant_id"],
            name="fk_ingestion_runs_source_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(trace_bucket_file_id IS NULL) = (trace_data_source_id IS NULL)",
            name="ck_ingestion_runs_trace_pair",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_ingestion_runs_status",
        ),
        CheckConstraint("records_read >= 0", name="ck_ingestion_runs_read"),
        CheckConstraint("records_written >= 0", name="ck_ingestion_runs_written"),
        CheckConstraint("bytes_written >= 0", name="ck_ingestion_runs_bytes"),
        Index("ix_ingestion_runs_dataset_status", "dataset_id", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dataset_id: Mapped[str] = mapped_column(
        ForeignKey("logical_datasets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    output_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
        index=True,
    )
    pipeline_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    requested_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    records_read: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    records_written: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    bytes_written: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    checkpoint: Mapped[dict] = mapped_column(
        _json_document_type(), default=dict, nullable=False
    )
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    lease_token: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    trace_bucket_file_id: Mapped[str | None] = mapped_column(
        ForeignKey("bucket_files.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    trace_data_source_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    dataset: Mapped[LogicalDataset] = relationship(foreign_keys=[dataset_id])
    output_version: Mapped[DatasetVersion | None] = relationship(
        foreign_keys=[output_version_id]
    )
    trace_bucket_file: Mapped[BucketFile | None] = relationship(
        foreign_keys=[trace_bucket_file_id]
    )
    inputs: Mapped[list["IngestionRunInput"]] = relationship(
        back_populates="ingestion_run",
        order_by="IngestionRunInput.ordinal",
        foreign_keys="IngestionRunInput.ingestion_run_id",
    )


class IngestionRunInput(Base):
    """Exact immutable inputs used by an ingestion run."""

    __tablename__ = "ingestion_run_inputs"
    __table_args__ = (
        UniqueConstraint("ingestion_run_id", "ordinal", name="uq_ingestion_inputs_ordinal"),
        ForeignKeyConstraint(
            ["ingestion_run_id", "tenant_id"],
            ["ingestion_runs.id", "ingestion_runs.tenant_id"],
            name="fk_ingestion_inputs_run_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["asset_version_id", "tenant_id"],
            ["data_asset_versions.id", "data_asset_versions.tenant_id"],
            name="fk_ingestion_inputs_asset_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dataset_version_id", "tenant_id"],
            ["dataset_versions.id", "dataset_versions.tenant_id"],
            name="fk_ingestion_inputs_version_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint("ordinal >= 0", name="ck_ingestion_inputs_ordinal"),
        CheckConstraint(
            "(CASE WHEN asset_version_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN dataset_version_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN external_ref IS NULL THEN 0 ELSE 1 END) = 1",
            name="ck_ingestion_inputs_one_source",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ingestion_run_id: Mapped[str] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(40), default="source", nullable=False)
    asset_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("data_asset_versions.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    dataset_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    external_ref: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    input_document: Mapped[dict] = mapped_column(
        _json_document_type(), default=dict, nullable=False
    )

    ingestion_run: Mapped[IngestionRun] = relationship(
        back_populates="inputs", foreign_keys=[ingestion_run_id]
    )
    asset_version: Mapped[DataAssetVersion | None] = relationship(
        foreign_keys=[asset_version_id]
    )
    dataset_version: Mapped[DatasetVersion | None] = relationship(
        foreign_keys=[dataset_version_id]
    )


class DatasetLineageEdge(Base):
    """Version-to-version lineage independent of scenario consumption."""

    __tablename__ = "dataset_lineage_edges"
    __table_args__ = (
        UniqueConstraint(
            "upstream_version_id",
            "downstream_version_id",
            "kind",
            "transformation_hash",
            name="uq_dataset_lineage_identity",
        ),
        ForeignKeyConstraint(
            ["upstream_version_id", "tenant_id"],
            ["dataset_versions.id", "dataset_versions.tenant_id"],
            name="fk_lineage_upstream_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["downstream_version_id", "tenant_id"],
            ["dataset_versions.id", "dataset_versions.tenant_id"],
            name="fk_lineage_downstream_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ingestion_run_id", "tenant_id"],
            ["ingestion_runs.id", "ingestion_runs.tenant_id"],
            name="fk_lineage_run_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "upstream_version_id <> downstream_version_id",
            name="ck_dataset_lineage_not_self",
        ),
        CheckConstraint(
            "kind IN ('copied', 'filtered', 'joined', 'aggregated', 'derived', 'manual')",
            name="ck_dataset_lineage_kind",
        ),
        CheckConstraint(
            _sha256_check("transformation_hash"), name="ck_dataset_lineage_transform_sha"
        ),
        Index("ix_dataset_lineage_downstream", "downstream_version_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    upstream_version_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    downstream_version_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    ingestion_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    transformation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    lineage_document: Mapped[dict] = mapped_column(
        _json_document_type(), default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    upstream_version: Mapped[DatasetVersion] = relationship(
        primaryjoin="DatasetLineageEdge.upstream_version_id == DatasetVersion.id",
        foreign_keys=[upstream_version_id],
    )
    downstream_version: Mapped[DatasetVersion] = relationship(
        primaryjoin="DatasetLineageEdge.downstream_version_id == DatasetVersion.id",
        foreign_keys=[downstream_version_id],
    )
    ingestion_run: Mapped[IngestionRun | None] = relationship(
        primaryjoin="DatasetLineageEdge.ingestion_run_id == IngestionRun.id",
        foreign_keys=[ingestion_run_id],
    )


class ScenarioDatasetBinding(Base):
    """A scenario's revocable reference to a dataset head or pinned version."""

    __tablename__ = "scenario_dataset_bindings"
    __table_args__ = (
        UniqueConstraint("scenario_id", "binding_key", name="uq_scenario_dataset_binding_key"),
        UniqueConstraint(
            "id",
            "scenario_id",
            "tenant_id",
            "dataset_id",
            name="uq_scenario_bindings_id_scope",
        ),
        ForeignKeyConstraint(
            ["scenario_id", "tenant_id"],
            ["business_scenarios.id", "business_scenarios.tenant_id"],
            name="fk_scenario_bindings_scenario_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["dataset_id", "tenant_id"],
            ["logical_datasets.id", "logical_datasets.tenant_id"],
            name="fk_scenario_bindings_dataset_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "role IN ('input', 'reference', 'rules', 'output')",
            name="ck_scenario_dataset_bindings_role",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled', 'error')",
            name="ck_scenario_dataset_bindings_status",
        ),
        CheckConstraint(
            "(binding_mode = 'head' AND dataset_head_id IS NOT NULL "
            "AND dataset_version_id IS NULL) OR "
            "(binding_mode = 'pinned' AND dataset_version_id IS NOT NULL "
            "AND dataset_head_id IS NULL)",
            name="ck_scenario_dataset_bindings_target",
        ),
        ForeignKeyConstraint(
            ["dataset_head_id", "dataset_id"],
            ["dataset_heads.id", "dataset_heads.dataset_id"],
            name="fk_scenario_bindings_head_dataset",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dataset_version_id", "dataset_id"],
            ["dataset_versions.id", "dataset_versions.dataset_id"],
            name="fk_scenario_bindings_version_dataset",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dataset_head_id", "dataset_id", "tenant_id"],
            ["dataset_heads.id", "dataset_heads.dataset_id", "dataset_heads.tenant_id"],
            name="fk_scenario_bindings_head_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dataset_version_id", "dataset_id", "tenant_id"],
            [
                "dataset_versions.id",
                "dataset_versions.dataset_id",
                "dataset_versions.tenant_id",
            ],
            name="fk_scenario_bindings_version_scope",
            ondelete="RESTRICT",
        ),
        Index("ix_scenario_dataset_bindings_dataset", "dataset_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dataset_id: Mapped[str] = mapped_column(
        ForeignKey("logical_datasets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    binding_key: Mapped[str] = mapped_column(String(180), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="input", nullable=False)
    binding_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    dataset_head_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    dataset_version_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    is_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    config: Mapped[dict] = mapped_column(_json_document_type(), default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    scenario: Mapped[BusinessScenario] = relationship(foreign_keys=[scenario_id])
    dataset: Mapped[LogicalDataset] = relationship(foreign_keys=[dataset_id])
    dataset_head: Mapped[DatasetHead | None] = relationship(
        primaryjoin="ScenarioDatasetBinding.dataset_head_id == DatasetHead.id",
        foreign_keys=[dataset_head_id],
        overlaps="dataset",
    )
    dataset_version: Mapped[DatasetVersion | None] = relationship(
        primaryjoin="ScenarioDatasetBinding.dataset_version_id == DatasetVersion.id",
        foreign_keys=[dataset_version_id],
        overlaps="dataset,dataset_head",
    )


class ServingProjection(Base):
    """Rebuildable serving acceleration; never the canonical dataset truth."""

    __tablename__ = "serving_projections"
    __table_args__ = (
        UniqueConstraint(
            "dataset_version_id",
            "engine",
            "projection_kind",
            "locator_hash",
            name="uq_serving_projections_identity",
        ),
        ForeignKeyConstraint(
            ["dataset_version_id", "tenant_id"],
            ["dataset_versions.id", "dataset_versions.tenant_id"],
            name="fk_serving_projections_version_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "projection_kind IN ('query', 'search', 'vector', 'cache')",
            name="ck_serving_projections_kind",
        ),
        CheckConstraint(
            "status IN ('provisioning', 'ready', 'stale', 'failed', 'retired')",
            name="ck_serving_projections_status",
        ),
        CheckConstraint(
            _sha256_check("locator_hash"), name="ck_serving_projections_locator_sha"
        ),
        CheckConstraint(
            _sha256_check("schema_hash"), name="ck_serving_projections_schema_sha"
        ),
        Index("ix_serving_projections_ready", "dataset_version_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dataset_version_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    engine: Mapped[str] = mapped_column(String(40), nullable=False)
    projection_kind: Mapped[str] = mapped_column(String(20), default="query", nullable=False)
    locator: Mapped[dict] = mapped_column(_json_document_type(), default=dict, nullable=False)
    locator_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="provisioning", nullable=False
    )
    is_rebuildable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    watermark: Mapped[dict] = mapped_column(
        _json_document_type(), default=dict, nullable=False
    )
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    dataset_version: Mapped[DatasetVersion] = relationship(
        foreign_keys=[dataset_version_id]
    )


class SemanticMapping(Base):
    """Scenario ontology entity bound to a catalog relation, never a table name."""

    __tablename__ = "semantic_mappings"
    __table_args__ = (
        UniqueConstraint("scenario_id", "mapping_key", name="uq_semantic_mappings_key"),
        UniqueConstraint(
            "scenario_id", "entity_id", "scenario_dataset_binding_id",
            name="uq_semantic_mappings_entity_binding",
        ),
        UniqueConstraint(
            "id",
            "tenant_id",
            "scenario_id",
            "dataset_id",
            "dataset_schema_id",
            "dataset_relation_id",
            "entity_id",
            name="uq_semantic_mappings_id_scope",
        ),
        UniqueConstraint(
            "id",
            "tenant_id",
            "scenario_id",
            "dataset_id",
            "dataset_schema_id",
            "dataset_relation_id",
            "entity_id",
            "scenario_dataset_binding_id",
            name="uq_semantic_mappings_id_binding_scope",
        ),
        ForeignKeyConstraint(
            ["scenario_id", "tenant_id"],
            ["business_scenarios.id", "business_scenarios.tenant_id"],
            name="fk_semantic_mappings_scenario_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["entity_id", "scenario_id"],
            ["ontology_entities.id", "ontology_entities.scenario_id"],
            name="fk_semantic_mappings_entity_scenario",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            [
                "scenario_dataset_binding_id",
                "scenario_id",
                "tenant_id",
                "dataset_id",
            ],
            [
                "scenario_dataset_bindings.id",
                "scenario_dataset_bindings.scenario_id",
                "scenario_dataset_bindings.tenant_id",
                "scenario_dataset_bindings.dataset_id",
            ],
            name="fk_semantic_mappings_binding_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dataset_schema_id", "dataset_id", "tenant_id"],
            [
                "dataset_schemas.id",
                "dataset_schemas.dataset_id",
                "dataset_schemas.tenant_id",
            ],
            name="fk_semantic_mappings_schema_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dataset_relation_id", "dataset_schema_id", "dataset_id", "tenant_id"],
            [
                "dataset_relations.id",
                "dataset_relations.schema_id",
                "dataset_relations.dataset_id",
                "dataset_relations.tenant_id",
            ],
            name="fk_semantic_mappings_relation_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('draft', 'active', 'error', 'retired')",
            name="ck_semantic_mappings_status",
        ),
        Index("ix_semantic_mappings_relation", "dataset_relation_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dataset_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scenario_dataset_binding_id: Mapped[str] = mapped_column(
        ForeignKey("scenario_dataset_bindings.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    dataset_schema_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_schemas.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    dataset_relation_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_relations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    mapping_key: Mapped[str] = mapped_column(String(180), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    identifier_strategy: Mapped[dict] = mapped_column(
        _json_document_type(), default=dict, nullable=False
    )
    filter_expression: Mapped[dict] = mapped_column(
        _json_document_type(), default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    scenario: Mapped[BusinessScenario] = relationship(foreign_keys=[scenario_id])
    entity: Mapped[OntologyEntity] = relationship(foreign_keys=[entity_id])
    scenario_dataset_binding: Mapped[ScenarioDatasetBinding] = relationship(
        foreign_keys=[scenario_dataset_binding_id]
    )
    dataset_schema: Mapped[DatasetSchema] = relationship(
        foreign_keys=[dataset_schema_id]
    )
    dataset_relation: Mapped[DatasetRelation] = relationship(
        foreign_keys=[dataset_relation_id]
    )
    field_mappings: Mapped[list["SemanticFieldMapping"]] = relationship(
        back_populates="semantic_mapping",
        order_by="SemanticFieldMapping.ordinal",
        foreign_keys="SemanticFieldMapping.semantic_mapping_id",
    )


class SemanticFieldMapping(Base):
    """Typed property-to-field mapping with structured transformations."""

    __tablename__ = "semantic_field_mappings"
    __table_args__ = (
        UniqueConstraint(
            "semantic_mapping_id",
            "ontology_property_id",
            name="uq_semantic_field_mappings_property",
        ),
        UniqueConstraint(
            "semantic_mapping_id", "ordinal", name="uq_semantic_field_mappings_ordinal"
        ),
        ForeignKeyConstraint(
            [
                "semantic_mapping_id",
                "tenant_id",
                "scenario_id",
                "dataset_id",
                "dataset_schema_id",
                "dataset_relation_id",
                "ontology_entity_id",
            ],
            [
                "semantic_mappings.id",
                "semantic_mappings.tenant_id",
                "semantic_mappings.scenario_id",
                "semantic_mappings.dataset_id",
                "semantic_mappings.dataset_schema_id",
                "semantic_mappings.dataset_relation_id",
                "semantic_mappings.entity_id",
            ],
            name="fk_semantic_fields_mapping_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["ontology_property_id", "ontology_entity_id"],
            ["ontology_properties.id", "ontology_properties.entity_id"],
            name="fk_semantic_fields_property_entity",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            [
                "dataset_field_id",
                "dataset_relation_id",
                "dataset_schema_id",
                "dataset_id",
                "tenant_id",
            ],
            [
                "dataset_fields.id",
                "dataset_fields.dataset_relation_id",
                "dataset_fields.schema_id",
                "dataset_fields.dataset_id",
                "dataset_fields.tenant_id",
            ],
            name="fk_semantic_fields_field_scope",
            ondelete="RESTRICT",
        ),
        CheckConstraint("ordinal >= 0", name="ck_semantic_field_mappings_ordinal"),
        CheckConstraint(
            "direction IN ('input', 'output', 'bidirectional')",
            name="ck_semantic_field_mappings_direction",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scenario_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    dataset_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    dataset_schema_id: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )
    dataset_relation_id: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )
    ontology_entity_id: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )
    semantic_mapping_id: Mapped[str] = mapped_column(
        ForeignKey("semantic_mappings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ontology_property_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_properties.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dataset_field_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_fields.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    direction: Mapped[str] = mapped_column(
        String(20), default="input", nullable=False
    )
    is_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    transform: Mapped[dict] = mapped_column(_json_document_type(), default=dict, nullable=False)

    semantic_mapping: Mapped[SemanticMapping] = relationship(
        back_populates="field_mappings", foreign_keys=[semantic_mapping_id]
    )
    ontology_property: Mapped[OntologyProperty] = relationship(
        foreign_keys=[ontology_property_id]
    )
    dataset_field: Mapped[DatasetField] = relationship(
        foreign_keys=[dataset_field_id]
    )


class SemanticRelationMapping(Base):
    """Ontology relation mapped through catalog field identities."""

    __tablename__ = "semantic_relation_mappings"
    __table_args__ = (
        UniqueConstraint(
            "ontology_relation_id", name="uq_semantic_relation_mappings_relation"
        ),
        ForeignKeyConstraint(
            ["scenario_id", "tenant_id"],
            ["business_scenarios.id", "business_scenarios.tenant_id"],
            name="fk_semantic_relations_scenario_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            [
                "ontology_relation_id",
                "scenario_id",
                "source_entity_id",
                "target_entity_id",
            ],
            [
                "ontology_relations.id",
                "ontology_relations.scenario_id",
                "ontology_relations.source_entity_id",
                "ontology_relations.target_entity_id",
            ],
            name="fk_semantic_relations_ontology_scope",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            [
                "scenario_dataset_binding_id",
                "scenario_id",
                "tenant_id",
                "dataset_id",
            ],
            [
                "scenario_dataset_bindings.id",
                "scenario_dataset_bindings.scenario_id",
                "scenario_dataset_bindings.tenant_id",
                "scenario_dataset_bindings.dataset_id",
            ],
            name="fk_semantic_relations_binding_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dataset_relation_id", "dataset_schema_id", "dataset_id", "tenant_id"],
            [
                "dataset_relations.id",
                "dataset_relations.schema_id",
                "dataset_relations.dataset_id",
                "dataset_relations.tenant_id",
            ],
            name="fk_semantic_relations_relation_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "source_semantic_mapping_id",
                "tenant_id",
                "scenario_id",
                "dataset_id",
                "dataset_schema_id",
                "source_dataset_relation_id",
                "source_entity_id",
                "scenario_dataset_binding_id",
            ],
            [
                "semantic_mappings.id",
                "semantic_mappings.tenant_id",
                "semantic_mappings.scenario_id",
                "semantic_mappings.dataset_id",
                "semantic_mappings.dataset_schema_id",
                "semantic_mappings.dataset_relation_id",
                "semantic_mappings.entity_id",
                "semantic_mappings.scenario_dataset_binding_id",
            ],
            name="fk_semantic_relations_source_mapping",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "target_semantic_mapping_id",
                "tenant_id",
                "scenario_id",
                "dataset_id",
                "dataset_schema_id",
                "target_dataset_relation_id",
                "target_entity_id",
                "scenario_dataset_binding_id",
            ],
            [
                "semantic_mappings.id",
                "semantic_mappings.tenant_id",
                "semantic_mappings.scenario_id",
                "semantic_mappings.dataset_id",
                "semantic_mappings.dataset_schema_id",
                "semantic_mappings.dataset_relation_id",
                "semantic_mappings.entity_id",
                "semantic_mappings.scenario_dataset_binding_id",
            ],
            name="fk_semantic_relations_target_mapping",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "source_field_id",
                "source_dataset_relation_id",
                "dataset_schema_id",
                "dataset_id",
                "tenant_id",
            ],
            [
                "dataset_fields.id",
                "dataset_fields.dataset_relation_id",
                "dataset_fields.schema_id",
                "dataset_fields.dataset_id",
                "dataset_fields.tenant_id",
            ],
            name="fk_semantic_relations_source_field",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "target_field_id",
                "target_dataset_relation_id",
                "dataset_schema_id",
                "dataset_id",
                "tenant_id",
            ],
            [
                "dataset_fields.id",
                "dataset_fields.dataset_relation_id",
                "dataset_fields.schema_id",
                "dataset_fields.dataset_id",
                "dataset_fields.tenant_id",
            ],
            name="fk_semantic_relations_target_field",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "mode IN ('foreign_key', 'join_relation', 'computed')",
            name="ck_semantic_relation_mappings_mode",
        ),
        CheckConstraint(
            "status IN ('draft', 'active', 'error', 'retired')",
            name="ck_semantic_relation_mappings_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dataset_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    dataset_schema_id: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )
    source_dataset_relation_id: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )
    target_dataset_relation_id: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )
    source_entity_id: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )
    target_entity_id: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ontology_relation_id: Mapped[str] = mapped_column(
        ForeignKey("ontology_relations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scenario_dataset_binding_id: Mapped[str] = mapped_column(
        ForeignKey("scenario_dataset_bindings.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    dataset_relation_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_relations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_semantic_mapping_id: Mapped[str] = mapped_column(
        ForeignKey("semantic_mappings.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    target_semantic_mapping_id: Mapped[str] = mapped_column(
        ForeignKey("semantic_mappings.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_field_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_fields.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    target_field_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_fields.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    mode: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    condition_document: Mapped[dict] = mapped_column(
        _json_document_type(), default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    scenario: Mapped[BusinessScenario] = relationship(foreign_keys=[scenario_id])
    ontology_relation: Mapped[OntologyRelation] = relationship(
        foreign_keys=[ontology_relation_id]
    )
    scenario_dataset_binding: Mapped[ScenarioDatasetBinding] = relationship(
        foreign_keys=[scenario_dataset_binding_id]
    )
    dataset_relation: Mapped[DatasetRelation] = relationship(
        foreign_keys=[dataset_relation_id]
    )
    source_semantic_mapping: Mapped[SemanticMapping] = relationship(
        foreign_keys=[source_semantic_mapping_id]
    )
    target_semantic_mapping: Mapped[SemanticMapping] = relationship(
        foreign_keys=[target_semantic_mapping_id]
    )
    source_field: Mapped[DatasetField] = relationship(foreign_keys=[source_field_id])
    target_field: Mapped[DatasetField] = relationship(foreign_keys=[target_field_id])


# ------------------------------------------------------------
# Append-only reasoning and reverse-derivation evidence
# ------------------------------------------------------------
class ReasoningTerm(Base):
    """Canonical subject/object identity used by observed and derived facts."""

    __tablename__ = "reasoning_terms"
    __table_args__ = (
        UniqueConstraint("tenant_id", "canonical_hash", name="uq_reasoning_terms_hash"),
        UniqueConstraint("id", "tenant_id", name="uq_reasoning_terms_id_tenant"),
        UniqueConstraint(
            "id",
            "tenant_id",
            "scenario_scope_key",
            name="uq_reasoning_terms_id_scope_key",
        ),
        ForeignKeyConstraint(
            ["scenario_id", "tenant_id"],
            ["business_scenarios.id", "business_scenarios.tenant_id"],
            name="fk_reasoning_terms_scenario_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ontology_instance_id", "scenario_id"],
            ["ontology_instances.id", "ontology_instances.scenario_id"],
            name="fk_reasoning_terms_instance_scenario",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ontology_entity_id", "scenario_id"],
            ["ontology_entities.id", "ontology_entities.scenario_id"],
            name="fk_reasoning_terms_entity_scenario",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dataset_version_id", "tenant_id"],
            ["dataset_versions.id", "dataset_versions.tenant_id"],
            name="fk_reasoning_terms_version_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "kind IN ('ontology_instance', 'ontology_entity', 'dataset_record', "
            "'iri', 'literal')",
            name="ck_reasoning_terms_kind",
        ),
        CheckConstraint(
            "(dataset_version_id IS NULL AND record_locator IS NULL) OR "
            "(dataset_version_id IS NOT NULL AND record_locator IS NOT NULL)",
            name="ck_reasoning_terms_record_pair",
        ),
        CheckConstraint(
            "(CASE WHEN ontology_instance_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN ontology_entity_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN dataset_version_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN iri IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN literal_value IS NULL THEN 0 ELSE 1 END) = 1",
            name="ck_reasoning_terms_one_identity",
        ),
        CheckConstraint(
            "(kind = 'ontology_instance' AND ontology_instance_id IS NOT NULL "
            "AND scenario_id IS NOT NULL) OR "
            "(kind = 'ontology_entity' AND ontology_entity_id IS NOT NULL "
            "AND scenario_id IS NOT NULL) OR "
            "(kind = 'dataset_record' AND dataset_version_id IS NOT NULL "
            "AND scenario_id IS NULL) OR "
            "(kind = 'iri' AND iri IS NOT NULL AND scenario_id IS NULL) OR "
            "(kind = 'literal' AND literal_value IS NOT NULL AND scenario_id IS NULL)",
            name="ck_reasoning_terms_kind_identity",
        ),
        CheckConstraint(
            "scenario_scope_key = coalesce(scenario_id, '')",
            name="ck_reasoning_terms_scenario_scope_key",
        ),
        CheckConstraint(
            _sha256_check("canonical_hash"), name="ck_reasoning_terms_canonical_sha"
        ),
        Index("ix_reasoning_terms_dataset", "dataset_version_id", "canonical_hash"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scenario_id: Mapped[str | None] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    scenario_scope_key: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", index=True
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    ontology_instance_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_instances.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    ontology_entity_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_entities.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    dataset_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    record_locator: Mapped[dict | None] = mapped_column(
        _json_document_type(), nullable=True
    )
    iri: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    literal_value: Mapped[object | None] = mapped_column(
        _json_document_type(), nullable=True
    )
    datatype_iri: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    canonical_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    ontology_instance: Mapped[OntologyInstance | None] = relationship(
        foreign_keys=[ontology_instance_id]
    )
    ontology_entity: Mapped[OntologyEntity | None] = relationship(
        foreign_keys=[ontology_entity_id]
    )
    dataset_version: Mapped[DatasetVersion | None] = relationship(
        foreign_keys=[dataset_version_id]
    )


class DerivationRun(Base):
    """Pinned and reproducible forward, reverse, or hybrid reasoning run."""

    __tablename__ = "derivation_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_derivation_runs_idempotency"),
        UniqueConstraint("id", "tenant_id", name="uq_derivation_runs_id_tenant"),
        UniqueConstraint(
            "id", "tenant_id", "scenario_id", name="uq_derivation_runs_id_scope"
        ),
        ForeignKeyConstraint(
            ["scenario_id", "tenant_id"],
            ["business_scenarios.id", "business_scenarios.tenant_id"],
            name="fk_derivation_runs_scenario_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ontology_snapshot_id", "tenant_id", "scenario_id"],
            [
                "ontology_snapshots.id",
                "ontology_snapshots.tenant_id",
                "ontology_snapshots.scenario_id",
            ],
            name="fk_derivation_runs_snapshot_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ontology_release_id", "tenant_id", "scenario_id", "ontology_snapshot_id"],
            [
                "ontology_releases.id",
                "ontology_releases.tenant_id",
                "ontology_releases.scenario_id",
                "ontology_releases.snapshot_id",
            ],
            name="fk_derivation_runs_release_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["trace_bucket_file_id", "trace_data_source_id"],
            ["bucket_files.id", "bucket_files.data_source_id"],
            name="fk_derivation_runs_trace_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["trace_data_source_id", "tenant_id"],
            ["data_sources.id", "data_sources.tenant_id"],
            name="fk_derivation_runs_source_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "ontology_snapshot_id IS NULL OR scenario_id IS NOT NULL",
            name="ck_derivation_runs_snapshot_scenario",
        ),
        CheckConstraint(
            "ontology_release_id IS NULL OR "
            "(scenario_id IS NOT NULL AND ontology_snapshot_id IS NOT NULL)",
            name="ck_derivation_runs_release_snapshot",
        ),
        CheckConstraint(
            "(trace_bucket_file_id IS NULL) = (trace_data_source_id IS NULL)",
            name="ck_derivation_runs_trace_pair",
        ),
        CheckConstraint(
            "mode IN ('forward', 'reverse', 'hybrid')",
            name="ck_derivation_runs_mode",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_derivation_runs_status",
        ),
        CheckConstraint("assertion_count >= 0", name="ck_derivation_runs_assertions"),
        CheckConstraint("evidence_count >= 0", name="ck_derivation_runs_evidence"),
        CheckConstraint(
            _sha256_check("ontology_content_hash"),
            name="ck_derivation_runs_ontology_sha",
        ),
        CheckConstraint(
            _sha256_check("rule_set_hash"), name="ck_derivation_runs_rules_sha"
        ),
        CheckConstraint(
            _sha256_check("input_fingerprint"), name="ck_derivation_runs_input_sha"
        ),
        Index("ix_derivation_runs_scenario_status", "scenario_id", "status", "created_at"),
        Index("ix_derivation_runs_fingerprint", "tenant_id", "input_fingerprint"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scenario_id: Mapped[str | None] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ontology_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_snapshots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ontology_release_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_releases.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ontology_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    engine: Mapped[str] = mapped_column(String(100), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(100), nullable=False)
    rule_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    requested_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    lease_token: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    assertion_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    evidence_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    trace_bucket_file_id: Mapped[str | None] = mapped_column(
        ForeignKey("bucket_files.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    trace_data_source_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    run_config: Mapped[dict] = mapped_column(
        _json_document_type(), default=dict, nullable=False
    )
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    scenario: Mapped[BusinessScenario | None] = relationship(
        foreign_keys=[scenario_id]
    )
    ontology_snapshot: Mapped[OntologySnapshot | None] = relationship(
        foreign_keys=[ontology_snapshot_id]
    )
    ontology_release: Mapped[OntologyRelease | None] = relationship(
        foreign_keys=[ontology_release_id]
    )
    trace_bucket_file: Mapped[BucketFile | None] = relationship(
        foreign_keys=[trace_bucket_file_id]
    )
    inputs: Mapped[list["DerivationRunInput"]] = relationship(
        back_populates="derivation_run",
        order_by="DerivationRunInput.ordinal",
        foreign_keys="DerivationRunInput.derivation_run_id",
    )


class DerivationRunInput(Base):
    """Exact dataset versions pinned as reasoning inputs."""

    __tablename__ = "derivation_run_inputs"
    __table_args__ = (
        UniqueConstraint(
            "derivation_run_id", "dataset_version_id", "role",
            name="uq_derivation_run_inputs_version",
        ),
        UniqueConstraint(
            "derivation_run_id", "ordinal", name="uq_derivation_run_inputs_ordinal"
        ),
        UniqueConstraint(
            "derivation_run_id",
            "tenant_id",
            "dataset_version_id",
            name="uq_derivation_inputs_run_tenant_version",
        ),
        ForeignKeyConstraint(
            ["derivation_run_id", "tenant_id"],
            ["derivation_runs.id", "derivation_runs.tenant_id"],
            name="fk_derivation_inputs_run_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dataset_version_id", "tenant_id"],
            ["dataset_versions.id", "dataset_versions.tenant_id"],
            name="fk_derivation_inputs_version_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint("ordinal >= 0", name="ck_derivation_run_inputs_ordinal"),
        CheckConstraint(
            "role IN ('input', 'rules', 'context', 'reference')",
            name="ck_derivation_run_inputs_role",
        ),
        CheckConstraint(
            _sha256_check("content_hash"), name="ck_derivation_inputs_content_sha"
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    derivation_run_id: Mapped[str] = mapped_column(
        ForeignKey("derivation_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    dataset_version_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="input", nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    derivation_run: Mapped[DerivationRun] = relationship(
        back_populates="inputs", foreign_keys=[derivation_run_id]
    )
    dataset_version: Mapped[DatasetVersion] = relationship(
        foreign_keys=[dataset_version_id]
    )


class Assertion(Base):
    """Append-only observed fact, derived conclusion, or reverse hypothesis."""

    __tablename__ = "assertions"
    __table_args__ = (
        UniqueConstraint(
            "derivation_run_id", "canonical_hash", name="uq_assertions_run_hash"
        ),
        UniqueConstraint("id", "tenant_id", name="uq_assertions_id_tenant"),
        UniqueConstraint(
            "id",
            "tenant_id",
            "derivation_run_id",
            name="uq_assertions_id_tenant_run",
        ),
        ForeignKeyConstraint(
            ["derivation_run_id", "tenant_id"],
            ["derivation_runs.id", "derivation_runs.tenant_id"],
            name="fk_assertions_run_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["derivation_run_id", "tenant_id", "scenario_id"],
            ["derivation_runs.id", "derivation_runs.tenant_id", "derivation_runs.scenario_id"],
            name="fk_assertions_run_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["scenario_id", "tenant_id"],
            ["business_scenarios.id", "business_scenarios.tenant_id"],
            name="fk_assertions_scenario_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["subject_term_id", "tenant_id"],
            ["reasoning_terms.id", "reasoning_terms.tenant_id"],
            name="fk_assertions_subject_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["subject_term_id", "tenant_id", "subject_scenario_scope_key"],
            [
                "reasoning_terms.id",
                "reasoning_terms.tenant_id",
                "reasoning_terms.scenario_scope_key",
            ],
            name="fk_assertions_subject_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["object_term_id", "tenant_id"],
            ["reasoning_terms.id", "reasoning_terms.tenant_id"],
            name="fk_assertions_object_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["object_term_id", "tenant_id", "object_scenario_scope_key"],
            [
                "reasoning_terms.id",
                "reasoning_terms.tenant_id",
                "reasoning_terms.scenario_scope_key",
            ],
            name="fk_assertions_object_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["predicate_property_id", "predicate_entity_id"],
            ["ontology_properties.id", "ontology_properties.entity_id"],
            name="fk_assertions_property_entity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["predicate_entity_id", "scenario_id"],
            ["ontology_entities.id", "ontology_entities.scenario_id"],
            name="fk_assertions_entity_scenario",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["predicate_relation_id", "scenario_id"],
            ["ontology_relations.id", "ontology_relations.scenario_id"],
            name="fk_assertions_relation_scenario",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["supersedes_assertion_id", "tenant_id"],
            ["assertions.id", "assertions.tenant_id"],
            name="fk_assertions_supersedes_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(CASE WHEN predicate_property_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN predicate_relation_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN predicate_key IS NULL THEN 0 ELSE 1 END) = 1",
            name="ck_assertions_one_predicate",
        ),
        CheckConstraint(
            "(predicate_property_id IS NOT NULL AND predicate_entity_id IS NOT NULL "
            "AND scenario_id IS NOT NULL) OR "
            "(predicate_relation_id IS NOT NULL AND predicate_entity_id IS NULL "
            "AND scenario_id IS NOT NULL) OR "
            "(predicate_key IS NOT NULL AND predicate_entity_id IS NULL)",
            name="ck_assertions_predicate_scope",
        ),
        CheckConstraint(
            "(subject_scenario_scope_key = '' OR "
            "(scenario_id IS NOT NULL AND subject_scenario_scope_key = scenario_id)) "
            "AND (object_scenario_scope_key = '' OR "
            "(scenario_id IS NOT NULL AND object_scenario_scope_key = scenario_id))",
            name="ck_assertions_term_scenario_scope",
        ),
        CheckConstraint(
            "assertion_kind IN ('observed', 'derived', 'hypothesis')",
            name="ck_assertions_kind",
        ),
        CheckConstraint(
            "status IN ('active', 'superseded', 'retracted', 'rejected')",
            name="ck_assertions_status",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_assertions_confidence",
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from",
            name="ck_assertions_valid_period",
        ),
        CheckConstraint(
            "system_to IS NULL OR system_to > system_from",
            name="ck_assertions_system_period",
        ),
        CheckConstraint(
            "supersedes_assertion_id IS NULL OR supersedes_assertion_id <> id",
            name="ck_assertions_not_self_supersede",
        ),
        CheckConstraint(
            _sha256_check("canonical_hash"), name="ck_assertions_canonical_sha"
        ),
        Index("ix_assertions_subject_predicate", "subject_term_id", "predicate_key"),
        Index("ix_assertions_run_status", "derivation_run_id", "status"),
        Index("ix_assertions_validity", "valid_from", "valid_to"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scenario_id: Mapped[str | None] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    derivation_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("derivation_runs.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    subject_term_id: Mapped[str] = mapped_column(
        ForeignKey("reasoning_terms.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    subject_scenario_scope_key: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", index=True
    )
    predicate_property_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_properties.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    predicate_entity_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    predicate_relation_id: Mapped[str | None] = mapped_column(
        ForeignKey("ontology_relations.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    predicate_key: Mapped[str | None] = mapped_column(String(300), nullable=True, index=True)
    object_term_id: Mapped[str] = mapped_column(
        ForeignKey("reasoning_terms.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    object_scenario_scope_key: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", index=True
    )
    polarity: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    confidence: Mapped[float] = mapped_column(
        Numeric(7, 6), default=1, nullable=False
    )
    assertion_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    canonical_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    supersedes_assertion_id: Mapped[str | None] = mapped_column(
        ForeignKey("assertions.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    valid_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    valid_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    system_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    system_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    assertion_document: Mapped[dict] = mapped_column(
        _json_document_type(), default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    derivation_run: Mapped[DerivationRun | None] = relationship(
        foreign_keys=[derivation_run_id]
    )
    subject_term: Mapped[ReasoningTerm] = relationship(foreign_keys=[subject_term_id])
    object_term: Mapped[ReasoningTerm] = relationship(foreign_keys=[object_term_id])
    predicate_property: Mapped[OntologyProperty | None] = relationship(
        foreign_keys=[predicate_property_id]
    )
    predicate_relation: Mapped[OntologyRelation | None] = relationship(
        foreign_keys=[predicate_relation_id]
    )
    supersedes_assertion: Mapped["Assertion | None"] = relationship(
        remote_side=[id], foreign_keys=[supersedes_assertion_id]
    )


class DerivationEvidence(Base):
    """Typed evidence edge supporting one assertion, with immutable locators."""

    __tablename__ = "derivation_evidence"
    __table_args__ = (
        UniqueConstraint("assertion_id", "ordinal", name="uq_derivation_evidence_ordinal"),
        ForeignKeyConstraint(
            ["assertion_id", "tenant_id"],
            ["assertions.id", "assertions.tenant_id"],
            name="fk_derivation_evidence_assertion_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["assertion_id", "tenant_id", "derivation_run_id"],
            ["assertions.id", "assertions.tenant_id", "assertions.derivation_run_id"],
            name="fk_derivation_evidence_assertion_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["derivation_run_id", "tenant_id"],
            ["derivation_runs.id", "derivation_runs.tenant_id"],
            name="fk_derivation_evidence_run_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["evidence_assertion_id", "tenant_id"],
            ["assertions.id", "assertions.tenant_id"],
            name="fk_derivation_evidence_support_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dataset_fragment_id", "tenant_id", "dataset_relation_id"],
            [
                "dataset_fragments.id",
                "dataset_fragments.tenant_id",
                "dataset_fragments.dataset_relation_id",
            ],
            name="fk_derivation_evidence_fragment_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "dataset_fragment_id",
                "tenant_id",
                "dataset_version_id",
                "dataset_relation_id",
            ],
            [
                "dataset_fragments.id",
                "dataset_fragments.tenant_id",
                "dataset_fragments.dataset_version_id",
                "dataset_fragments.dataset_relation_id",
            ],
            name="fk_derivation_evidence_fragment_input",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["derivation_run_id", "tenant_id", "dataset_version_id"],
            [
                "derivation_run_inputs.derivation_run_id",
                "derivation_run_inputs.tenant_id",
                "derivation_run_inputs.dataset_version_id",
            ],
            name="fk_derivation_evidence_pinned_input",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dataset_version_id", "tenant_id"],
            ["dataset_versions.id", "dataset_versions.tenant_id"],
            name="fk_derivation_evidence_version_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["dataset_field_id", "tenant_id", "dataset_relation_id"],
            [
                "dataset_fields.id",
                "dataset_fields.tenant_id",
                "dataset_fields.dataset_relation_id",
            ],
            name="fk_derivation_evidence_field_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["document_chunk_id", "document_data_source_id"],
            ["document_chunks.id", "document_chunks.data_source_id"],
            name="fk_derivation_evidence_chunk_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["document_data_source_id", "tenant_id"],
            ["data_sources.id", "data_sources.tenant_id"],
            name="fk_derivation_evidence_source_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["action_execution_log_id", "action_scenario_id"],
            ["action_execution_logs.id", "action_execution_logs.scenario_id"],
            name="fk_derivation_evidence_action_scenario",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["action_scenario_id", "tenant_id"],
            ["business_scenarios.id", "business_scenarios.tenant_id"],
            name="fk_derivation_evidence_action_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint("ordinal >= 0", name="ck_derivation_evidence_ordinal"),
        CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 1)",
            name="ck_derivation_evidence_score",
        ),
        CheckConstraint(
            "(CASE WHEN evidence_assertion_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN dataset_fragment_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN document_chunk_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN action_execution_log_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN external_locator IS NULL THEN 0 ELSE 1 END) = 1",
            name="ck_derivation_evidence_one_source",
        ),
        CheckConstraint(
            "evidence_assertion_id IS NULL OR evidence_assertion_id <> assertion_id",
            name="ck_derivation_evidence_not_self",
        ),
        CheckConstraint(
            "(dataset_fragment_id IS NULL AND dataset_field_id IS NULL "
            "AND dataset_relation_id IS NULL AND dataset_version_id IS NULL) OR "
            "(dataset_fragment_id IS NOT NULL AND dataset_relation_id IS NOT NULL "
            "AND dataset_version_id IS NOT NULL AND derivation_run_id IS NOT NULL)",
            name="ck_derivation_evidence_dataset_scope",
        ),
        CheckConstraint(
            "(document_chunk_id IS NULL) = (document_data_source_id IS NULL)",
            name="ck_derivation_evidence_document_pair",
        ),
        CheckConstraint(
            "(action_execution_log_id IS NULL) = (action_scenario_id IS NULL)",
            name="ck_derivation_evidence_action_pair",
        ),
        CheckConstraint(
            _sha256_check("content_hash"), name="ck_derivation_evidence_content_sha"
        ),
        Index("ix_derivation_evidence_fragment", "dataset_fragment_id", "dataset_field_id"),
        Index("ix_derivation_evidence_hash", "content_hash"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    derivation_run_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    assertion_id: Mapped[str] = mapped_column(
        ForeignKey("assertions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_assertion_id: Mapped[str | None] = mapped_column(
        ForeignKey("assertions.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    dataset_fragment_id: Mapped[str | None] = mapped_column(
        ForeignKey("dataset_fragments.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    dataset_field_id: Mapped[str | None] = mapped_column(
        ForeignKey("dataset_fields.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    dataset_relation_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    dataset_version_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    record_locator: Mapped[dict | None] = mapped_column(
        _json_document_type(), nullable=True
    )
    document_chunk_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_chunks.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    document_data_source_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    action_execution_log_id: Mapped[str | None] = mapped_column(
        ForeignKey("action_execution_logs.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    action_scenario_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    external_locator: Mapped[dict | None] = mapped_column(
        _json_document_type(), nullable=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    score: Mapped[float | None] = mapped_column(Numeric(7, 6), nullable=True)
    evidence_document: Mapped[dict] = mapped_column(
        _json_document_type(), default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    assertion: Mapped[Assertion] = relationship(
        foreign_keys=[assertion_id]
    )
    derivation_run: Mapped[DerivationRun | None] = relationship(
        foreign_keys=[derivation_run_id]
    )
    evidence_assertion: Mapped[Assertion | None] = relationship(
        foreign_keys=[evidence_assertion_id]
    )
    dataset_fragment: Mapped[DatasetFragment | None] = relationship(
        primaryjoin="DerivationEvidence.dataset_fragment_id == DatasetFragment.id",
        foreign_keys=[dataset_fragment_id]
    )
    dataset_version: Mapped[DatasetVersion | None] = relationship(
        foreign_keys=[dataset_version_id]
    )
    dataset_field: Mapped[DatasetField | None] = relationship(
        foreign_keys=[dataset_field_id]
    )
    document_chunk: Mapped[DocumentChunk | None] = relationship(
        foreign_keys=[document_chunk_id]
    )
    action_execution_log: Mapped[ActionExecutionLog | None] = relationship(
        foreign_keys=[action_execution_log_id]
    )
