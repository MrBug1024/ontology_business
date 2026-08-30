"""Public control contracts for platform and Agent migrations.

Shadow comparison values are intentionally absent.  They are produced by
server-owned validation executors and only their safe ledger summaries are
returned by these APIs.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class MigrationReasonIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2_000)

    model_config = {"extra": "forbid"}


class MigrationBatchIn(BaseModel):
    batch_size: int = Field(default=100, ge=1, le=500)

    model_config = {"extra": "forbid"}


class AgentModeChangeIn(BaseModel):
    target_mode: Literal["shadow", "prefer_capability", "capability_only"]
    reason: str = Field(min_length=1, max_length=2_000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=180)

    model_config = {"extra": "forbid"}


class AgentRollbackIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2_000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=180)

    model_config = {"extra": "forbid"}


class AgentShadowManagedInputIn(BaseModel):
    """One governed selector used by the persisted shadow turn."""

    port_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
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
    def exactly_one_reference(self) -> "AgentShadowManagedInputIn":
        references = (
            self.dataset_version_id,
            self.dataset_head_id,
            self.asset_version_id,
            self.artifact_id,
            self.binding_key,
        )
        if sum(value is not None for value in references) != 1:
            raise ValueError("each managed input requires exactly one governed reference")
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
            document["expected_signature"] = self.expected_signature
        return document


class AgentShadowValidationIn(BaseModel):
    """References and original inputs for a server-executed shadow comparison.

    Comparison metrics are deliberately absent: only the platform executor may
    derive and persist them.
    """

    source_message_id: str = Field(min_length=1, max_length=32)
    legacy_tool_result_id: str = Field(min_length=1, max_length=240)
    capability_kind: Literal["function", "action", "workflow"]
    capability_key: str = Field(min_length=1, max_length=240)
    inputs: dict[str, Any] = Field(default_factory=dict)
    managed_inputs: list[AgentShadowManagedInputIn] = Field(
        default_factory=list,
        max_length=100,
    )

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def unique_managed_ports(self) -> "AgentShadowValidationIn":
        keys = [item.port_key.casefold() for item in self.managed_inputs]
        if len(keys) != len(set(keys)):
            raise ValueError("a managed input port may be supplied only once")
        return self


__all__ = [
    "AgentModeChangeIn",
    "AgentRollbackIn",
    "AgentShadowManagedInputIn",
    "AgentShadowValidationIn",
    "MigrationBatchIn",
    "MigrationReasonIn",
]
