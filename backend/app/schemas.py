"""Pydantic schemas (request/response models)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────
# 通用
# ──────────────────────────────────────────────
class Msg(BaseModel):
    ok: bool = True
    message: str = ""
    data: Any = None


class RegisterIn(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=128)
    password_confirm: str
    display_name: str = Field(default="", max_length=120)


class LoginIn(BaseModel):
    email: str
    password: str


class VerifyEmailIn(BaseModel):
    email: str
    code: str = Field(min_length=6, max_length=6)


class ResendCodeIn(BaseModel):
    email: str


class ForgotPasswordIn(BaseModel):
    email: str


class ResetPasswordIn(BaseModel):
    email: str
    code: str = Field(min_length=6, max_length=6)
    password: str = Field(min_length=8, max_length=128)
    password_confirm: str


class UserOut(BaseModel):
    id: str
    email: str
    display_name: str = ""
    tenant_id: str
    email_verified: bool = True


class AuthMessage(Msg):
    email: str = ""


# ──────────────────────────────────────────────
# 本体
# ──────────────────────────────────────────────
class PropertyIn(BaseModel):
    name: str
    data_type: str = "string"
    description: str = ""
    is_key: bool = False
    is_required: bool = False
    is_enum: bool = False
    enum_values: list[str] = []
    default_value: str = ""


class EntityIn(BaseModel):
    name: str
    description: str = ""
    icon: str = "box"
    color: str = "#4f46e5"
    is_abstract: bool = False
    properties: list[PropertyIn] = []


class EntityOut(EntityIn):
    id: str
    scenario_id: str
    created_at: datetime

    model_config = {"from_attributes": True}


class RelationIn(BaseModel):
    name: str
    source_entity_id: str
    target_entity_id: str
    relation_type: str = "1:N"
    description: str = ""


class RelationOut(RelationIn):
    id: str
    scenario_id: str
    source_entity_name: str = ""
    target_entity_name: str = ""

    model_config = {"from_attributes": True}


class ScenarioIn(BaseModel):
    name: str
    description: str = ""
    industry: str = ""
    status: str = "draft"


class ScenarioOut(ScenarioIn):
    id: str
    created_at: datetime
    updated_at: datetime
    entity_count: int = 0
    relation_count: int = 0
    data_source_count: int = 0
    action_count: int = 0
    rule_count: int = 0
    event_count: int = 0
    workflow_count: int = 0

    model_config = {"from_attributes": True}


class InstanceIn(BaseModel):
    entity_id: str
    name: str
    attributes: dict = Field(default_factory=dict)
    source: str = "manual"
    source_ref: str = ""


class InstanceOut(InstanceIn):
    id: str
    scenario_id: str
    entity_name: str = ""
    entity_color: str = ""
    created_at: datetime


class RelationInstanceIn(BaseModel):
    relation_id: str
    source_instance_id: str
    target_instance_id: str
    attributes: dict = Field(default_factory=dict)


class RelationInstanceOut(RelationInstanceIn):
    id: str
    scenario_id: str
    relation_name: str = ""
    source_instance_name: str = ""
    target_instance_name: str = ""
    created_at: datetime


class DataMappingIn(BaseModel):
    entity_id: str
    data_source_id: str
    table_name: str = ""
    column_map: dict = Field(default_factory=dict)


class DataMappingOut(DataMappingIn):
    id: str
    scenario_id: str
    entity_name: str = ""
    data_source_name: str = ""
    data_source_type: str = ""
    created_at: datetime


class ScenarioDetail(ScenarioOut):
    entities: list[EntityOut] = []
    relations: list[RelationOut] = []
    data_sources: list["DataSourceOut"] = []
    instances: list[InstanceOut] = []
    relation_instances: list[RelationInstanceOut] = []
    mappings: list[DataMappingOut] = []
    actions: list["ActionOut"] = []
    rules: list["RuleOut"] = []
    events: list["EventOut"] = []
    workflows: list["WorkflowOut"] = []


# ──────────────────────────────────────────────
# 数据源
# ──────────────────────────────────────────────
class DataSourceIn(BaseModel):
    name: str
    type: Literal["mysql", "postgres", "sqlite", "file_bucket"] = "mysql"
    scenario_id: str | None = None
    config: dict = Field(default_factory=dict)


class DataSourceOut(DataSourceIn):
    id: str
    scenario_id: str | None = None
    status: str = "unknown"
    last_error: str = ""
    created_at: datetime
    file_count: int = 0

    model_config = {"from_attributes": True}


class BucketFileOut(BaseModel):
    id: str
    data_source_id: str
    filename: str
    size: int
    mime: str
    status: str
    error: str = ""
    created_at: datetime

    model_config = {"from_attributes": True}


class TableInfo(BaseModel):
    name: str
    columns: list[dict] = []
    row_count: int = -1


class QueryResult(BaseModel):
    columns: list[str] = []
    rows: list[list] = []
    row_count: int = 0
    truncated: bool = False


# ──────────────────────────────────────────────
# LLM
# ──────────────────────────────────────────────
class LLMConfigIn(BaseModel):
    name: str
    provider: str = "openai"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.2
    max_tokens: int = 4096
    is_default: bool = False


class LLMConfigOut(LLMConfigIn):
    id: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ──────────────────────────────────────────────
# Skill / MCP
# ──────────────────────────────────────────────
class SkillOut(BaseModel):
    id: str
    name: str
    description: str
    path: str
    source: str
    enabled: bool
    metadata: dict = {}
    created_at: datetime

    model_config = {"from_attributes": True}


class SkillToggle(BaseModel):
    enabled: bool


class MCPConfigIn(BaseModel):
    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = []
    url: str = ""
    env: dict = {}
    headers: dict = {}
    enabled: bool = True


class MCPConfigOut(MCPConfigIn):
    id: str
    created_at: datetime

    model_config = {"from_attributes": True}


class MCPToolInfo(BaseModel):
    name: str
    description: str = ""
    input_schema: dict = {}


# ──────────────────────────────────────────────
# Agent
# ──────────────────────────────────────────────
class AgentIn(BaseModel):
    name: str
    description: str = ""
    scenario_id: Optional[str] = None
    llm_config_id: Optional[str] = None
    system_prompt: str = ""
    skill_ids: list[str] = []
    mcp_ids: list[str] = []
    data_source_ids: list[str] = []
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_tokens: int = Field(default=4096, ge=256, le=32768)


class AgentOut(AgentIn):
    id: str
    created_at: datetime
    updated_at: datetime
    scenario_name: str = ""
    llm_name: str = ""
    skill_names: list[str] = []
    mcp_names: list[str] = []
    data_source_names: list[str] = []

    model_config = {"from_attributes": True}


class ConversationOut(BaseModel):
    id: str
    agent_id: str
    title: str
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageOut(BaseModel):
    id: str
    conversation_id: str
    role: str
    content: str
    tool_calls: list = []
    tool_results: list = []
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None


class ChatEvent(BaseModel):
    type: str  # status / tool_call / tool_result / token / done / error
    data: Any = None


# ──────────────────────────────────────────────
# 本体扩展：操作 / 规则 / 事件 / 工作流
# ──────────────────────────────────────────────
class ActionIn(BaseModel):
    entity_id: str
    name: str
    description: str = ""
    input_schema: dict = Field(default_factory=dict)
    executor_type: Literal["sql", "skill", "mcp", "http", "script"] = "sql"
    executor_config: dict = Field(default_factory=dict)
    precondition: str = ""
    postcondition: str = ""
    enabled: bool = True


class ActionOut(ActionIn):
    id: str
    scenario_id: str
    entity_name: str = ""
    created_at: datetime

    model_config = {"from_attributes": True}


class RuleIn(BaseModel):
    entity_id: Optional[str] = None
    name: str
    description: str = ""
    condition: dict = Field(default_factory=dict)
    action_on_match: str = ""
    trigger_action_ids: list[str] = []
    severity: Literal["info", "warning", "critical"] = "info"
    enabled: bool = True


class RuleOut(RuleIn):
    id: str
    scenario_id: str
    entity_name: str = ""
    created_at: datetime

    model_config = {"from_attributes": True}


class EventIn(BaseModel):
    name: str
    description: str = ""
    payload_schema: dict = Field(default_factory=dict)
    trigger_source: str = ""
    enabled: bool = True


class EventOut(EventIn):
    id: str
    scenario_id: str
    created_at: datetime

    model_config = {"from_attributes": True}


class WorkflowIn(BaseModel):
    name: str
    description: str = ""
    trigger_type: Literal["manual", "scheduled", "event"] = "manual"
    trigger_config: dict = Field(default_factory=dict)
    steps: list = Field(default_factory=list)  # 旧版线性步骤（兼容）
    nodes: list = Field(default_factory=list)  # 可视化 DAG 节点
    edges: list = Field(default_factory=list)  # 可视化 DAG 连线
    enabled: bool = True


class WorkflowOut(WorkflowIn):
    id: str
    scenario_id: str
    created_at: datetime

    model_config = {"from_attributes": True}


class WorkflowGenerateRequest(BaseModel):
    description: str = ""


class ActionExecutionLogOut(BaseModel):
    id: str
    scenario_id: str
    target_type: str
    target_id: str
    target_name: str
    input_params: dict = {}
    status: str
    result: dict = {}
    error: str = ""
    duration_ms: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}


class ActionExecuteRequest(BaseModel):
    params: dict = Field(default_factory=dict)


class WorkflowExecuteRequest(BaseModel):
    params: dict = Field(default_factory=dict)
