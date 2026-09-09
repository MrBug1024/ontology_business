"""Bounded commands and projections for manual scenario releases."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ScenarioReleaseCreate(BaseModel):
    scenario_id: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=160)
    notes: str = Field(default="", max_length=8000)
    confirmed: bool = False
    model_config = ConfigDict(extra="forbid")


class ScenarioReleaseChange(BaseModel):
    action: Literal["enable", "disable", "retire", "delete"]
    expected_revision: int = Field(ge=1)
    model_config = ConfigDict(extra="forbid")


class ScenarioReleaseOut(BaseModel):
    id: str
    scenario_id: str
    scenario_name: str
    name: str
    notes: str
    enabled: bool
    status: str
    revision: int
    created_at: datetime
    created_by_user_id: str | None
    created_by_name: str
    retired_at: datetime | None
    deleted_at: datetime | None
    can_manage: bool


class ScenarioReleasePage(BaseModel):
    items: list[ScenarioReleaseOut]
    limit: int
    offset: int
    has_more: bool
