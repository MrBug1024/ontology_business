"""Schemas intentionally isolated from the first-party UI API contract."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


ExternalApiScope = Literal[
    "scenarios:read",
    "objects:read",
    "capabilities:read",
    "capabilities:invoke",
    "assets:write",
]


class ExternalApiKeyCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    # An owner may issue a constrained integration credential for another active
    # member; the external caller still executes with that member's live RBAC
    # and ACL policy.
    user_id: str | None = Field(default=None, min_length=1, max_length=32)
    scopes: list[ExternalApiScope] = Field(min_length=1, max_length=5)
    expires_in_days: int = Field(default=90, ge=1, le=365)

    @field_validator("scopes")
    @classmethod
    def unique_scopes(cls, values: list[ExternalApiScope]) -> list[ExternalApiScope]:
        if len(set(values)) != len(values):
            raise ValueError("scopes 不能重复")
        return values


class ExternalApiKeyOut(BaseModel):
    id: str
    tenant_id: str
    user_id: str
    issued_by_user_id: str | None = None
    revoked_by_user_id: str | None = None
    name: str
    key_prefix: str
    token_hint: str
    scopes: list[ExternalApiScope]
    status: Literal["active", "revoked"]
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime


class ExternalApiKeyCreatedOut(ExternalApiKeyOut):
    # This field exists only in the create response.  It is never persisted or
    # returned by list/revoke endpoints.
    token: str = Field(repr=False)


class ExternalApiIdentityOut(BaseModel):
    api_version: Literal["v1"] = "v1"
    key_id: str
    tenant_id: str
    user_id: str
    scopes: list[ExternalApiScope]
    expires_at: datetime | None = None


class ExternalScenarioOut(BaseModel):
    id: str
    name: str
    description: str = ""
    industry: str = ""
    status: str
    created_at: datetime
    updated_at: datetime


class ExternalCapabilityScenarioOut(BaseModel):
    """Minimal logical bootstrap record for capability clients."""

    id: str
    name: str
    description: str = ""
    industry: str = ""


class ExternalPropertyOut(BaseModel):
    name: str
    data_type: str
    description: str = ""
    is_key: bool = False
    is_required: bool = False
    is_enum: bool = False
    enum_values: list[str] = Field(default_factory=list)


class ExternalEntityOut(BaseModel):
    id: str
    scenario_id: str
    name: str
    description: str = ""
    properties: list[ExternalPropertyOut] = Field(default_factory=list)


class ExternalObjectOut(BaseModel):
    id: str
    scenario_id: str
    entity_id: str
    entity_name: str = ""
    name: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ExternalObjectPageOut(BaseModel):
    items: list[ExternalObjectOut] = Field(default_factory=list)
    total: int
    limit: int
    offset: int
    query: str = ""
    entity_id: str | None = None
    # ``total`` remains for v1 compatibility.  On a large candidate set ACL
    # filtering is intentionally bounded, so callers must use ``has_more`` for
    # progress and only rely on total when it is marked exact.
    total_is_exact: bool = True
    has_more: bool = False


ExternalCapabilityKind = Literal["function", "action", "workflow"]
ExternalCapabilityEnvironment = Literal["dev", "staging", "prod"]


class ExternalCapabilityPortOut(BaseModel):
    key: str
    name: str
    description: str = ""
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
    ]
    schema_document: dict[str, Any] = Field(
        default_factory=dict,
        serialization_alias="schema",
        validation_alias="schema",
    )
    schema_hash: str = ""
    required: bool = True
    cardinality: Literal["one", "many"] = "one"
    binding_policy: Literal[
        "per_invocation", "scenario_default", "release_pinned", "none"
    ] = "per_invocation"
    binding_kinds: list[Literal[
        "dataset_version", "dataset_head", "asset_version", "connector_binding"
    ]] = Field(default_factory=list)
    allow_override: bool = False

    model_config = {"populate_by_name": True}


class ExternalCapabilityReadinessOut(BaseModel):
    ready: bool
    issues: list[dict[str, Any]] = Field(default_factory=list)


class ExternalCapabilityOut(BaseModel):
    scenario_id: str
    environment: ExternalCapabilityEnvironment
    kind: ExternalCapabilityKind
    key: str
    name: str
    description: str = ""
    definition_hash: str
    deployment_fingerprint: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    side_effect: bool = False
    requires_confirmation: bool = False
    idempotency_required: bool = False
    data_ports: list[ExternalCapabilityPortOut] = Field(default_factory=list)
    readiness: ExternalCapabilityReadinessOut


class ExternalManagedInputIn(BaseModel):
    """One governed invocation reference; never a physical connection override."""

    port_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    dataset_version_id: str | None = Field(default=None, min_length=1, max_length=32)
    dataset_head_id: str | None = Field(default=None, min_length=1, max_length=32)
    asset_version_id: str | None = Field(default=None, min_length=1, max_length=32)
    artifact_id: str | None = Field(default=None, min_length=1, max_length=32)
    binding_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=180,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    expected_signature: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def exactly_one_reference(self) -> "ExternalManagedInputIn":
        references = (
            self.dataset_version_id,
            self.dataset_head_id,
            self.asset_version_id,
            self.artifact_id,
            self.binding_key,
        )
        if sum(value is not None for value in references) != 1:
            raise ValueError("每个端口必须且只能提供一个受管引用")
        return self

    def runtime_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {"port_key": self.port_key}
        for field in (
            "dataset_version_id",
            "dataset_head_id",
            "asset_version_id",
            "artifact_id",
            "binding_key",
        ):
            value = getattr(self, field)
            if value is not None:
                document[field] = value
        if self.expected_signature is not None:
            document["signature"] = self.expected_signature
        return document


class ExternalManagedInputOptionOut(BaseModel):
    """One invocation-ready logical choice; physical source data is impossible."""

    binding_kind: Literal[
        "dataset_version", "dataset_head", "asset_version", "connector_binding"
    ]
    label: str
    managed_input: ExternalManagedInputIn
    version_number: int | None = None
    environment: ExternalCapabilityEnvironment | None = None
    connector_kind: Literal["data_source", "mcp", "llm"] | None = None
    updated_at: datetime | None = None


class ExternalManagedInputOptionsOut(BaseModel):
    scenario_id: str
    environment: ExternalCapabilityEnvironment
    kind: ExternalCapabilityKind
    key: str
    port_key: str
    definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    deployment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_kinds: list[Literal[
        "dataset_version", "dataset_head", "asset_version", "connector_binding"
    ]] = Field(default_factory=list)
    allow_override: bool
    items: list[ExternalManagedInputOptionOut] = Field(default_factory=list)
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    offset: int = Field(ge=0)
    has_more: bool = False


class ExternalConfirmationIn(BaseModel):
    preview_invocation_id: str = Field(min_length=1, max_length=32)
    confirmation_token: str = Field(min_length=1, max_length=512)
    required: bool = True
    expires_at: datetime | None = None

    model_config = {"extra": "forbid"}


class ExternalCapabilityInvocationIn(BaseModel):
    environment: ExternalCapabilityEnvironment = "prod"
    mode: Literal["execute", "preview", "confirm"] = "execute"
    inputs: dict[str, Any] = Field(default_factory=dict)
    managed_inputs: list[ExternalManagedInputIn] = Field(
        default_factory=list,
        max_length=100,
    )
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=180)
    correlation_id: str | None = Field(default=None, min_length=1, max_length=240)
    request_id: str | None = Field(default=None, min_length=1, max_length=64)
    expected_definition_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    expected_deployment_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    confirmation: ExternalConfirmationIn | None = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def confirmation_matches_mode(self) -> "ExternalCapabilityInvocationIn":
        if self.mode == "confirm" and self.confirmation is None:
            raise ValueError("confirm 模式必须提供服务端签发的确认票据")
        if self.mode != "confirm" and self.confirmation is not None:
            raise ValueError("只有 confirm 模式可以提交确认票据")
        keys = [item.port_key.casefold() for item in self.managed_inputs]
        if len(keys) != len(set(keys)):
            raise ValueError("同一端口不能重复提交受管输入")
        return self


class ExternalCapabilityReceiptOut(BaseModel):
    invocation_id: str
    status: Literal[
        "pending",
        "running",
        "awaiting_confirmation",
        "succeeded",
        "failed",
        "cancelled",
        "rejected",
        "timed_out",
    ]
    capability: dict[str, Any]
    definition_hash: str
    deployment_fingerprint: str
    data_context_fingerprint: str
    output: Any = Field(default_factory=dict)
    audit_ref: dict[str, Any] = Field(default_factory=dict)
    confirmation: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
