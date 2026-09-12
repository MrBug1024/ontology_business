"""Shared tenant and ownership checks for immutable runtime attachments."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    BucketFile,
    DataAsset,
    DataAssetVersion,
    DataSource,
    DatasetVersion,
    DatasetVersionAsset,
    LogicalDataset,
    ManagedUploadRun,
)
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


# These purposes are created by the Agent upload boundary and therefore carry
# an immutable, exact Agent owner.  ``NULL`` remains the explicit Global
# Assistant namespace for legacy/shared catalog rows and global uploads.
AGENT_PRIVATE_PURPOSES = frozenset({"validation_asset", "invocation_attachment"})


def _normalise_purpose(value: object) -> str:
    return str(value or "").strip().lower()


def _asset_purpose(asset: DataAsset | None) -> str:
    labels = getattr(asset, "labels", None)
    if not isinstance(labels, Mapping):
        return ""
    return _normalise_purpose(labels.get("catalog_purpose"))


def _check_agent_owner(
    owner_agent_id: str | None,
    *,
    agent_id: str | None,
    purpose: str | None = None,
) -> None:
    """Check the owner boundary for one asset or upload reference.

    For an Agent request, an owned row must match that exact Agent.  An
    ownerless row is allowed only when it is not marked with one of the two
    Agent-private upload purposes; this is the narrow compatibility path for
    tenant-shared governed assets.  Without an Agent, ``NULL`` is the explicit
    Global Assistant namespace and remains valid for global uploads/evidence.
    """
    owner = str(owner_agent_id or "").strip() or None
    private = _normalise_purpose(purpose) in AGENT_PRIVATE_PURPOSES
    if agent_id:
        expected = str(agent_id or "").strip() or None
        if owner is not None and owner != expected:
            raise AttachmentAccessError(
                "attachment_unavailable",
                "部分附件不存在、已失效或不能作为当前 Agent 的输入",
                status_code=404,
            )
        if owner is None and private:
            raise AttachmentAccessError(
                "attachment_unavailable",
                "部分附件不存在、已失效或不能作为当前 Agent 的输入",
                status_code=404,
            )
    elif owner is not None:
        raise AttachmentAccessError(
            "attachment_unavailable",
            "部分附件不存在、已失效或不能作为当前会话的输入",
            status_code=404,
        )


def require_asset_scope(
    asset: DataAsset | None,
    *,
    agent_id: str | None,
    purpose: str | None = None,
) -> None:
    """Apply the shared/Agent-private asset rule to a loaded catalog asset.

    Lifecycle, tenant and version readiness checks stay with each caller;
    this helper only decides whether the ownership namespace is usable.  It
    intentionally does not inspect physical storage, so an ownerless,
    non-private governed asset remains compatible regardless of its lineage.
    """

    if asset is None:
        raise AttachmentAccessError(
            "attachment_unavailable",
            "部分附件不存在、已失效或不能作为当前 Agent 的输入"
            if agent_id
            else "部分附件不存在、已失效或不能作为当前会话的输入",
            status_code=404,
        )
    # A few legacy versions recorded the upload purpose only in their
    # immutable version document.  Callers may provide that value so such a
    # row cannot be reinterpreted as a shared asset merely because its labels
    # are incomplete.
    asset_purpose = _asset_purpose(asset)
    supplied_purpose = _normalise_purpose(purpose)
    effective_purpose = (
        asset_purpose
        if asset_purpose in AGENT_PRIVATE_PURPOSES
        else supplied_purpose
    )
    _check_agent_owner(
        getattr(asset, "owner_agent_id", None),
        agent_id=agent_id,
        purpose=effective_purpose,
    )


def _version_purpose(version: DataAssetVersion) -> str | None:
    document = (
        version.version_document
        if isinstance(version.version_document, Mapping)
        else {}
    )
    lifecycle = document.get("lifecycle")
    return (
        _normalise_purpose(lifecycle.get("purpose"))
        if isinstance(lifecycle, Mapping)
        else None
    )


def _asset_version_purpose(
    asset: DataAsset | None,
    version: DataAssetVersion,
) -> str:
    asset_purpose = _asset_purpose(asset)
    return (
        asset_purpose
        if asset_purpose in AGENT_PRIVATE_PURPOSES
        else _normalise_purpose(_version_purpose(version))
    )


def require_asset_version_scope(
    db: Session,
    asset: DataAsset | None,
    version: DataAssetVersion,
    *,
    tenant_id: str,
    agent_id: str | None,
) -> None:
    """Validate both logical ownership and an optional physical-file lineage."""

    require_asset_scope(
        asset,
        agent_id=agent_id,
        purpose=_asset_version_purpose(asset, version),
    )
    file_id = str(version.bucket_file_id or "").strip() or None
    source_id = str(version.bucket_data_source_id or "").strip() or None
    if file_id is None and source_id is None:
        # Some governed inputs are virtual/legacy immutable documents with no
        # physical file. Their ownership is fully represented by DataAsset.
        return
    if file_id is None or source_id is None or asset is None:
        raise AttachmentAccessError(
            "attachment_unavailable",
            "部分附件不存在、已失效或不能作为当前 Agent 的输入"
            if agent_id
            else "部分附件不存在、已失效或不能作为当前会话的输入",
            status_code=404,
        )
    row = db.execute(
        select(BucketFile, DataSource)
        .join(DataSource, DataSource.id == BucketFile.data_source_id)
        .where(
            BucketFile.id == file_id,
            BucketFile.data_source_id == source_id,
            DataSource.id == source_id,
            DataSource.tenant_id == tenant_id,
        )
    ).first()
    if row is None:
        raise AttachmentAccessError(
            "attachment_unavailable",
            "部分附件不存在、已失效或不能作为当前 Agent 的输入"
            if agent_id
            else "部分附件不存在、已失效或不能作为当前会话的输入",
            status_code=404,
        )
    bucket_file, source = row
    # Local import keeps the ownership primitive reusable without introducing
    # a module-initialization cycle between catalog and runtime services.
    from . import catalog_service

    try:
        catalog_service._require_asset_file_scope(
            db,
            asset,
            bucket_file,
            source,
            allow_shared_runtime_source=True,
            permission_verb="read",
            tenant_id=tenant_id,
        )
    except catalog_service.CatalogError as exc:
        raise AttachmentAccessError(
            "attachment_unavailable",
            "部分附件不存在、已失效或不能作为当前 Agent 的输入"
            if agent_id
            else "部分附件不存在、已失效或不能作为当前会话的输入",
            status_code=404,
        ) from exc


def _check_dataset_scope(
    db: Session,
    dataset: LogicalDataset,
    version: DatasetVersion,
    *,
    tenant_id: str,
    agent_id: str | None,
) -> None:
    """Validate generated dataset ownership through its immutable lineage."""
    labels = dataset.labels if isinstance(dataset.labels, dict) else {}
    # Only generated validation packages carry Agent-private lineage. Other
    # governed datasets keep their existing tenant-level access semantics.
    if labels.get("catalog_purpose") != "validation_dataset":
        return
    if (
        str(getattr(dataset, "lifecycle_status", "") or "").lower() != "active"
        or labels.get("lifecycle") == "agent_deleted"
    ):
        raise AttachmentAccessError(
            "attachment_unavailable",
            "部分附件不存在、已失效或不能作为当前 Agent 的输入",
            status_code=404,
        )
    owner = str(labels.get("owner_agent_id") or "") or None
    expected_agent_id = str(agent_id or "") or None
    if owner != expected_agent_id:
        raise AttachmentAccessError(
            "attachment_unavailable",
            "部分附件不存在、已失效或不能作为当前 Agent 的输入",
            status_code=404,
        )
    # The label is only a compatibility marker.  Reconcile it with every
    # immutable link before allowing a generated package to cross an Agent
    # boundary (including malformed ownerless/global rows).
    from . import catalog_service

    if not catalog_service._validation_dataset_lineage_is_consistent(
        db,
        dataset,
        owner_agent_id=owner,
    ):
        raise AttachmentAccessError(
            "attachment_unavailable",
            "部分附件不存在、已失效或不能作为当前 Agent 的输入",
            status_code=404,
        )
    links = list(
        db.execute(
            select(DataAsset, DataAssetVersion)
            .select_from(DatasetVersionAsset)
            .join(DataAssetVersion, DataAssetVersion.id == DatasetVersionAsset.asset_version_id)
            .join(DataAsset, DataAsset.id == DataAssetVersion.asset_id)
            .where(
                DatasetVersionAsset.dataset_version_id == version.id,
                DatasetVersionAsset.dataset_id == dataset.id,
                DatasetVersionAsset.tenant_id == tenant_id,
                DataAssetVersion.tenant_id == tenant_id,
                DataAsset.tenant_id == tenant_id,
            )
        ).all()
    )
    if not links or any(
        (str(asset.owner_agent_id or "") or None) != expected_agent_id
        for asset, _version in links
    ):
        raise AttachmentAccessError(
            "attachment_unavailable",
            "部分附件不存在、已失效或不能作为当前 Agent 的输入",
            status_code=404,
        )
    for asset, asset_version in links:
        try:
            require_asset_version_scope(
                db,
                asset,
                asset_version,
                tenant_id=tenant_id,
                agent_id=agent_id,
            )
        except AttachmentAccessError:
            raise


def validate_attachments(
    db: Session,
    attachments: Sequence[AttachmentReference],
    *,
    user_id: str,
    agent_id: str | None = None,
) -> None:
    tenant_id = tenant_service.current_tenant_id(db)
    for attachment in attachments:
        if getattr(attachment, "upload_run_id", None):
            statement = select(ManagedUploadRun).where(
                ManagedUploadRun.id == getattr(attachment, "upload_run_id", None),
                ManagedUploadRun.tenant_id == tenant_id,
                ManagedUploadRun.requested_by_user_id == user_id,
            )
            if agent_id:
                statement = statement.where(
                    ManagedUploadRun.owner_agent_id == agent_id
                ).with_for_update()
            else:
                statement = statement.where(ManagedUploadRun.owner_agent_id.is_(None))
            upload = db.scalar(statement)
            if upload is None or upload.status in {"failed", "cancelled"}:
                raise AttachmentAccessError(
                    "attachment_unavailable",
                    "部分附件不存在、已失效或不能作为正式运行输入",
                    status_code=404,
                )
            _check_agent_owner(
                upload.owner_agent_id,
                agent_id=agent_id,
                purpose=upload.purpose,
            )
            if attachment.expected_signature:
                raise AttachmentAccessError(
                    "invalid_attachment_reference",
                    "尚未就绪的上传任务不能声明内容签名",
                    status_code=422,
                )
        elif attachment.asset_version_id:
            version_statement = select(DataAssetVersion).where(
                DataAssetVersion.id == attachment.asset_version_id,
                DataAssetVersion.tenant_id == tenant_id,
                DataAssetVersion.status == "ready",
            )
            version = db.scalar(version_statement)
            asset = None
            if version is not None:
                asset_statement = select(DataAsset).where(
                    DataAsset.id == version.asset_id,
                    DataAsset.tenant_id == tenant_id,
                ).execution_options(populate_existing=True)
                if agent_id:
                    asset_statement = asset_statement.with_for_update()
                asset = db.scalar(asset_statement)
                if agent_id and asset is not None:
                    # Agent deletion and upload publication lock the parent
                    # asset before its immutable versions.  Re-read the
                    # version after that parent fence so this path cannot
                    # retain the inverse Version -> Asset lock order.
                    version = db.scalar(
                        select(DataAssetVersion)
                        .where(
                            DataAssetVersion.id == attachment.asset_version_id,
                            DataAssetVersion.asset_id == asset.id,
                            DataAssetVersion.tenant_id == tenant_id,
                            DataAssetVersion.status == "ready",
                        )
                        .execution_options(populate_existing=True)
                        .with_for_update()
                    )
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
            purpose = _asset_version_purpose(asset, version)
            require_asset_version_scope(
                db,
                asset,
                version,
                tenant_id=tenant_id,
                agent_id=agent_id,
            )
            if purpose == "invocation_attachment" and asset.created_by_user_id != user_id:
                raise AttachmentAccessError(
                    "attachment_unavailable",
                    "部分附件不存在、已失效或不能作为正式运行输入",
                    status_code=404,
                )
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
            _check_dataset_scope(
                db,
                dataset,
                version,
                tenant_id=tenant_id,
                agent_id=agent_id,
            )
            if (
                attachment.expected_signature
                and attachment.expected_signature != version.content_hash
            ):
                raise AttachmentAccessError("attachment_conflict", "附件版本已变化，请重新选择后提交")
