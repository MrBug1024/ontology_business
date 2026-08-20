"""ORM models for the ontology business agent platform."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ──────────────────────────────────────────────
# 租户、用户与认证
# ──────────────────────────────────────────────
class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    users: Mapped[list["User"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


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
    tenant: Mapped[Tenant | None] = relationship()


class OntologyEntity(Base):
    """本体中的实体类型（Object Type），例如对象、事项、资源等领域概念。"""

    __tablename__ = "ontology_entities"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    icon: Mapped[str] = mapped_column(String(50), default="box")
    color: Mapped[str] = mapped_column(String(20), default="#4f46e5")
    is_abstract: Mapped[bool] = mapped_column(Boolean, default=False)
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
    default_value: Mapped[str] = mapped_column(String(500), default="")

    entity: Mapped[OntologyEntity] = relationship(back_populates="properties")


class OntologyRelation(Base):
    """实体间关系（Link Type）。"""

    __tablename__ = "ontology_relations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("business_scenarios.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
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
    table_name: Mapped[str] = mapped_column(String(300), default="")
    column_map: Mapped[dict] = mapped_column(JSON, default=dict)  # {本体属性名: 表列名}
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
    status: Mapped[str] = mapped_column(String(20), default="unknown")  # unknown / ok / error
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    scenario: Mapped[BusinessScenario | None] = relationship(back_populates="data_sources")
    files: Mapped[list[BucketFile]] = relationship(
        back_populates="data_source", cascade="all, delete-orphan"
    )
    tenant: Mapped[Tenant | None] = relationship()


class BucketFile(Base):
    """文件桶中的业务文件及其解析结果。"""

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    data_source: Mapped[DataSource] = relationship(back_populates="files")


# ──────────────────────────────────────────────
# LLM 配置
# ──────────────────────────────────────────────
class LLMConfig(Base):
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
    model: Mapped[str] = mapped_column(String(200), default="")
    temperature: Mapped[float] = mapped_column(Float, default=0.2)
    max_tokens: Mapped[int] = mapped_column(Integer, default=4096)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    tenant: Mapped[Tenant | None] = relationship()


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


# ──────────────────────────────────────────────
# 本体扩展：操作 / 规则 / 事件 / 工作流
# （元模型层：平台只提供框架，业务语义由用户定义）
# ──────────────────────────────────────────────
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
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    scenario: Mapped[BusinessScenario] = relationship()


class ActionExecutionLog(Base):
    """操作执行日志：记录每次 Action/Workflow 的执行轨迹（可追溯）。"""

    __tablename__ = "action_execution_logs"

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
    # 执行状态: running / success / failed
    status: Mapped[str] = mapped_column(String(20), default="running")
    # 执行结果
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    # 错误信息
    error: Mapped[str] = mapped_column(Text, default="")
    # 执行耗时（毫秒）
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    scenario: Mapped[BusinessScenario] = relationship()
