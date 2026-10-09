"""Explicit business acceptance and immutable package build requests."""
from __future__ import annotations

from typing import Literal
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, model_validator


class PackageCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["function", "action", "rule", "workflow"]
    key: str = Field(min_length=1, max_length=240)


class PackageAcceptanceCase(PackageCapability):
    role: Literal["success", "boundary", "failure"]
    invocation_id: str = Field(min_length=1, max_length=32)
    expected_status: Literal["succeeded", "failed", "rejected"] = "succeeded"


class ScenarioPackageBuild(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_revision: int = Field(ge=1)
    target: Literal["claude_code", "codex"] = "claude_code"
    capabilities: list[PackageCapability] = Field(min_length=1, max_length=20)
    acceptance_cases: list[PackageAcceptanceCase] = Field(min_length=3, max_length=60)
    confirmed_business_acceptance: bool = False

    @model_validator(mode="after")
    def unique_selection(self):
        identities = {(item.kind, item.key) for item in self.capabilities}
        if len(identities) != len(self.capabilities):
            raise ValueError("不能重复选择能力")
        cases = {(item.kind, item.key, item.role) for item in self.acceptance_cases}
        if len(cases) != len(self.acceptance_cases):
            raise ValueError("每项能力的每类案例只能提交一条")
        if any((item.kind, item.key) not in identities for item in self.acceptance_cases):
            raise ValueError("验收案例必须属于本次选择的能力")
        if len({item.invocation_id for item in self.acceptance_cases}) != len(self.acceptance_cases):
            raise ValueError("不同案例必须提供不同执行回执")
        if any({role for kind, key, role in cases if (kind, key) == identity}
               != {"success", "boundary", "failure"} for identity in identities):
            raise ValueError("每项能力必须提供成功、边界、失败三类案例")
        return self


class PackageEvidence(PackageCapability):
    invocation_id: str = Field(min_length=1, max_length=32)
    status: Literal["succeeded", "failed", "rejected"]
    created_at: datetime
    workflow_status: str | None = Field(default=None, max_length=32)
    eligible: bool
