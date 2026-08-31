"""Public contracts for the governed data catalog and scenario bindings."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


CatalogAssetKind = Literal["file", "stream", "api", "database", "generated", "other"]
CatalogBindingRole = Literal[
    "modeling_evidence",
    "test_fixture",
    "invocation_input",
    "reference",
    "rules",
    "output",
    "input",
]
CatalogEnvironment = Literal["dev", "staging", "prod"]
CatalogUploadPurpose = Literal[
    "managed_asset",
    "validation_asset",
    "invocation_attachment",
]


class ConnectorBindingOptionOut(BaseModel):
    """Credential-free connector choice for a governed invocation port."""

    binding_key: str
    label: str
    connector_kind: Literal["data_source", "mcp", "llm"]
    environment: CatalogEnvironment
    ready: bool
    blocking_reason: str = ""
    capabilities: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


class DataAssetCreate(BaseModel):
    key: str = Field(min_length=1, max_length=180)
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=8_000)
    kind: CatalogAssetKind = "file"
    media_type: str = Field(default="", max_length=200)
    labels: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class DataAssetOut(DataAssetCreate):
    id: str
    tenant_id: str
    lifecycle_status: Literal["active", "retired"]
    created_by_user_id: str | None = None
    created_at: datetime
    updated_at: datetime
    retired_at: datetime | None = None
    version_count: int = 0

    model_config = {"from_attributes": True}


class DataAssetVersionRegister(BaseModel):
    bucket_file_id: str = Field(min_length=1, max_length=32)
    provenance_kind: Literal[
        "upload", "connector", "import", "reconstruction", "generated"
    ] = "upload"
    version_document: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class DataAssetVersionOut(BaseModel):
    id: str
    tenant_id: str
    asset_id: str
    version_number: int
    provenance_kind: str
    status: str
    content_sha256: str
    byte_size: int
    version_document: dict[str, Any] = Field(default_factory=dict)
    created_by_user_id: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class CatalogManagedUploadMetadata(BaseModel):
    """Client-controlled metadata for one server-managed catalog upload.

    Physical storage coordinates are deliberately absent.  The caller selects
    an existing managed file bucket by id; the server owns every MinIO locator
    and credential used afterwards.
    """

    file_bucket_id: str = Field(min_length=1, max_length=32)
    purpose: CatalogUploadPurpose = "managed_asset"
    asset_key: str | None = Field(default=None, min_length=1, max_length=180)
    name: str | None = Field(default=None, min_length=1, max_length=300)
    description: str = Field(default="", max_length=8_000)
    labels: dict[str, Any] = Field(default_factory=dict)
    expires_in_seconds: int | None = Field(
        default=None,
        ge=300,
        le=7 * 24 * 60 * 60,
    )

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def lifecycle_is_explicit(self) -> "CatalogManagedUploadMetadata":
        if self.purpose != "invocation_attachment" and self.expires_in_seconds is not None:
            raise ValueError("长期资产不能设置临时附件到期时间")
        return self


class CatalogManagedAssetRefOut(BaseModel):
    id: str
    key: str
    name: str
    kind: str
    media_type: str
    lifecycle_status: str


class CatalogManagedVersionRefOut(BaseModel):
    id: str
    asset_id: str
    version_number: int
    provenance_kind: str
    status: str
    content_sha256: str
    byte_size: int
    profile: dict[str, Any] = Field(default_factory=dict)
    lifecycle: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class CatalogManagedUploadOut(BaseModel):
    """Safe upload result containing catalog references, never MinIO locators."""

    purpose: CatalogUploadPurpose
    temporary: bool
    expires_at: datetime | None = None
    created: bool
    asset: CatalogManagedAssetRefOut
    version: CatalogManagedVersionRefOut


class ValidationDatasetBuildIn(BaseModel):
    asset_version_ids: list[str] = Field(min_length=1, max_length=20)
    name: str = Field(default="验证数据包", min_length=1, max_length=300)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def unique_asset_versions(self) -> "ValidationDatasetBuildIn":
        if len(self.asset_version_ids) != len(set(self.asset_version_ids)):
            raise ValueError("验证数据文件不能重复")
        return self


class ValidationDatasetOut(BaseModel):
    dataset_id: str
    dataset_version_id: str
    content_hash: str
    schema_hash: str
    record_count: int
    byte_size: int
    relation_names: list[str] = Field(default_factory=list)
    source_asset_version_ids: list[str] = Field(default_factory=list)
    reused: bool = False


class ValidationDatasetJobOut(BaseModel):
    id: str
    status: Literal["queued", "running", "succeeded", "failed"]
    error: str = ""
    created_at: datetime
    updated_at: datetime
    result: ValidationDatasetOut | None = None


class LogicalDatasetCreate(BaseModel):
    key: str = Field(min_length=1, max_length=180)
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=8_000)
    labels: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class LogicalDatasetOut(LogicalDatasetCreate):
    id: str
    tenant_id: str
    lifecycle_status: Literal["active", "retired"]
    created_by_user_id: str | None = None
    created_at: datetime
    updated_at: datetime
    retired_at: datetime | None = None
    schema_count: int = 0
    version_count: int = 0
    heads: dict[str, str] = Field(default_factory=dict)

    model_config = {"from_attributes": True}


class DatasetFieldCreate(BaseModel):
    field_key: str = Field(min_length=1, max_length=180)
    source_name: str = Field(min_length=1, max_length=300)
    logical_type: str = Field(min_length=1, max_length=80)
    physical_type: str = Field(default="", max_length=200)
    nullable: bool = True
    key_ordinal: int | None = Field(default=None, ge=0)
    semantic_role: str = Field(default="", max_length=80)
    field_document: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class DatasetRelationCreate(BaseModel):
    relation_key: str = Field(min_length=1, max_length=180)
    display_name: str = Field(min_length=1, max_length=300)
    kind: Literal["table", "view", "stream", "document"] = "table"
    description: str = Field(default="", max_length=8_000)
    fields: list[DatasetFieldCreate] = Field(default_factory=list, max_length=500)

    model_config = {"extra": "forbid"}


class DatasetSchemaCreate(BaseModel):
    compatibility: Literal["none", "backward", "forward", "full"] = "none"
    schema_document: dict[str, Any] = Field(default_factory=dict)
    relations: list[DatasetRelationCreate] = Field(min_length=1, max_length=200)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def unique_keys(self) -> "DatasetSchemaCreate":
        relation_keys = [item.relation_key for item in self.relations]
        if len(relation_keys) != len(set(relation_keys)):
            raise ValueError("relation_key 不能重复")
        for relation in self.relations:
            field_keys = [item.field_key for item in relation.fields]
            if len(field_keys) != len(set(field_keys)):
                raise ValueError(f"关系 {relation.relation_key} 的 field_key 不能重复")
        return self


class DatasetFieldOut(DatasetFieldCreate):
    id: str
    ordinal: int

    model_config = {"from_attributes": True}


class DatasetRelationOut(BaseModel):
    id: str
    relation_key: str
    display_name: str
    kind: str
    ordinal: int
    description: str = ""
    fields: list[DatasetFieldOut] = Field(default_factory=list)


class DatasetSchemaOut(BaseModel):
    id: str
    tenant_id: str
    dataset_id: str
    schema_version: int
    schema_hash: str
    compatibility: str
    schema_document: dict[str, Any] = Field(default_factory=dict)
    relations: list[DatasetRelationOut] = Field(default_factory=list)
    created_by_user_id: str | None = None
    created_at: datetime


class DatasetVersionCreate(BaseModel):
    schema_id: str = Field(min_length=1, max_length=32)
    asset_version_ids: list[str] = Field(default_factory=list, max_length=200)
    manifest: dict[str, Any] = Field(default_factory=dict)
    parent_version_id: str | None = Field(default=None, max_length=32)

    model_config = {"extra": "forbid"}


class DatasetVersionOut(BaseModel):
    id: str
    tenant_id: str
    dataset_id: str
    schema_id: str
    version_number: int
    parent_version_id: str | None = None
    status: str
    record_count: int
    fragment_count: int
    byte_size: int
    content_hash: str
    manifest: dict[str, Any] = Field(default_factory=dict)
    created_by_user_id: str | None = None
    created_at: datetime
    ready_at: datetime | None = None

    model_config = {"from_attributes": True}


class DatasetHeadSet(BaseModel):
    dataset_version_id: str = Field(min_length=1, max_length=32)
    # Optional compare-and-set guard. Existing clients may keep unconditional
    # writes, while migration/cutover clients can reject a stale Head update.
    expected_dataset_version_id: str | None = Field(default=None, max_length=32)

    model_config = {"extra": "forbid"}


class DatasetHeadOut(BaseModel):
    id: str
    tenant_id: str
    dataset_id: str
    environment: CatalogEnvironment
    dataset_version_id: str
    updated_by_user_id: str | None = None
    updated_at: datetime

    model_config = {"from_attributes": True}


class ScenarioDatasetBindingCreate(BaseModel):
    dataset_id: str = Field(min_length=1, max_length=32)
    binding_key: str = Field(min_length=1, max_length=180)
    environment: CatalogEnvironment = "dev"
    role: CatalogBindingRole
    binding_mode: Literal["head", "pinned"]
    dataset_head_id: str | None = Field(default=None, max_length=32)
    dataset_version_id: str | None = Field(default=None, max_length=32)
    is_required: bool = True
    status: Literal["active", "disabled", "error"] = "active"
    config: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def exactly_one_target(self) -> "ScenarioDatasetBindingCreate":
        if self.binding_mode == "head":
            if not self.dataset_head_id or self.dataset_version_id:
                raise ValueError("head 绑定必须且只能提供 dataset_head_id")
        elif not self.dataset_version_id or self.dataset_head_id:
            raise ValueError("pinned 绑定必须且只能提供 dataset_version_id")
        return self


class ScenarioDatasetBindingOut(ScenarioDatasetBindingCreate):
    id: str
    tenant_id: str
    scenario_id: str
    resolved_dataset_version_id: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ScenarioCapabilityPortCreate(BaseModel):
    capability_kind: Literal["function", "action", "workflow"]
    capability_key: str = Field(min_length=1, max_length=240)
    port_key: str = Field(min_length=1, max_length=180)
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=8_000)
    direction: Literal["input", "output"]
    role: Literal[
        "modeling_evidence",
        "test_fixture",
        "invocation_input",
        "reference",
        "rules",
        "output",
    ]
    media_kind: Literal[
        "message", "structured", "document", "dataset", "connector", "artifact"
    ] = "structured"
    dataset_id: str | None = Field(default=None, max_length=32)
    dataset_schema_id: str | None = Field(default=None, max_length=32)
    schema_document: dict[str, Any] = Field(default_factory=dict)
    is_required: bool = True
    cardinality: Literal["one", "many"] = "one"
    binding_policy: Literal[
        "per_invocation", "scenario_default", "release_pinned", "none"
    ] = "per_invocation"
    status: Literal["draft", "active", "retired"] = "draft"
    config: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def valid_contract(self) -> "ScenarioCapabilityPortCreate":
        if (self.direction == "output") != (self.role == "output"):
            raise ValueError("输出端口必须使用 output 用途，输入端口不能使用 output")
        if bool(self.dataset_id) != bool(self.dataset_schema_id):
            raise ValueError("dataset_id 与 dataset_schema_id 必须同时提供或同时省略")
        if self.binding_policy == "none" and self.is_required:
            raise ValueError("binding_policy=none 的端口不能声明为必填")
        return self


class ScenarioCapabilityPortOut(ScenarioCapabilityPortCreate):
    id: str
    tenant_id: str
    scenario_id: str
    dataset_schema_hash: str = ""
    created_by_user_id: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SemanticFieldMappingCreate(BaseModel):
    ontology_property_id: str = Field(min_length=1, max_length=32)
    dataset_field_id: str = Field(min_length=1, max_length=32)
    direction: Literal["input", "output", "bidirectional"] = "input"
    is_required: bool = False
    transform: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class SemanticMappingCreate(BaseModel):
    scenario_dataset_binding_id: str = Field(min_length=1, max_length=32)
    entity_id: str = Field(min_length=1, max_length=32)
    dataset_schema_id: str = Field(min_length=1, max_length=32)
    dataset_relation_id: str = Field(min_length=1, max_length=32)
    mapping_key: str = Field(min_length=1, max_length=180)
    status: Literal["draft", "active", "error", "retired"] = "draft"
    identifier_strategy: dict[str, Any] = Field(default_factory=dict)
    filter_expression: dict[str, Any] = Field(default_factory=dict)
    fields: list[SemanticFieldMappingCreate] = Field(default_factory=list, max_length=500)

    model_config = {"extra": "forbid"}


class SemanticFieldMappingOut(SemanticFieldMappingCreate):
    id: str
    ordinal: int

    model_config = {"from_attributes": True}


class SemanticMappingOut(BaseModel):
    id: str
    tenant_id: str
    dataset_id: str
    scenario_id: str
    entity_id: str
    scenario_dataset_binding_id: str
    dataset_schema_id: str
    dataset_relation_id: str
    mapping_key: str
    status: str
    identifier_strategy: dict[str, Any] = Field(default_factory=dict)
    filter_expression: dict[str, Any] = Field(default_factory=dict)
    fields: list[SemanticFieldMappingOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
