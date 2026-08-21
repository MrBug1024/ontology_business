"""Schemas intentionally isolated from the first-party UI API contract."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


ExternalApiScope = Literal["scenarios:read", "objects:read"]


class ExternalApiKeyCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    # An owner may issue a constrained integration credential for another active
    # member; the external caller still executes with that member's live RBAC
    # and ACL policy.
    user_id: str | None = Field(default=None, min_length=1, max_length=32)
    scopes: list[ExternalApiScope] = Field(min_length=1, max_length=2)
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
