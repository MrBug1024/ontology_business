"""Management contracts for publishing configured Agents as MCP services."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class AgentMCPServiceCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    agent_id: str = Field(min_length=1, max_length=32)
    expires_in_days: int = Field(default=365, ge=1, le=3650)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("MCP 服务名称不能为空")
        return value


class AgentMCPServiceUpdateIn(BaseModel):
    enabled: bool


class AgentMCPTokenRotateIn(BaseModel):
    expires_in_days: int = Field(default=365, ge=1, le=3650)


class AgentMCPCandidateOut(BaseModel):
    id: str
    name: str
    scenario_name: str = ""
    ready: bool
    missing: list[str] = Field(default_factory=list)


class AgentMCPServiceOut(BaseModel):
    id: str
    name: str
    agent_id: str
    agent_name: str
    scenario_name: str = ""
    enabled: bool
    ready: bool
    stale: bool
    missing: list[str] = Field(default_factory=list)
    endpoint_url: str
    key_prefix: str
    token_hint: str
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    runtime_environment: str
    definition_hash: str
    created_at: datetime
    updated_at: datetime


class AgentMCPServiceCreatedOut(AgentMCPServiceOut):
    token: str = Field(repr=False)
    config: dict
    config_json: str


class AgentMCPServiceTestOut(BaseModel):
    ok: bool
    message: str
    tool_name: str = "invoke_agent"
    agent_name: str
    runtime_environment: str
    definition_hash: str
