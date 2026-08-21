"""Pydantic contracts for immutable, code-versioned starter-kit assets."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StarterKitSummary(BaseModel):
    """Safe catalog metadata for a governed ontology starter kit.

    The fingerprint is recomputed by the server from the package asset before an
    instance of this model is returned.  It is therefore an integrity statement,
    not client-provided metadata.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=200)
    industry: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=2000)
    fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    resource_counts: dict[str, int]

    @field_validator("resource_counts")
    @classmethod
    def validate_resource_counts(cls, value: dict[str, int]) -> dict[str, int]:
        if not value:
            raise ValueError("resource_counts 不能为空")
        if any(isinstance(count, bool) or count < 0 for count in value.values()):
            raise ValueError("resource_counts 必须是非负整数")
        return dict(value)


class StarterKitArtifact(StarterKitSummary):
    """Verified catalog metadata plus its portable resource package."""

    package: dict[str, Any]

