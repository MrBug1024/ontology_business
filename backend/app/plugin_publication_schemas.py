"""Closed contracts for deliberate plugin publication and installation."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PluginPublicationChange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    artifact_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    expected_revision: int = Field(ge=0, le=1_000_000_000)
    action: Literal['publish', 'withdraw']
    confirmed_publication: bool = False

    @model_validator(mode='after')
    def publication_is_deliberate(self):
        if self.action == 'publish' and not self.confirmed_publication:
            raise ValueError('请确认允许第三方下载此插件代码与能力契约')
        return self


class PluginInstallationOut(BaseModel):
    model_config = ConfigDict(extra='forbid')
    host: Literal['claude_code', 'codex'] = 'claude_code'
    scope: Literal['local_test', 'public']
    package_name: str = Field(max_length=160)
    marketplace_name: str = Field(max_length=180)
    plugin_version: str = Field(max_length=40)
    marketplace_url: str = Field(max_length=2400)
    marketplace_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    installer_url: str = Field(max_length=2400)
    installer_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    powershell_command: str = Field(max_length=10000)
    bash_command: str = Field(max_length=10000)
    usage_command: str = Field(max_length=240)
    requirements: list[str] = Field(max_length=8)
    configuration_notes: list[str] = Field(max_length=8)


class PluginPublicationOut(BaseModel):
    model_config = ConfigDict(extra='forbid')
    publication_id: str
    artifact_id: str
    revision: int = Field(ge=0)
    status: Literal['unpublished', 'published', 'withdrawn']
    published_at: datetime | None = None
    configuration_ready: bool
    available: bool
    unavailable_reason: str
    installation: PluginInstallationOut | None = None
