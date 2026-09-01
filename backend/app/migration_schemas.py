"""Public control contracts for platform and Agent migrations.

Shadow comparison values are intentionally absent.  They are produced by
server-owned validation executors and only their safe ledger summaries are
returned by these APIs.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MigrationReasonIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2_000)

    model_config = {"extra": "forbid"}


class MigrationBatchIn(BaseModel):
    batch_size: int = Field(default=100, ge=1, le=500)

    model_config = {"extra": "forbid"}


class AgentModeChangeIn(BaseModel):
    target_mode: Literal["capability_only"]
    reason: str = Field(min_length=1, max_length=2_000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=180)

    model_config = {"extra": "forbid"}


__all__ = [
    "AgentModeChangeIn",
    "MigrationBatchIn",
    "MigrationReasonIn",
]
