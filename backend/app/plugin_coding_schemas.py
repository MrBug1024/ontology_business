"""Closed contracts for a reviewed plugin coding workspace."""
from __future__ import annotations

from typing import Annotated, Literal
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from .capability_access_schemas import CapabilityAccessScenarioOut, CapabilityAccessDeploymentOut
from .external_api_schemas import ExternalCapabilityPortOut
from .scenario_package_schemas import ScenarioPackageBuild, PackageCapability
from .scenario_capability_blueprint_schemas import ScenarioCapabilityBlueprintOut, PluginDeliveryProfileOut
from .services.plugin_source_policy import editable_path

PLUGIN_VERSION_PATTERN = r"^(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})$"
MAX_CODING_BODY_BYTES = 512 * 1024


class CodingResourceSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    llm_config_id: str = Field(min_length=1, max_length=32)
    skill_ids: list[Annotated[str, Field(min_length=1, max_length=32)]] = Field(default_factory=list, max_length=10)
    mcp_ids: list[Annotated[str, Field(min_length=1, max_length=32)]] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def unique_resources(self):
        for name in ("skill_ids", "mcp_ids"):
            values = getattr(self, name)
            if len(set(values)) != len(values):
                raise ValueError("编程扩展不能重复安装")
            setattr(self, name, sorted(values))
        return self


class PluginCodingSettings(CodingResourceSelection):
    expected_revision: int = Field(ge=1)
    request_id: str = Field(min_length=8, max_length=100)


class CodingModelOption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str = Field(max_length=200)
    model: str = Field(max_length=200)
    supports_tools: bool


class CodingSkillOption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str = Field(max_length=200)
    description: str = Field(max_length=2000)
    version: str = Field(max_length=40)
    mode: Literal["instructions"] = "instructions"


class CodingMCPOption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str = Field(max_length=200)
    transport: Literal["sse", "streamable_http"]
    mode: Literal["read_only_resources"] = "read_only_resources"


class CodingToolOption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    title: str
    description: str


class CodingResourceCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    models: list[CodingModelOption] = Field(max_length=200)
    skills: list[CodingSkillOption] = Field(max_length=10)
    mcps: list[CodingMCPOption] = Field(max_length=200)
    base_tools: list[CodingToolOption] = Field(max_length=10)


class CodingResourceReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str = Field(max_length=100)
    title: str = Field(max_length=200)
    read_only: Literal[True] = True
    run_id: str = Field(max_length=32)
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    retrieved_at: str = Field(max_length=40)


class CodingReadinessIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    axis: str = Field(default='validation', max_length=40)
    code: str = Field(max_length=100)
    message: str = Field(max_length=4000)
    blocking: bool
    port_key: str | None = Field(default=None, max_length=240)


class CodingReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ready: bool = False
    issues: list[CodingReadinessIssue] = Field(default_factory=list, max_length=100)


class CodingCapabilityOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal['function', 'action', 'rule', 'workflow']
    key: str = Field(max_length=240)
    name: str = Field(max_length=1000)
    description: str = Field(max_length=20000)
    definition_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]
    side_effect: bool
    requires_confirmation: bool
    idempotency_required: bool
    data_ports: list[ExternalCapabilityPortOut] = Field(max_length=100)
    readiness: CodingReadiness = Field(default_factory=CodingReadiness)


class CodingFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=160)
    content: str = Field(max_length=32768)

    @model_validator(mode="after")
    def contained_path(self):
        if not editable_path(self.path):
            raise ValueError('文件路径不可编辑')
        return self


class PluginCodingCreate(ScenarioPackageBuild):
    request_id: str = Field(min_length=8, max_length=100)
    llm_config_id: str = Field(min_length=1, max_length=32)
    instruction: str = Field(min_length=1, max_length=4000)
    skill_ids: list[Annotated[str, Field(min_length=1, max_length=32)]] = Field(default_factory=list, max_length=10)
    mcp_ids: list[Annotated[str, Field(min_length=1, max_length=32)]] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def valid_extensions(self):
        CodingResourceSelection(llm_config_id=self.llm_config_id, skill_ids=self.skill_ids, mcp_ids=self.mcp_ids)
        return self


class PluginCodingDraftCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_revision: int = Field(ge=1)
    target: Literal["claude_code", "codex"] = "claude_code"
    capabilities: list[PackageCapability] = Field(min_length=1, max_length=20)
    request_id: str = Field(min_length=8, max_length=100)
    llm_config_id: str = Field(min_length=1, max_length=32)
    instruction: str = Field(min_length=1, max_length=4000)
    skill_ids: list[Annotated[str, Field(min_length=1, max_length=32)]] = Field(default_factory=list, max_length=10)
    mcp_ids: list[Annotated[str, Field(min_length=1, max_length=32)]] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def unique_selection(self):
        if len({(item.kind, item.key) for item in self.capabilities}) != len(self.capabilities):
            raise ValueError('不能重复选择能力')
        CodingResourceSelection(llm_config_id=self.llm_config_id, skill_ids=self.skill_ids, mcp_ids=self.mcp_ids)
        return self


class PluginCodingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    request_id: str = Field(min_length=8, max_length=100)
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    action: Literal["generate", "save", "discuss", "stop"]
    base_files_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    instruction: str = Field(default="", max_length=4000)
    files: list[CodingFile] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def complete_instruction(self):
        if self.action in {"generate", "discuss"} and not self.instruction.strip():
            raise ValueError("请描述本轮编码目标或修正意见")
        if self.action == "discuss" and self.files:
            raise ValueError("讨论轮次不能提交文件")
        if self.action == "stop" and (self.files or self.instruction):
            raise ValueError("停止轮次不能提交文件或需求")
        if len({item.path for item in self.files}) != len(self.files):
            raise ValueError("不能重复编辑同一文件")
        return self


class PluginCodingExport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    files_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    format: Literal["plugin", "marketplace"] = "plugin"
    plugin_version: str = Field(pattern=PLUGIN_VERSION_PATTERN)
    confirmed_code_review: Literal[True]


class PluginCodingReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    files_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    plugin_version: str = Field(pattern=PLUGIN_VERSION_PATTERN)
    confirmed_code_review: Literal[True]
    acceptance: ScenarioPackageBuild | None = None


class PluginArtifactDownload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    format: Literal["plugin", "marketplace"] = "plugin"


class PluginArtifactRetire(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class PluginArtifactDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class PluginArtifactOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    workspace_id: str
    scenario_id: str
    scenario_name: str
    release_id: str
    release_name: str
    package_name: str
    host: Literal['claude_code', 'codex'] = 'claude_code'
    host_label: Literal['Claude Code', 'Codex'] = 'Claude Code'
    plugin_version: str
    artifact_hash: str
    created_at: datetime
    available: bool
    unavailable_reason: str
    retired: bool = False
    retired_at: datetime | None = None


class PluginArtifactPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[PluginArtifactOut]
    offset: int
    limit: int
    has_more: bool


class CodingFileOut(BaseModel):
    path: str
    content: str
    previous: str = ""
    editable: bool


class CodingEventOut(BaseModel):
    sequence: int
    kind: str
    message: str
    path: str = ""
    run_id: str | None = None


class CodingTurnOut(BaseModel):
    id: str
    instruction: str
    status: Literal["waiting_upload", "queued", "running", "succeeded", "failed", "cancelled"]
    created_at: str
    mode: Literal["discuss", "generate"] = "generate"


class CodingWorkspaceOut(BaseModel):
    id: str
    host: Literal['claude_code', 'codex'] = 'claude_code'
    release_id: str
    scenario_id: str
    revision: int
    phase: Literal["draft", "generating", "ready_for_review", "validation_failed", "released"]
    source_phase: Literal["draft", "generating", "ready_for_review", "validation_failed", "released"]
    session_id: str = ""
    session_title: str = ""
    files_hash: str
    files: list[CodingFileOut]
    events: list[CodingEventOut]
    validation: list[str]
    active_run_id: str | None
    run_status: str | None
    exported_count: int
    turns: list[CodingTurnOut]
    capabilities: list[CodingCapabilityOut] = Field(default_factory=list, max_length=20)
    business_acceptance_required: bool = False
    resource_selection: CodingResourceSelection
    resource_receipts: list[CodingResourceReceipt] = Field(default_factory=list, max_length=30)
    scenario_blueprint: ScenarioCapabilityBlueprintOut | None = None
    delivery_profile: PluginDeliveryProfileOut | None = None


class CodingSessionOut(BaseModel):
    # A session is a context-bounded conversation on the shared plugin project;
    # it never implies a plugin version. `frozen` marks legacy workspaces whose
    # code diverged before projects existed and are kept as read-only history.
    model_config = ConfigDict(extra="forbid")
    id: str
    project_id: str
    release_id: str
    scenario_id: str
    title: str
    host: Literal['claude_code', 'codex'] = 'claude_code'
    phase: str
    active: bool = False
    frozen: bool = False
    created_at: datetime


class CodingProjectCapabilityOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal['function', 'action', 'rule', 'workflow']
    key: str = Field(max_length=240)
    name: str = Field(max_length=1000)


class CodingProjectSummaryOut(BaseModel):
    # Explorer projection: the durable per-scenario, per-host plugin project that
    # owns the only mutable source tree. plugin_version is the latest reviewed
    # snapshot version (empty before the first review); reviews never change the
    # project itself, so development state and publication state stay decoupled.
    model_config = ConfigDict(extra="forbid")
    id: str
    scenario_id: str
    release_id: str
    host: Literal['claude_code', 'codex'] = 'claude_code'
    phase: Literal["draft", "generating", "ready_for_review", "validation_failed", "released"]
    plugin_version: str = ""
    capabilities: list[CodingProjectCapabilityOut] = Field(default_factory=list, max_length=20)
    created_at: datetime


class CodingProjectOut(BaseModel):
    # Explorer projection for one plugin project: source files and identity only.
    # Conversation turns, receipts and events stay behind the full workspace read.
    model_config = ConfigDict(extra="forbid")
    id: str
    release_id: str
    revision: int = Field(ge=1)
    phase: Literal["draft", "generating", "ready_for_review", "validation_failed", "released"]
    files: list[CodingFileOut] = Field(max_length=64)


class CodingContextOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: CapabilityAccessScenarioOut
    deployment: CapabilityAccessDeploymentOut
    capabilities: list[CodingCapabilityOut] = Field(max_length=200)
    scenario_blueprint: ScenarioCapabilityBlueprintOut
    delivery_profile: PluginDeliveryProfileOut
