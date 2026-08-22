"""Pydantic schemas (request/response models)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


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
    # UI hint only. Every management endpoint remains responsible for enforcing
    # its own server-side permission check.
    can_manage: bool = False


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
    default_value: Any = ""
    constraints: dict = Field(default_factory=dict)
    is_sensitive: bool = False


class EntityIn(BaseModel):
    name: str
    namespace: str = Field(default="", max_length=180)
    description: str = ""
    icon: str = "box"
    color: str = "#4f46e5"
    is_abstract: bool = False
    state_property: str = Field(default="", max_length=200)
    properties: list[PropertyIn] = []


class EntityOut(EntityIn):
    id: str
    scenario_id: str
    created_at: datetime

    model_config = {"from_attributes": True}


class RelationIn(BaseModel):
    name: str
    namespace: str = Field(default="", max_length=180)
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
    namespace: str = Field(default="default", min_length=1, max_length=180)
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
    state: str = Field(default="", max_length=120)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    quality: dict = Field(default_factory=dict)
    access_scope: Literal["tenant", "restricted"] = "tenant"


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


class ObjectProvenanceOut(BaseModel):
    """对象来源：保留可追溯信息，但不泄露数据源连接配置。"""

    kind: str = "manual"
    reference: str = ""
    mapping_id: str | None = None
    data_source_id: str | None = None
    data_source_name: str = ""
    table_name: str = ""
    status: str = "unknown"


class ObjectRelationOut(BaseModel):
    id: str
    direction: Literal["outgoing", "incoming"]
    relation_id: str
    relation_name: str = ""
    relation_type: str = ""
    related_object_id: str
    related_object_name: str = ""
    related_entity_id: str = ""
    related_entity_name: str = ""
    attributes: dict = Field(default_factory=dict)
    created_at: datetime


class ObjectSearchItemOut(BaseModel):
    id: str
    scenario_id: str
    entity_id: str
    entity_name: str = ""
    entity_color: str = ""
    name: str
    attributes: dict = Field(default_factory=dict)
    source: str = "manual"
    source_ref: str = ""
    state: str = ""
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    quality: dict = Field(default_factory=dict)
    access_scope: Literal["tenant", "restricted"] = "tenant"
    provenance: ObjectProvenanceOut
    relation_count: int = 0
    created_at: datetime


class ObjectSearchOut(BaseModel):
    items: list[ObjectSearchItemOut] = []
    total: int = 0
    limit: int = 50
    offset: int = 0
    query: str = ""
    entity_id: str | None = None


class ObjectDetailOut(ObjectSearchItemOut):
    relations: list[ObjectRelationOut] = []


class DataMappingIn(BaseModel):
    entity_id: str
    data_source_id: str
    data_source_binding_key: str = ""
    data_source_binding_ref: dict = Field(default_factory=dict)
    table_name: str = ""
    column_map: dict = Field(default_factory=dict)
    transform_rules: dict = Field(default_factory=dict)


class DataMappingOut(DataMappingIn):
    id: str
    scenario_id: str
    entity_name: str = ""
    data_source_name: str = ""
    data_source_type: str = ""
    status: str = "unknown"
    last_error: str = ""
    last_checked_at: datetime | None = None
    last_refreshed_at: datetime | None = None
    last_row_count: int = 0
    last_imported_count: int = 0
    created_at: datetime


class DataMappingFieldPreviewOut(BaseModel):
    property_name: str
    data_type: str = "string"
    is_key: bool = False
    is_required: bool = False
    source_column: str = ""
    source_exists: bool = False
    status: Literal["mapped", "missing", "invalid"] = "missing"
    transform_rules: list[dict] = Field(default_factory=list)


class DataMappingPreviewOut(BaseModel):
    mapping_id: str
    entity_name: str = ""
    data_source_name: str = ""
    table_name: str = ""
    ok: bool = True
    message: str = ""
    columns: list[str] = []
    sample_rows: list[list[Any]] = []
    transformed_rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    fields: list[DataMappingFieldPreviewOut] = []
    missing_properties: list[str] = []
    unmapped_columns: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []


class DataMappingTestOut(DataMappingPreviewOut):
    status: str = "unknown"
    checked_at: datetime


class DataMappingRefreshOut(BaseModel):
    mapping_id: str
    ok: bool = True
    status: str = "unknown"
    message: str = ""
    rows_scanned: int = 0
    instances_created: int = 0
    relations_created: int = 0
    last_refreshed_at: datetime | None = None
    last_error: str = ""


class DataMappingRefreshJobOut(BaseModel):
    id: str
    mapping_id: str
    scenario_id: str
    environment: str
    status: str
    limit: int = 50
    attempt: int = 0
    max_attempts: int = 3
    timeout_seconds: int = 300
    available_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    next_retry_at: datetime | None = None
    rows_scanned: int = 0
    instances_created: int = 0
    instances_updated: int = 0
    relations_created: int = 0
    connector_audit: list[dict[str, Any]] = Field(default_factory=list)
    # Provenance is intentionally an identifier/hash summary only.  The frozen
    # mapping body may contain operational connection descriptors and remains
    # worker-internal.
    definition_snapshot_id: str | None = None
    release_id: str | None = None
    definition_hash: str = ""
    definition_source: str = "live"
    error: str = ""
    created_at: datetime
    updated_at: datetime


def _empty_function_schema() -> dict:
    return {"type": "object", "properties": {}, "additionalProperties": False}


class FunctionDefinitionIn(BaseModel):
    """A typed contract with an optional closed-list built-in runtime.

    ``runtime_kind`` never accepts code, URLs or connector settings.  Runtime
    execution is limited to server-side deterministic operators exposed by
    the function runtime.
    """

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=8_000)
    input_schema: dict = Field(default_factory=_empty_function_schema)
    output_schema: dict = Field(default_factory=_empty_function_schema)
    tags: list[str] = Field(default_factory=list, max_length=20)
    visibility: Literal["scenario", "tenant"] = "scenario"
    runtime_kind: Literal[
        "contract", "weighted_score", "threshold", "geo_distance", "timeseries_aggregate"
    ] = "contract"
    runtime_config: dict = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class FunctionDefinitionOut(FunctionDefinitionIn):
    id: str
    scenario_id: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class FunctionRunIn(BaseModel):
    params: dict = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=180)


class FunctionRunOut(BaseModel):
    id: str
    tenant_id: str
    scenario_id: str
    function_id: str
    run_type: str
    status: str
    input_payload: dict = Field(default_factory=dict)
    output_payload: dict = Field(default_factory=dict)
    error: str = ""
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_by_user_id: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ScenarioDetail(ScenarioOut):
    # 由服务端按当前主体的 RBAC + 场景 ACL 计算。前端不能仅依据角色或
    # 场景归属推断该能力，否则显式 deny 与公共场景只读语义会被绕过。
    can_write: bool = False
    entities: list[EntityOut] = []
    relations: list[RelationOut] = []
    data_sources: list["DataSourceOut"] = []
    instances: list[InstanceOut] = []
    relation_instances: list[RelationInstanceOut] = []
    mappings: list[DataMappingOut] = []
    functions: list[FunctionDefinitionOut] = []
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
    # 前端据此区分“可检索的公开资源”和“可修改的自有资源”。
    can_write: bool = False

    model_config = {"from_attributes": True}


class BucketFileOut(BaseModel):
    id: str
    data_source_id: str
    filename: str
    size: int
    mime: str
    status: str
    error: str = ""
    index_status: str = "pending"
    index_error: str = ""
    index_version: str = ""
    indexed_at: datetime | None = None
    chunk_count: int = 0
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
LLM_CAPABILITIES = {"chat", "embedding", "vision", "tool"}


class LLMConfigIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    provider: str = Field(default="openai", max_length=50)
    base_url: str = Field(default="", max_length=500)
    # 只允许通过写入请求传递；所有响应模型都会强制回写为空字符串。
    api_key: str = Field(default="", max_length=500, repr=False)
    model: str = Field(default="", max_length=200)
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_tokens: int = Field(default=4096, ge=1, le=131_072)
    is_default: bool = False
    capabilities: list[str] = Field(default_factory=lambda: ["chat", "tool"], max_length=4)
    enabled: bool = True
    # 数字越小越优先；同优先级时默认模型优先。
    routing_priority: int = Field(default=100, ge=0, le=10_000)
    input_cost_per_million: float = Field(default=0.0, ge=0)
    output_cost_per_million: float = Field(default=0.0, ge=0)
    budget_limit: float = Field(default=0.0, ge=0)
    cost_currency: str = Field(default="USD", min_length=1, max_length=12)

    @field_validator("capabilities")
    @classmethod
    def normalize_capabilities(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values or []:
            capability = str(value).strip().lower()
            if capability not in LLM_CAPABILITIES:
                raise ValueError(f"不支持的模型能力: {value}")
            if capability not in normalized:
                normalized.append(capability)
        if not normalized:
            raise ValueError("至少需要配置一种模型能力")
        if "tool" in normalized and "chat" not in normalized:
            raise ValueError("tool 能力必须与 chat 能力一起配置")
        return normalized

    @field_validator("cost_currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.strip().upper()


class LLMConfigOut(LLMConfigIn):
    id: str
    # 禁止 ORM 直接序列化时意外暴露服务端密钥。
    api_key: str = Field(default="", repr=False)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class LLMRouteOut(BaseModel):
    capability: Literal["chat", "embedding", "vision", "tool"]
    selected: LLMConfigOut
    candidates: list[LLMConfigOut] = Field(default_factory=list)


class LLMTraceOut(BaseModel):
    id: str
    llm_config_id: str | None = None
    provider: str
    model: str
    capability: str
    operation: str
    status: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost: float
    currency: str
    tool_count: int
    correlation_id: str = ""
    agent_id: str | None = None
    conversation_id: str | None = None
    scenario_id: str | None = None
    user_id: str | None = None
    error: str = ""
    created_at: datetime

    model_config = {"from_attributes": True}


class LLMUsageSummaryOut(BaseModel):
    llm_config_id: str
    since: datetime
    until: datetime
    invocation_count: int = 0
    succeeded_count: int = 0
    failed_count: int = 0
    cancelled_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float = 0.0
    budget_limit: float = 0.0
    budget_remaining: float | None = None
    currency: str = "USD"
    average_latency_ms: float = 0.0
    by_capability: dict[str, dict[str, float | int]] = Field(default_factory=dict)


class LLMEvaluationIn(BaseModel):
    name: str = Field(default="基础评测", min_length=1, max_length=200)
    capability: Literal["chat", "embedding", "vision", "tool"] = "chat"
    passed: bool = True
    score: float = Field(default=0.0, ge=0, le=1)
    latency_ms: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated_cost: float = Field(default=0.0, ge=0)
    notes: str = Field(default="", max_length=2_000)
    metrics: dict = Field(default_factory=dict)


class LLMEvaluationOut(LLMEvaluationIn):
    id: str
    llm_config_id: str | None = None
    currency: str = "USD"
    created_at: datetime

    model_config = {"from_attributes": True}


class LLMEvaluationSummaryOut(BaseModel):
    llm_config_id: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    average_score: float = 0.0
    average_latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float = 0.0
    latest_at: datetime | None = None


# ──────────────────────────────────────────────
# Skill / MCP
# ──────────────────────────────────────────────
class SkillOut(BaseModel):
    id: str
    name: str
    description: str
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
    data_source_ids: list[str] = []
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_tokens: int = Field(default=4096, ge=256, le=32768)


class AgentOut(AgentIn):
    id: str
    created_at: datetime
    updated_at: datetime
    scenario_name: str = ""
    llm_name: str = ""
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
    citations: list = []
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None


class ChatEvent(BaseModel):
    type: str  # status / tool_call / tool_result / token / done / error
    data: Any = None


# ──────────────────────────────────────────────
# 全局 AI 助手
# ──────────────────────────────────────────────
class AssistantThreadOut(BaseModel):
    id: str
    scenario_id: str | None = None
    scope_key: str = "global"
    title: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DocumentSearchIn(BaseModel):
    """资料库检索请求；服务端会再次按租户可见性收窄候选数据源。"""

    query: str = Field(min_length=1, max_length=2_000)
    data_source_ids: list[str] = Field(default_factory=list, max_length=100)
    scenario_id: str | None = None
    top_k: int = Field(default=5, ge=1, le=20)


class RagCitationOut(BaseModel):
    citation_id: str
    chunk_id: str
    file_id: str
    filename: str
    data_source_id: str
    data_source_name: str = ""
    char_start: int
    char_end: int
    chunk_ordinal: int
    content_hash: str
    embedding_model: str
    index_version: str
    score: float
    vector_score: float
    keyword_score: float
    text: str


class DocumentSearchOut(BaseModel):
    query: str
    results: list[RagCitationOut] = Field(default_factory=list)
    searched_data_source_ids: list[str] = Field(default_factory=list)
    excluded_data_source_ids: list[str] = Field(default_factory=list)
    permission_message: str = ""
    retrieval_mode: str = "hybrid-vector-keyword"


class DocumentReindexOut(BaseModel):
    data_source_id: str
    files_total: int = 0
    files_indexed: int = 0
    chunks_total: int = 0
    jobs_queued: int = 0
    jobs_existing: int = 0
    items: list[dict] = Field(default_factory=list)


class AssistantMessageOut(BaseModel):
    id: str
    thread_id: str
    role: str
    content: str
    context: dict = Field(default_factory=dict)
    attachments: list = Field(default_factory=list)
    proposal: dict = Field(default_factory=dict)
    thinking: list = Field(default_factory=list)
    evidence: dict = Field(default_factory=dict)
    action_preview: dict = Field(default_factory=dict)
    created_at: datetime

    model_config = {"from_attributes": True}


class AssistantAttachmentOut(BaseModel):
    id: str
    filename: str
    mime: str = ""
    size: int = 0
    status: str = "pending"
    error: str = ""
    created_at: datetime

    model_config = {"from_attributes": True}


class AssistantChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    thread_id: str | None = None
    scenario_id: str | None = None
    page: str = ""
    path: str = ""
    selection: dict = Field(default_factory=dict)
    attachment_ids: list[str] = Field(default_factory=list)
    # The assistant may answer or prepare a reviewed change set. Effects are
    # deliberately executed only through the explicit Action/task flows.
    # ``ask`` is kept for existing clients.  The four explicit modes make the
    # safety boundary visible to newer clients: explanation is read-only,
    # draft may only prepare a Change Set, and apply/execute in chat merely
    # guide the user to the separately governed confirmation/execution flows.
    mode: Literal["ask", "explain", "draft", "apply", "execute"] = "ask"


class AssistantProposalApplyRequest(BaseModel):
    kind: Literal["scenario", "ontology", "mapping", "workflow"]
    # ``scenario`` proposals originate from a global assistant thread and do
    # not have a scenario id until they are explicitly applied.  Every other
    # proposal kind remains scenario-bound and is checked at the write edge.
    scenario_id: str | None = None
    thread_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1, max_length=64)
    confirm: bool = False
    # 兼容旧客户端字段；服务端应用时始终以已保存消息中的 payload 为准。
    payload: dict = Field(default_factory=dict)


class AssistantQuestionOptionOut(BaseModel):
    label: str
    value: str
    impact: str
    recommended: bool = False


class AssistantQuestionOut(BaseModel):
    id: str
    title: str
    message: str
    options: list[AssistantQuestionOptionOut] = Field(default_factory=list)


class AssistantEvidenceOut(BaseModel):
    rules_used: list[dict] = Field(default_factory=list)
    tools_called: list[dict] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    uncertainties: list[str] = Field(default_factory=list)


class AssistantReplyOut(BaseModel):
    thread_id: str
    reply: str
    proposal: dict = Field(default_factory=dict)
    questions: list[AssistantQuestionOut] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    sources: list[dict] = Field(default_factory=list)
    evidence: AssistantEvidenceOut = Field(default_factory=AssistantEvidenceOut)
    action_preview: dict = Field(default_factory=dict)


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
    requires_confirmation: bool = True
    idempotency_required: bool = True
    permission_scope: Literal["scenario"] = "scenario"
    access_scope: Literal["tenant", "restricted"] = "tenant"


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
    status: Literal["draft", "active", "disabled"] = "draft"
    enabled: bool = True
    access_scope: Literal["tenant", "restricted"] = "tenant"


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
    mode: str = "execute"
    idempotency_key: str | None = None
    environment: Literal["dev", "staging", "prod"] = "dev"
    definition_snapshot_id: str | None = None
    release_id: str | None = None
    definition_hash: str = ""
    definition_source: str = "live"
    result: dict = {}
    connector_audit: list[dict] = Field(default_factory=list)
    # Decision-chain provenance.  Values are copied only from authenticated
    # request/worker context; legacy or context-less rows remain explicitly
    # unknown instead of inventing a user, Agent or model identity.
    actor_type: Literal["user", "agent", "unknown"] = "unknown"
    actor_user_id: str | None = None
    agent_id: str | None = None
    llm_config_id: str | None = None
    model_name: str = ""
    permission_decision: dict = Field(default_factory=dict)
    data_context: dict = Field(default_factory=dict)
    correlation_id: str = ""
    parent_action_log_id: str | None = None
    agent_message_id: str | None = None
    assistant_message_id: str | None = None
    error: str = ""
    duration_ms: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}


class ActionExecuteRequest(BaseModel):
    params: dict = Field(default_factory=dict)
    dry_run: bool = False
    confirm: bool = False
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=120)
    # Optional preview pin.  When supplied on confirm, all fields are matched
    # against the persisted dry-run and the current runtime definition.
    preview_log_id: str | None = Field(default=None, min_length=1, max_length=32)
    correlation_id: str | None = Field(default=None, min_length=1, max_length=64)
    expected_environment: Literal["dev", "staging", "prod"] | None = None
    expected_definition_snapshot_id: str | None = Field(default=None, max_length=32)
    expected_release_id: str | None = Field(default=None, max_length=32)
    expected_definition_hash: str | None = Field(default=None, max_length=64)


class WorkflowExecuteRequest(BaseModel):
    params: dict = Field(default_factory=dict)


class WorkflowRunCreateRequest(BaseModel):
    """提交一次人工运行；可靠性策略由工作流 trigger_config 统一控制。"""

    params: dict = Field(default_factory=dict)


class WorkflowRunOut(BaseModel):
    id: str
    scenario_id: str
    workflow_id: str
    workflow_name: str = ""
    trigger_source: str
    environment: Literal["dev", "staging", "prod"] = "dev"
    definition_snapshot_id: str | None = None
    release_id: str | None = None
    definition_hash: str = ""
    definition_source: str = "live"
    status: str
    input_params: dict = Field(default_factory=dict)
    attempt: int = 0
    max_attempts: int = 1
    timeout_seconds: int = 300
    available_at: datetime | None = None
    scheduled_for: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    next_retry_at: datetime | None = None
    error: str = ""
    result: dict = Field(default_factory=dict)
    pending_approval: bool = False
    # 由服务端针对当前主体与工作流 ACL 计算；任务中心据此决定是否呈现
    # 重试/取消（execute）与审批（approve）操作，不能从任务状态推断权限。
    can_execute: bool = False
    can_approve: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class WorkflowApprovalOut(BaseModel):
    id: str
    workflow_run_id: str
    scenario_id: str
    workflow_id: str
    workflow_name: str = ""
    node_id: str
    node_name: str = ""
    instructions: str = ""
    status: str
    requested_at: datetime
    expires_at: datetime | None = None
    resolved_at: datetime | None = None
    comment: str = ""

    model_config = {"from_attributes": True}


class ApprovalDecisionIn(BaseModel):
    comment: str = Field(default="", max_length=1000)


class EventPublishIn(BaseModel):
    payload: dict = Field(default_factory=dict)
    dedupe_key: str | None = Field(default=None, min_length=1, max_length=180)


class EventEnvelopeOut(BaseModel):
    id: str
    scenario_id: str
    event_id: str
    name: str = ""
    payload: dict = Field(default_factory=dict)
    source: str = "manual"
    source_run_id: str | None = None
    environment: Literal["dev", "staging", "prod"] = "dev"
    definition_snapshot_id: str | None = None
    release_id: str | None = None
    definition_hash: str = ""
    definition_source: str = "live"
    created_at: datetime
    queued_workflow_run_ids: list[str] = Field(default_factory=list)

    model_config = {"from_attributes": True}
