"""Bounded, browser-facing contract for collaborative business investigation."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from .distillation_attachment_schemas import AttachmentOut
from .distillation_schemas import ClosedModel, DistillationDocument, RevisionRequest


InvestigationToolKey = Literal[
    "list_evidence",
    "read_evidence",
    "read_current_document",
    "review_business",
    "read_target_system",
    "read_database_sample",
    "compare_database_samples",
    "discover_data_landscape",
    "infer_data_lineage",
    "record_human_statement",
    "open_business_system",
    "inspect_business_page",
    "navigate_business_page",
    "fill_business_query",
    "click_business_control",
    "login_business_system",
    "list_library_sources",
    "list_library_files",
    "read_library_source",
    "read_attachment",
    "ask_human",
    "propose_document",
]


class ResourceSelection(ClosedModel):
    """Bounded selection of model, skill methods and read-only MCP resources."""

    llm_config_id: Annotated[str, Field(min_length=1, max_length=32)] | None = None
    skill_ids: list[Annotated[str, Field(min_length=1, max_length=32)]] = Field(default_factory=list, max_length=20)
    mcp_ids: list[Annotated[str, Field(min_length=1, max_length=32)]] = Field(default_factory=list, max_length=20)
    investigation_tool_keys: list[InvestigationToolKey] | None = Field(
        default=None,
        max_length=24,
        description="未传时使用默认全部可选内置调查工具；显式空数组仅保留人工澄清与阶段建议。",
    )

    @model_validator(mode="after")
    def normalize_references(self):
        for field_name in ("skill_ids", "mcp_ids"):
            values = getattr(self, field_name)
            if len(set(values)) != len(values):
                raise ValueError("调查资源不能重复选择")
            setattr(self, field_name, sorted(values))
        if self.investigation_tool_keys is not None:
            if len(set(self.investigation_tool_keys)) != len(self.investigation_tool_keys):
                raise ValueError("调查工具不能重复选择")
            required = {"ask_human", "propose_document"}
            if required.intersection(self.investigation_tool_keys):
                raise ValueError("人工澄清和阶段建议始终可用，无需选择")
            self.investigation_tool_keys = sorted(self.investigation_tool_keys)
        return self


class InvestigationToolOut(ClosedModel):
    key: InvestigationToolKey
    title: Annotated[str, Field(max_length=200)]
    description: Annotated[str, Field(max_length=2000)]
    selectable: bool
    always_available: bool


class InvestigationToolCatalogOut(ClosedModel):
    default_tool_keys: list[InvestigationToolKey] = Field(
        max_length=24,
        description="省略 investigation_tool_keys 时启用的可选工具。",
    )
    always_available_tool_keys: list[InvestigationToolKey] = Field(
        max_length=24,
        description="始终保留的人工澄清与阶段建议工具；显式空数组不会移除它们。",
    )
    tools: list[InvestigationToolOut] = Field(max_length=24)


class TurnCreate(RevisionRequest):
    request_id: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    message: Annotated[str, Field(max_length=12_000)] = ""
    attachment_ids: list[Annotated[str, Field(min_length=1, max_length=32)]] = Field(default_factory=list, max_length=5)
    resource_selection: ResourceSelection = Field(default_factory=ResourceSelection)

    @model_validator(mode="after")
    def require_message_or_attachment(self):
        if not self.message and not self.attachment_ids:
            raise ValueError("请填写消息或选择临时附件")
        if len(set(self.attachment_ids)) != len(self.attachment_ids):
            raise ValueError("临时附件不能重复选择")
        return self


class ClarificationQuestion(ClosedModel):
    id: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]
    title: Annotated[str, Field(min_length=1, max_length=100)]
    question: Annotated[str, Field(min_length=1, max_length=2000)]
    reason: Annotated[str, Field(min_length=1, max_length=1000)]
    options: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(default_factory=list, max_length=5)


class WebsiteObservation(ClosedModel):
    target_key: Annotated[str, Field(max_length=64)]
    url: Annotated[str, Field(max_length=2048)]
    title: Annotated[str, Field(max_length=200)]
    status: Literal["observed", "login_required", "redirect_blocked", "javascript_required"]
    text: Annotated[str, Field(max_length=16_000)]
    content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    retrieved_at: datetime
    limitations: list[Annotated[str, Field(max_length=1000)]] = Field(max_length=10)
    read_only: Literal[True]
    visible_fields: list[Annotated[str, Field(max_length=200)]] = Field(max_length=40)
    allowed_links: list[Annotated[str, Field(max_length=2048)]] = Field(max_length=20)


class LibraryReadReceipt(ClosedModel):
    data_source_id: Annotated[str, Field(max_length=32)]
    bucket_file_id: Annotated[str, Field(max_length=32)] | None = None
    evidence_key: Annotated[str, Field(max_length=64)]
    title: Annotated[str, Field(max_length=200)]
    identity_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    retrieved_at: datetime


class MCPMaterialRead(ClosedModel):
    mcp_id: Annotated[str, Field(min_length=1, max_length=32)]
    connector_revision: Annotated[int, Field(ge=1)]
    resource_key: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    text: Annotated[str, StringConstraints(strip_whitespace=False), Field(max_length=24_000)]
    content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    retrieved_at: datetime
    read_only: Literal[True] = True


class MCPReadReceipt(ClosedModel):
    mcp_id: Annotated[str, Field(max_length=32)]
    evidence_key: Annotated[str, Field(max_length=64)]
    title: Annotated[str, Field(max_length=200)]
    summary: Annotated[str, Field(max_length=1000)]
    content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    identity_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    retrieved_at: datetime
    read_only: Literal[True] = True


class ToolStep(ClosedModel):
    id: Annotated[str, Field(max_length=32)]
    tool_name: Annotated[str, Field(max_length=64)]
    title: Annotated[str, Field(max_length=200)]
    status: Literal["running", "succeeded", "failed"]
    summary: Annotated[str, Field(max_length=4000)]
    started_at: datetime
    completed_at: datetime | None = None
    source: WebsiteObservation | None = None
    library: LibraryReadReceipt | None = None
    libraries: list[LibraryReadReceipt] = Field(default_factory=list, max_length=6)
    mcp: MCPReadReceipt | None = None


class TurnOut(ClosedModel):
    id: str
    project_id: str
    turn_number: int
    request_id: str
    status: Literal["queued", "running", "waiting", "succeeded", "cancelled", "failed"]
    base_revision: int
    message: Annotated[str, Field(max_length=12_000)]
    assistant_message: Annotated[str, Field(max_length=16_000)]
    steps: list[ToolStep] = Field(max_length=40)
    questions: list[ClarificationQuestion] = Field(max_length=3)
    proposal: DistillationDocument | None
    applied_revision: int | None
    error: Annotated[str, Field(max_length=500)]
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    attachments: list[AttachmentOut] = Field(default_factory=list, max_length=5)


class ConversationPage(ClosedModel):
    turns: list[TurnOut]
    has_more: bool
