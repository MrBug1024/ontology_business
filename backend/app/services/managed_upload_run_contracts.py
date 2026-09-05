"""Shared contracts for durable managed-upload intake and processing."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..catalog_schemas import CatalogManagedUploadMetadata
from ..models import DataSource


class ManagedUploadError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class ManagedUploadConflict(ManagedUploadError):
    def __init__(self, message: str = "上传任务状态已变化，请刷新后重试") -> None:
        super().__init__("managed_upload_conflict", message, status_code=409)


@dataclass(frozen=True)
class UploadLease:
    token: str
    generation: int
    expires_at: datetime


@dataclass(frozen=True)
class UploadProcessingSnapshot:
    tenant_id: str
    user_id: str
    data_source_id: str
    bucket_file_id: str
    filename: str
    client_media_type: str
    byte_size: int
    content_sha256: str
    created_at: datetime | None
    metadata: CatalogManagedUploadMetadata
    bucket_name: str
    object_key: str
    object_version_id: str


@dataclass(frozen=True)
class ContentUploadSnapshot:
    tenant_id: str
    user_id: str
    data_source_id: str
    filename: str
    client_media_type: str
    source: DataSource


@dataclass(frozen=True)
class ManagedInvocationAttachment:
    """Owner-authorized, storage-free projection consumed by Assistant retrieval."""

    id: str
    filename: str
    mime: str
    size: int
    status: str
    content_hash: str
    parsed_text: str
    error: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def lease_matches(run: object, lease: UploadLease, *, as_of: datetime) -> bool:
    expires_at = as_utc(getattr(run, "lease_expires_at", None))
    return bool(
        getattr(run, "lease_token", "") == lease.token
        and getattr(run, "lease_generation", 0) == lease.generation
        and expires_at is not None
        and expires_at > as_of
    )
