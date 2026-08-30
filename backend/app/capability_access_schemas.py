"""Safe first-party contracts for capability publication and adapter setup."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class CapabilityAccessScenarioOut(BaseModel):
    id: str
    name: str


class CapabilityAccessDeploymentOut(BaseModel):
    environment: Literal["dev", "staging", "prod"]
    definition_source: Literal["live", "release"]
    release_id: str | None = None
    snapshot_id: str | None = None
    definition_hash: str


class CapabilityAccessPortOut(BaseModel):
    key: str
    name: str
    direction: Literal["input", "output"]
    role: str
    media_kind: str
    schema_hash: str
    required: bool
    cardinality: str
    binding_policy: str


class CapabilityAccessCapabilityOut(BaseModel):
    kind: Literal["function", "action", "workflow"]
    key: str
    name: str
    input_schema_hash: str
    output_schema_hash: str
    side_effect: bool
    requires_confirmation: bool
    idempotency_required: bool
    ready: bool
    blocking_codes: list[str] = Field(default_factory=list)
    data_ports: list[CapabilityAccessPortOut] = Field(default_factory=list)


class CapabilityAccessAdapterOut(BaseModel):
    protocol: Literal["rest", "mcp"]
    endpoint: str
    discovery: str | None = None
    invocation: str | None = None
    receipt: str | None = None
    managed_input_upload: str | None = None
    authentication: dict[str, str]
    required_scopes: list[str]
    optional_scopes: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)


class CapabilityAccessReleaseOut(BaseModel):
    id: str
    snapshot_id: str
    environment: Literal["dev", "staging", "prod"]
    status: str
    created_at: datetime


class CapabilityAccessCheckOut(BaseModel):
    code: str
    passed: bool
    count: int | None = None


class CapabilityAccessManifestOut(BaseModel):
    manifest_version: Literal["capability-access-manifest/v1"]
    manifest_id: str
    scenario: CapabilityAccessScenarioOut
    deployment: CapabilityAccessDeploymentOut
    capabilities: list[CapabilityAccessCapabilityOut]
    adapters: list[CapabilityAccessAdapterOut]
    release_history: list[CapabilityAccessReleaseOut]
    checks: list[CapabilityAccessCheckOut]
