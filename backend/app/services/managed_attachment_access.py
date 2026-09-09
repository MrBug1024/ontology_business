"""Shared tenant and ownership checks for immutable runtime attachments."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import DataAsset, DataAssetVersion, DatasetVersion, LogicalDataset, ManagedUploadRun
from . import managed_asset_lifecycle, tenant_service


class AttachmentReference(Protocol):
    asset_version_id: str | None
    dataset_version_id: str | None
    expected_signature: str | None


class AttachmentAccessError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def validate_attachments(db: Session, attachments: Sequence[AttachmentReference], *, user_id: str) -> None:
    tenant_id = tenant_service.current_tenant_id(db)
    for attachment in attachments:
        if getattr(attachment, "upload_run_id", None):
            upload = db.scalar(
                select(ManagedUploadRun).where(
                    ManagedUploadRun.id == getattr(attachment, "upload_run_id", None),
                    ManagedUploadRun.tenant_id == tenant_id,
                    ManagedUploadRun.requested_by_user_id == user_id,
                )
            )
            if upload is None or upload.status in {"failed", "cancelled"}:
                raise AttachmentAccessError(
                    "attachment_unavailable",
                    "部分附件不存在、已失效或不能作为正式运行输入",
                    status_code=404,
                )
            if attachment.expected_signature:
                raise AttachmentAccessError(
                    "invalid_attachment_reference",
                    "尚未就绪的上传任务不能声明内容签名",
                    status_code=422,
                )
        elif attachment.asset_version_id:
            version = db.scalar(
                select(DataAssetVersion).where(
                    DataAssetVersion.id == attachment.asset_version_id,
                    DataAssetVersion.tenant_id == tenant_id,
                    DataAssetVersion.status == "ready",
                )
            )
            asset = db.get(DataAsset, version.asset_id) if version is not None else None
            if (
                version is None
                or asset is None
                or asset.tenant_id != tenant_id
                or asset.lifecycle_status != "active"
                or asset.usage_plane == "modeling_material"
            ):
                raise AttachmentAccessError(
                    "attachment_unavailable",
                    "部分附件不存在、已失效或不能作为正式运行输入",
                    status_code=404,
                )
            purpose = str((asset.labels or {}).get("catalog_purpose") or "")
            if purpose == "invocation_attachment" and asset.created_by_user_id != user_id:
                raise AttachmentAccessError(
                    "attachment_unavailable",
                    "部分附件不存在、已失效或不能作为正式运行输入",
                    status_code=404,
                )
            try:
                managed_asset_lifecycle.require_current_asset_version(
                    version.version_document
                )
            except managed_asset_lifecycle.ManagedAssetLifecycleError as exc:
                raise AttachmentAccessError(
                    "attachment_expired"
                    if exc.code == "managed_reference_expired"
                    else "attachment_unavailable",
                    "临时附件已过期，请重新上传"
                    if exc.code == "managed_reference_expired"
                    else "临时附件缺少有效生命周期",
                    status_code=410 if exc.code == "managed_reference_expired" else 409,
                ) from None
            if (
                attachment.expected_signature
                and attachment.expected_signature != version.content_sha256
            ):
                raise AttachmentAccessError("attachment_conflict", "附件版本已变化，请重新选择后提交")
        elif attachment.dataset_version_id:
            version = db.scalar(
                select(DatasetVersion).where(
                    DatasetVersion.id == attachment.dataset_version_id,
                    DatasetVersion.tenant_id == tenant_id,
                    DatasetVersion.status == "ready",
                )
            )
            dataset = db.get(LogicalDataset, version.dataset_id) if version is not None else None
            if (
                version is None
                or dataset is None
                or dataset.tenant_id != tenant_id
                or dataset.lifecycle_status != "active"
                or dataset.usage_plane == "modeling_material"
            ):
                raise AttachmentAccessError(
                    "attachment_unavailable",
                    "部分附件不存在、已失效或不能作为正式运行输入",
                    status_code=404,
                )
            if (
                attachment.expected_signature
                and attachment.expected_signature != version.content_hash
            ):
                raise AttachmentAccessError("attachment_conflict", "附件版本已变化，请重新选择后提交")
