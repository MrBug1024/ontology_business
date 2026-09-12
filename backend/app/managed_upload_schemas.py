"""Public, credential-free contracts for durable managed uploads."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .catalog_schemas import CatalogManagedUploadOut


ManagedUploadPurpose = Literal["validation_asset", "invocation_attachment"]
ManagedUploadStatus = Literal[
    "awaiting_upload",
    "uploading",
    "stored",
    "processing",
    "ready",
    "failed",
    "cancelled",
]


class ManagedUploadCreateIn(BaseModel):
    filename: str = Field(min_length=1, max_length=500)
    byte_size: int = Field(gt=0)
    media_type: str = Field(default="", max_length=200)
    purpose: ManagedUploadPurpose = "validation_asset"
    idempotency_key: str = Field(min_length=1, max_length=180)
    name: str | None = Field(default=None, min_length=1, max_length=300)
    description: str = Field(default="", max_length=8_000)
    labels: dict[str, Any] = Field(default_factory=dict)
    expires_in_seconds: int | None = Field(
        default=None,
        ge=300,
        le=7 * 24 * 60 * 60,
    )
    # Optional Agent scope.  Global Assistant uploads continue to omit both
    # fields; Agent routes must provide and authorize the scope server-side.
    agent_id: str | None = Field(default=None, min_length=1, max_length=32)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=32)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def lifecycle_is_explicit(self) -> "ManagedUploadCreateIn":
        if self.purpose != "invocation_attachment" and self.expires_in_seconds is not None:
            raise ValueError("长期验证资料不能设置临时到期时间")
        if self.conversation_id and not self.agent_id:
            raise ValueError("conversation_id 必须与 agent_id 一起提供")
        return self


class ManagedUploadErrorOut(BaseModel):
    code: str
    message: str

    model_config = {"extra": "forbid"}


class ManagedUploadRunOut(BaseModel):
    id: str
    parent_run_id: str | None = None
    purpose: ManagedUploadPurpose
    filename: str
    declared_byte_size: int
    byte_size: int
    status: ManagedUploadStatus
    revision: int = Field(ge=1)
    asset_id: str | None = None
    asset_version_id: str | None = None
    content_sha256: str = ""
    error: ManagedUploadErrorOut | None = None
    result: CatalogManagedUploadOut | None = None
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    owner_agent_id: str | None = None

    model_config = {"extra": "forbid"}


class ManagedUploadRetryIn(BaseModel):
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=180)
    # Agent-scoped callers must echo the owner on every state transition.
    # Global Assistant retries intentionally omit this field and therefore
    # resolve only runs whose owner_agent_id is NULL.
    agent_id: str | None = Field(default=None, min_length=1, max_length=32)

    model_config = {"extra": "forbid"}


class ManagedUploadCancelIn(BaseModel):
    expected_revision: int = Field(ge=1)
    # See ManagedUploadRetryIn.agent_id.
    agent_id: str | None = Field(default=None, min_length=1, max_length=32)

    model_config = {"extra": "forbid"}
