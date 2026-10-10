"""DTOs for member-machine investigation connector sessions."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from .distillation_schemas import ClosedModel


class ConnectorSessionCreate(ClosedModel):
    target_key: Annotated[str, Field(max_length=64)]


class ConnectorCommandOut(ClosedModel):
    """The plaintext token appears exactly once, in this response."""

    id: str
    target_key: str
    status: Literal["pending", "connected", "revoked", "expired"]
    token_prefix: str
    connector_platform: Annotated[str, Field(max_length=200)] = ""
    command: str
    script_url: str
    created_at: datetime
    expires_at: datetime
    connected_at: datetime | None = None
    last_seen_at: datetime | None = None
    revoked_reason: Annotated[str, Field(max_length=200)] = ""


class ConnectorSessionOut(ClosedModel):
    id: str
    target_key: str
    status: Literal["pending", "connected", "revoked", "expired"]
    token_prefix: str
    connector_platform: Annotated[str, Field(max_length=200)] = ""
    created_at: datetime
    expires_at: datetime
    connected_at: datetime | None = None
    last_seen_at: datetime | None = None
    revoked_reason: Annotated[str, Field(max_length=200)] = ""


class ConnectorSessionPage(ClosedModel):
    sessions: list[ConnectorSessionOut] = Field(default_factory=list, max_length=8)
