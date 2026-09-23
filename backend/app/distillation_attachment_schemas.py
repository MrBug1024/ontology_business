"""Temporary conversation inputs, kept separate from library assets."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from .distillation_schemas import ClosedModel, ResourceId


class AttachmentOut(ClosedModel):
    id: ResourceId
    project_id: ResourceId | None
    request_id: Annotated[str, Field(min_length=1, max_length=64)]
    filename: Annotated[str, Field(max_length=255)]
    media_type: Annotated[str, Field(max_length=150)]
    byte_size: int = Field(ge=1, le=10 * 1024 * 1024)
    content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    status: Literal["ready", "bound", "removed", "expired"]
    created_at: datetime
    expires_at: datetime
