"""Tenant catalog and revocable scenario data-binding endpoints."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..catalog_schemas import (
    CatalogManagedUploadMetadata,
    CatalogManagedUploadOut,
    CatalogUsagePlane,
    ConnectorBindingOptionOut,
    DataAssetCreate,
    DataAssetOut,
    DataAssetVersionOut,
    DataAssetVersionRegister,
    DatasetFieldOut,
    DatasetHeadOut,
    DatasetHeadSet,
    DatasetRelationOut,
    DatasetSchemaCreate,
    DatasetSchemaOut,
    DatasetVersionCreate,
    DatasetVersionOut,
    LogicalDatasetCreate,
    LogicalDatasetOut,
    ScenarioCapabilityPortCreate,
    ScenarioCapabilityPortOut,
    ScenarioDatasetBindingCreate,
    ScenarioDatasetBindingOut,
    SemanticFieldMappingOut,
    SemanticMappingCreate,
    SemanticMappingOut,
    ValidationDatasetBuildIn,
    ValidationDatasetJobOut,
    ValidationDatasetOut,
)
from ..models import (
    Agent,
    BucketFile,
    Conversation,
    DataAsset,
    DataAssetVersion,
    DataSource,
    DatasetHead,
    DatasetFragment,
    DatasetSchema,
    DatasetVersion,
    DatasetVersionAsset,
    IngestionRun,
    LogicalDataset,
    ManagedUploadRun,
    ScenarioCapabilityPort,
    ScenarioDatasetBinding,
    SemanticMapping,
)
from ..config import get_settings
from ..services import (
    agent_deletion_reference_service,
    agent_scope_access_service,
    catalog_ingestion_service,
    catalog_service,
    connector_service,
    datasource_service,
    object_deletion_service,
    object_storage_service,
    permission_service,
    template_catalog_service,
    tenant_service,
    upload_staging_service,
    validation_dataset_service,
)
from ..services.auth_service import get_current_user, get_tenant_db


router = APIRouter(
    prefix="/catalog",
    tags=["data-catalog"],
    dependencies=[Depends(get_current_user)],
)
scenario_router = APIRouter(
    prefix="/scenarios",
    tags=["scenario-data-bindings"],
    dependencies=[Depends(get_current_user)],
)


def _commit(db: Session, *, conflict: str) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=conflict) from exc


def _catalog_error(exc: catalog_service.CatalogError, status_code: int = 400) -> HTTPException:
    return HTTPException(status_code=status_code, detail=str(exc))


def _asset_out(asset) -> DataAssetOut:
    return DataAssetOut(
        id=asset.id,
        tenant_id=asset.tenant_id,
        key=asset.key,
        name=asset.name,
        description=asset.description or "",
        kind=asset.kind,
        media_type=asset.media_type or "",
        usage_plane=asset.usage_plane,
        labels=asset.labels or {},
        lifecycle_status=asset.lifecycle_status,
        created_by_user_id=asset.created_by_user_id,
        owner_agent_id=asset.owner_agent_id,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
        retired_at=asset.retired_at,
        version_count=len(asset.versions),
    )


def _dataset_out(dataset: LogicalDataset) -> LogicalDatasetOut:
    public_labels = {
        key: value
        for key, value in dict(dataset.labels or {}).items()
        if key
        not in {
            "modeling_source_data_source_id",
            "modeling_source_bucket_file_id",
            "source_content_sha256",
        }
    }
    return LogicalDatasetOut(
        id=dataset.id,
        tenant_id=dataset.tenant_id,
        key=dataset.key,
        name=dataset.name,
        description=dataset.description or "",
        usage_plane=dataset.usage_plane,
        labels=public_labels,
        lifecycle_status=dataset.lifecycle_status,
        created_by_user_id=dataset.created_by_user_id,
        created_at=dataset.created_at,
        updated_at=dataset.updated_at,
        retired_at=dataset.retired_at,
        schema_count=len(dataset.schemas),
        version_count=len(dataset.versions),
        head_version_id=next((item.dataset_version_id for item in dataset.heads), None),
    )


def _schema_out(schema: DatasetSchema) -> DatasetSchemaOut:
    return DatasetSchemaOut(
        id=schema.id,
        tenant_id=schema.tenant_id,
        dataset_id=schema.dataset_id,
        schema_version=schema.schema_version,
        schema_hash=schema.schema_hash,
        compatibility=schema.compatibility,
        schema_document=schema.schema_document or {},
        relations=[
            DatasetRelationOut(
                id=relation.id,
                relation_key=relation.relation_key,
                display_name=relation.display_name,
                kind=relation.kind,
                ordinal=relation.ordinal,
                description=relation.description or "",
                fields=[
                    DatasetFieldOut.model_validate(field)
                    for field in relation.fields
                ],
            )
            for relation in schema.relations
        ],
        created_by_user_id=schema.created_by_user_id,
        created_at=schema.created_at,
    )


def _binding_out(binding: ScenarioDatasetBinding) -> ScenarioDatasetBindingOut:
    return ScenarioDatasetBindingOut(
        id=binding.id,
        tenant_id=binding.tenant_id,
        scenario_id=binding.scenario_id,
        dataset_id=binding.dataset_id,
        binding_key=binding.binding_key,
        role=binding.role,
        binding_mode=binding.binding_mode,
        dataset_head_id=binding.dataset_head_id,
        dataset_version_id=binding.dataset_version_id,
        is_required=binding.is_required,
        status=binding.status,
        config=binding.config or {},
        resolved_dataset_version_id=catalog_service.resolved_binding_version(binding),
        created_at=binding.created_at,
        updated_at=binding.updated_at,
    )


def _port_out(port: ScenarioCapabilityPort) -> ScenarioCapabilityPortOut:
    return ScenarioCapabilityPortOut(
        id=port.id,
        tenant_id=port.tenant_id,
        scenario_id=port.scenario_id,
        capability_kind=port.capability_kind,
        capability_key=port.capability_key,
        port_key=port.port_key,
        name=port.name,
        description=port.description or "",
        direction=port.direction,
        role=port.role,
        media_kind=port.media_kind,
        dataset_id=port.dataset_id,
        dataset_schema_id=port.dataset_schema_id,
        dataset_schema_hash=(
            port.dataset_schema.schema_hash if port.dataset_schema else ""
        ),
        schema_document=port.schema_document or {},
        is_required=bool(port.is_required),
        cardinality=port.cardinality,
        binding_policy=port.binding_policy,
        status=port.status,
        config=port.config or {},
        created_by_user_id=port.created_by_user_id,
        created_at=port.created_at,
        updated_at=port.updated_at,
    )


def _mapping_out(mapping: SemanticMapping) -> SemanticMappingOut:
    return SemanticMappingOut(
        id=mapping.id,
        tenant_id=mapping.tenant_id,
        dataset_id=mapping.dataset_id,
        scenario_id=mapping.scenario_id,
        entity_id=mapping.entity_id,
        scenario_dataset_binding_id=mapping.scenario_dataset_binding_id,
        dataset_schema_id=mapping.dataset_schema_id,
        dataset_relation_id=mapping.dataset_relation_id,
        mapping_key=mapping.mapping_key,
        status=mapping.status,
        identifier_strategy=mapping.identifier_strategy or {},
        filter_expression=mapping.filter_expression or {},
        fields=[
            SemanticFieldMappingOut.model_validate(item)
            for item in mapping.field_mappings
        ],
        created_at=mapping.created_at,
        updated_at=mapping.updated_at,
    )


def _managed_upload_out(
    asset,
    version,
    *,
    fallback_purpose: str,
    created: bool,
) -> CatalogManagedUploadOut:
    return CatalogManagedUploadOut.model_validate(
        catalog_ingestion_service.managed_upload_document(
            asset,
            version,
            fallback_purpose=fallback_purpose,
            created=created,
        )
    )


_CATALOG_UPLOAD_FORM_FIELDS = {
    "file",
    "file_bucket_id",
    "purpose",
    "asset_key",
    "name",
    "description",
    "labels",
    "expires_in_seconds",
    "agent_id",
    "conversation_id",
}
_CATALOG_UPLOAD_PHYSICAL_FIELDS = {
    "bucket",
    "bucket_name",
    "object_key",
    "object_path",
    "path",
    "prefix",
    "endpoint",
    "access_key",
    "secret_key",
    "password",
    "credential",
    "credentials",
    "token",
}


def _authorize_asset_agent(
    db: Session,
    agent_id: str | None,
    *,
    permission_verb: str = "read",
    active_runtime: bool = False,
    lock: bool = False,
) -> Agent | None:
    """Authorize an optional Agent catalog scope without exposing resources."""
    if permission_verb not in {"read", "write"}:
        raise ValueError("不支持的资产权限动作")
    try:
        agent = agent_scope_access_service.require_optional_agent_permission(
            db,
            agent_id,
            permission_verb,
            lock=lock,
            message="没有该 Agent 所属业务场景的权限",
        )
    except agent_scope_access_service.AgentScopeNotFoundError as exc:
        raise HTTPException(status_code=404, detail="资产不存在") from exc
    if agent is None:
        return None
    if agent.scenario_id:
        scenario = tenant_service.require_scenario(db, agent.scenario_id)
        if active_runtime and scenario.status == "retired":
            raise HTTPException(409, "业务场景已退役，不能新增验证附件")
    return agent


def _require_catalog_dataset_read(
    db: Session,
    dataset_id: str,
    *,
    agent_id: str | None = None,
) -> LogicalDataset:
    if agent_id is None:
        permission_service.require_tenant_permission(db, "read")
    try:
        return catalog_service.require_dataset(
            db,
            dataset_id,
            agent_id=agent_id,
        )
    except catalog_service.CatalogError as exc:
        raise HTTPException(status_code=404, detail="逻辑数据集不存在") from exc


def _authorize_asset_storage_sources(
    db: Session,
    asset: DataAsset,
    versions: list[DataAssetVersion],
    files: list[BucketFile],
    *,
    agent_id: str | None,
) -> None:
    """Apply the owning scenario ACL to every file touched by asset deletion.

    Catalog assets are tenant-wide identities, but their physical files may
    live in a scenario-owned source.  Deleting through this route must not
    become an alternate path around the scenario write ACL (or around an
    Agent's private runtime scope).
    """
    source_ids = {
        str(version.bucket_data_source_id)
        for version in versions
        if version.bucket_data_source_id
    }
    source_ids.update(str(item.data_source_id) for item in files if item.data_source_id)
    if not source_ids:
        return
    sources = list(
        db.scalars(
            select(DataSource)
            .where(
                DataSource.id.in_(sorted(source_ids)),
                DataSource.tenant_id == asset.tenant_id,
            )
            .with_for_update()
        ).all()
    )
    by_id = {str(source.id): source for source in sources}
    if set(by_id) != source_ids:
        raise catalog_service.CatalogError("资产文件存储归属无效")
    for source in sources:
        if agent_id:
            if source.owner_agent_id not in (None, agent_id):
                raise HTTPException(status_code=404, detail="资产不存在")
        elif source.owner_agent_id is not None:
            # An unscoped catalog request cannot delete a private runtime
            # source even when the asset itself has a legacy NULL owner.
            raise HTTPException(status_code=404, detail="资产不存在")
        if source.scenario_id:
            scenario = tenant_service.require_scenario(db, source.scenario_id)
            permission_service.require_scenario_permission(
                db,
                scenario,
                "write",
                message="没有该资产存储所属业务场景的删除权限",
            )


_ASSET_DELETE_REFERENCE_CONFLICT = "资产仍被独立数据集或运行记录引用，不能删除"


def _is_owned_validation_dataset(
    dataset: LogicalDataset,
    agent_id: str | None,
) -> bool:
    if not agent_id or dataset.usage_plane != "invocation_input":
        return False
    labels = dataset.labels if isinstance(dataset.labels, dict) else {}
    return (
        labels.get("catalog_purpose") == "validation_dataset"
        and str(labels.get("owner_agent_id") or "") == str(agent_id)
    )


def _asset_deletion_dataset_scope(
    db: Session,
    asset: DataAsset,
    versions: list[DataAssetVersion],
    *,
    agent_id: str | None,
) -> tuple[set[str], set[str]]:
    """Return only same-Agent validation datasets derived from this asset."""

    version_ids = [str(item.id) for item in versions]
    if not version_ids:
        return set(), set()
    rows = db.execute(
        select(DatasetVersionAsset, DatasetVersion, LogicalDataset)
        .join(
            DatasetVersion,
            (DatasetVersion.id == DatasetVersionAsset.dataset_version_id)
            & (DatasetVersion.tenant_id == DatasetVersionAsset.tenant_id),
        )
        .join(
            LogicalDataset,
            (LogicalDataset.id == DatasetVersion.dataset_id)
            & (LogicalDataset.tenant_id == DatasetVersion.tenant_id),
        )
        .where(
            DatasetVersionAsset.asset_version_id.in_(version_ids),
            DatasetVersionAsset.tenant_id == asset.tenant_id,
        )
        .with_for_update()
    ).all()
    datasets: set[str] = set()
    dataset_versions: set[str] = set()
    for link, version, dataset in rows:
        if (
            str(link.dataset_id) != str(version.dataset_id)
            or str(dataset.id) != str(version.dataset_id)
            or str(dataset.tenant_id) != str(asset.tenant_id)
            or not _is_owned_validation_dataset(dataset, agent_id)
        ):
            raise catalog_service.CatalogError(_ASSET_DELETE_REFERENCE_CONFLICT)
        datasets.add(str(dataset.id))
        dataset_versions.add(str(version.id))
    return datasets, dataset_versions


def _retire_owned_validation_dataset_children(
    db: Session,
    *,
    tenant_id: str,
    agent_id: str | None,
    dataset_ids: set[str],
) -> dict[str, int]:
    """Retire validation packages proven to depend on a deleted Agent asset.

    ``detach_platform_catalog_references_for_deletion`` intentionally works
    at the physical-file boundary and therefore cannot know which logical
    package owns a file.  Once the caller has proved the exact same-Agent
    DatasetVersionAsset links, retire only those parent packages and their
    versions/jobs.  Independent datasets never enter this function.
    """

    if not agent_id or not dataset_ids:
        return {
            "validation_datasets_retired": 0,
            "validation_versions_retired": 0,
            "validation_jobs_cancelled": 0,
        }
    datasets = list(
        db.scalars(
            select(LogicalDataset)
            .where(
                LogicalDataset.id.in_(sorted(dataset_ids)),
                LogicalDataset.tenant_id == tenant_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        ).all()
    )
    if {str(item.id) for item in datasets} != {str(item) for item in dataset_ids}:
        raise catalog_service.CatalogError(_ASSET_DELETE_REFERENCE_CONFLICT)
    if any(not _is_owned_validation_dataset(item, agent_id) for item in datasets):
        raise catalog_service.CatalogError(_ASSET_DELETE_REFERENCE_CONFLICT)

    versions = list(
        db.scalars(
            select(DatasetVersion)
            .where(
                DatasetVersion.tenant_id == tenant_id,
                DatasetVersion.dataset_id.in_(sorted(dataset_ids)),
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        ).all()
    )
    jobs = list(
        db.scalars(
            select(IngestionRun)
            .where(
                IngestionRun.tenant_id == tenant_id,
                IngestionRun.dataset_id.in_(sorted(dataset_ids)),
                IngestionRun.pipeline_kind == "validation_dataset",
                IngestionRun.status.in_(("pending", "running")),
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        ).all()
    )
    now = datetime.now(timezone.utc)
    for version in versions:
        # A package whose source asset was explicitly deleted is no longer a
        # selectable immutable version.  Keep the row for audit/FK history.
        version.status = "retired"
    for run in jobs:
        run.status = "cancelled"
        run.error = "所属附件数据源已删除，验证数据包任务已取消"
        run.lease_token = ""
        run.lease_expires_at = None
        run.finished_at = now
    for dataset in datasets:
        labels = dict(dataset.labels or {}) if isinstance(dataset.labels, dict) else {}
        labels.update(
            {
                "catalog_purpose": "validation_dataset",
                "owner_agent_id": str(agent_id),
                "lifecycle": "asset_deleted",
                "deleted_at": now.isoformat(),
            }
        )
        dataset.lifecycle_status = "retired"
        dataset.retired_at = now
        dataset.name = "已删除验证附件数据源"
        dataset.description = ""
        dataset.labels = labels
    return {
        "validation_datasets_retired": len(datasets),
        "validation_versions_retired": len(versions),
        "validation_jobs_cancelled": len(jobs),
    }


def _assert_unscoped_asset_file_references(
    db: Session,
    asset: DataAsset,
    files: list[BucketFile],
    *,
    allowed_dataset_version_ids: set[str],
) -> None:
    """Keep the legacy/global asset path from detaching independent files."""

    file_ids = {str(item.id) for item in files}
    if not file_ids:
        return
    asset_ref = db.scalar(
        select(DataAssetVersion.id)
        .where(
            DataAssetVersion.bucket_file_id.in_(sorted(file_ids)),
            DataAssetVersion.tenant_id == asset.tenant_id,
            DataAssetVersion.asset_id != asset.id,
        )
        .limit(1)
    )
    if asset_ref is not None:
        raise catalog_service.CatalogError(_ASSET_DELETE_REFERENCE_CONFLICT)
    fragment_ref = db.scalar(
        select(DatasetFragment.id)
        .join(
            DatasetVersion,
            (DatasetVersion.id == DatasetFragment.dataset_version_id)
            & (DatasetVersion.tenant_id == DatasetFragment.tenant_id),
        )
        .where(
            DatasetFragment.bucket_file_id.in_(sorted(file_ids)),
            DatasetFragment.tenant_id == asset.tenant_id,
            ~DatasetFragment.dataset_version_id.in_(
                sorted(allowed_dataset_version_ids)
            )
            if allowed_dataset_version_ids
            else True,
        )
        .limit(1)
    )
    manifest_ref = db.scalar(
        select(DatasetVersion.id)
        .where(
            DatasetVersion.manifest_bucket_file_id.in_(sorted(file_ids)),
            DatasetVersion.tenant_id == asset.tenant_id,
            ~DatasetVersion.id.in_(sorted(allowed_dataset_version_ids))
            if allowed_dataset_version_ids
            else True,
        )
        .limit(1)
    )
    if fragment_ref is not None or manifest_ref is not None:
        raise catalog_service.CatalogError(_ASSET_DELETE_REFERENCE_CONFLICT)


def _asset_upload_runs(
    db: Session,
    asset: DataAsset,
    versions: list[DataAssetVersion],
    files: list[BucketFile],
    *,
    agent_id: str | None,
) -> list[ManagedUploadRun]:
    """Lock upload children and reject a cross-owner pointer."""

    predicates = [ManagedUploadRun.asset_id == asset.id]
    version_ids = {str(item.id) for item in versions}
    file_ids = {str(item.id) for item in files}
    if version_ids:
        predicates.append(ManagedUploadRun.asset_version_id.in_(sorted(version_ids)))
    if file_ids:
        predicates.append(ManagedUploadRun.bucket_file_id.in_(sorted(file_ids)))
    runs = list(
        db.scalars(
            select(ManagedUploadRun)
            .where(
                ManagedUploadRun.tenant_id == asset.tenant_id,
                or_(*predicates),
            )
            .with_for_update()
        ).all()
    )
    for run in runs:
        owner_matches = (
            str(run.owner_agent_id) == str(agent_id)
            if agent_id
            else run.owner_agent_id is None
        )
        run_asset_id = str(run.asset_id) if run.asset_id else None
        run_version_id = str(run.asset_version_id) if run.asset_version_id else None
        points_at_asset = (
            run_asset_id == str(asset.id)
            and (run_version_id is None or run_version_id in version_ids)
        ) or (
            run_asset_id is None
            and run_version_id is not None
            and run_version_id in version_ids
        )
        if not owner_matches or not points_at_asset:
            raise catalog_service.CatalogError(_ASSET_DELETE_REFERENCE_CONFLICT)
    return runs


def _assert_scoped_asset_file_references(
    db: Session,
    asset: DataAsset,
    files: list[BucketFile],
    *,
    agent: Agent,
    allowed_dataset_ids: set[str],
    allowed_dataset_version_ids: set[str],
    owned_run_ids: set[str],
) -> None:
    """Reuse the Agent cleanup proof for the security-definer detach call."""

    file_ids = {str(item.id) for item in files}
    if file_ids:
        # The detach function operates by physical file, not by DatasetVersion.
        # Prove that every fragment/manifest selected by that operation belongs
        # to one of the exact child versions being retired.  A second version
        # of the same logical dataset is still an independent immutable row.
        fragment_statement = select(DatasetFragment.dataset_version_id).where(
            DatasetFragment.tenant_id == asset.tenant_id,
            DatasetFragment.bucket_file_id.in_(sorted(file_ids)),
        )
        manifest_statement = select(DatasetVersion.id).where(
            DatasetVersion.tenant_id == asset.tenant_id,
            DatasetVersion.manifest_bucket_file_id.in_(sorted(file_ids)),
        )
        if allowed_dataset_version_ids:
            fragment_statement = fragment_statement.where(
                ~DatasetFragment.dataset_version_id.in_(
                    sorted(allowed_dataset_version_ids)
                )
            )
            manifest_statement = manifest_statement.where(
                ~DatasetVersion.id.in_(sorted(allowed_dataset_version_ids))
            )
        if (
            db.scalar(fragment_statement.limit(1)) is not None
            or db.scalar(manifest_statement.limit(1)) is not None
        ):
            raise catalog_service.CatalogError(_ASSET_DELETE_REFERENCE_CONFLICT)

    for item in files:
        if agent_deletion_reference_service.generated_file_has_external_reference(
            db,
            file=item,
            agent=agent,
            owned_dataset_ids=allowed_dataset_ids,
            owned_dataset_version_ids=allowed_dataset_version_ids,
            owned_asset_ids={str(asset.id)},
            owned_run_ids=owned_run_ids,
        ):
            raise catalog_service.CatalogError(_ASSET_DELETE_REFERENCE_CONFLICT)


def _fence_asset_upload_runs(
    runs: list[ManagedUploadRun],
    *,
    now: datetime,
) -> None:
    """Fence and redact durable upload children before physical deletion."""

    for run in runs:
        run.status = "cancelled"
        run.revision = max(1, int(run.revision or 1) + 1)
        run.lease_generation = max(0, int(run.lease_generation or 0) + 1)
        run.lease_token = ""
        run.lease_expires_at = None
        run.available_at = now
        run.error_code = "asset_deleted"
        run.error_message = "所属附件数据源已删除"
        run.finished_at = now
        run.updated_at = now
        run.bucket_file_id = None
        run.asset_id = None
        run.asset_version_id = None


@router.get("/assets", response_model=list[DataAssetOut])
def list_assets(
    usage_plane: CatalogUsagePlane | None = None,
    agent_id: str | None = None,
    db: Session = Depends(get_tenant_db),
) -> list[DataAssetOut]:
    try:
        _authorize_asset_agent(db, agent_id)
        return [
            _asset_out(item)
            for item in catalog_service.list_assets(
                db,
                usage_plane=usage_plane,
                owner_agent_id=agent_id,
            )
        ]
    except catalog_service.CatalogError as exc:
        raise _catalog_error(exc) from exc


@router.post(
    "/assets",
    response_model=DataAssetOut,
    status_code=status.HTTP_201_CREATED,
)
def create_asset(
    payload: DataAssetCreate,
    db: Session = Depends(get_tenant_db),
) -> DataAssetOut:
    try:
        item = catalog_service.create_asset(db, payload)
        _commit(db, conflict="资产 key 已存在")
        loaded = next(
            entry for entry in catalog_service.list_assets(db) if entry.id == item.id
        )
        return _asset_out(loaded)
    except catalog_service.CatalogError as exc:
        db.rollback()
        raise _catalog_error(exc) from exc


@router.delete("/assets/{asset_id}")
def delete_asset(
    asset_id: str,
    agent_id: str | None = None,
    db: Session = Depends(get_tenant_db),
) -> dict[str, object]:
    """Retire one tenant asset and durably remove every owned MinIO payload.

    Published capability definitions contain contracts rather than asset ids,
    so this operation never edits a release. Any validation dataset assembled
    from the deleted asset is retired and cannot be selected for a new run.
    """
    scoped_agent = _authorize_asset_agent(
        db,
        agent_id,
        permission_verb="write",
        lock=bool(agent_id),
    )
    asset = db.scalar(
        select(DataAsset)
        .where(
            DataAsset.id == asset_id,
            DataAsset.tenant_id == tenant_service.current_tenant_id(db),
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if asset is None:
        raise HTTPException(status_code=404, detail="资产不存在")
    if asset.owner_agent_id:
        if agent_id != asset.owner_agent_id:
            raise HTTPException(status_code=404, detail="资产不存在")
    elif agent_id:
        raise HTTPException(status_code=404, detail="资产不存在")
    if asset.lifecycle_status != "active":
        return {"message": "资产已删除", "asset_id": asset.id, "cleanup_jobs": []}
    versions = list(
        db.scalars(
            select(DataAssetVersion)
            .where(
                DataAssetVersion.asset_id == asset.id,
                DataAssetVersion.tenant_id == asset.tenant_id,
            )
            .order_by(DataAssetVersion.version_number)
            .with_for_update()
        ).all()
    )
    try:
        dependent_dataset_ids, dependent_dataset_version_ids = (
            _asset_deletion_dataset_scope(
                db,
                asset,
                versions,
                agent_id=agent_id,
            )
        )
    except catalog_service.CatalogError as exc:
        db.rollback()
        raise _catalog_error(exc, status_code=409) from exc
    version_ids = [str(item.id) for item in versions]
    file_id_set = {str(item.bucket_file_id) for item in versions if item.bucket_file_id}
    if dependent_dataset_version_ids:
        file_id_set.update(
            str(value)
            for value in db.scalars(
                select(DatasetFragment.bucket_file_id).where(
                    DatasetFragment.dataset_version_id.in_(
                        sorted(dependent_dataset_version_ids)
                    ),
                    DatasetFragment.tenant_id == asset.tenant_id,
                )
            ).all()
            if value
        )
        # Validation datasets normally keep their manifest inline, but older
        # generated versions may have a managed manifest object.  Include it
        # only when the owning dataset/version passed the strict scope check.
        file_id_set.update(
            str(value)
            for value in db.scalars(
                select(DatasetVersion.manifest_bucket_file_id).where(
                    DatasetVersion.id.in_(sorted(dependent_dataset_version_ids)),
                    DatasetVersion.tenant_id == asset.tenant_id,
                    DatasetVersion.manifest_bucket_file_id.is_not(None),
                )
            ).all()
            if value
        )
    file_ids = sorted(file_id_set)
    files = (
        list(
            db.scalars(
                select(BucketFile)
                .join(DataSource, DataSource.id == BucketFile.data_source_id)
                .where(
                    BucketFile.id.in_(file_ids),
                    DataSource.tenant_id == asset.tenant_id,
                )
                .with_for_update()
            ).all()
        )
        if file_ids
        else []
    )
    try:
        if len(files) != len(file_ids):
            raise catalog_service.CatalogError("资产文件记录不存在或租户归属无效")
        upload_runs = _asset_upload_runs(
            db, asset, versions, files, agent_id=agent_id
        )
        if scoped_agent is not None:
            _assert_scoped_asset_file_references(
                db,
                asset,
                files,
                agent=scoped_agent,
                allowed_dataset_ids=dependent_dataset_ids,
                allowed_dataset_version_ids=dependent_dataset_version_ids,
                owned_run_ids={str(item.id) for item in upload_runs},
            )
        else:
            _assert_unscoped_asset_file_references(
                db,
                asset,
                files,
                allowed_dataset_version_ids=dependent_dataset_version_ids,
            )
        _authorize_asset_storage_sources(
            db,
            asset,
            versions,
            files,
            agent_id=agent_id,
        )
        template_catalog_service.assert_bucket_files_not_registered(
            db, [item.id for item in files]
        )
        cleanup_jobs: list[str] = []
        cleanup = {
            "asset_versions_detached": 0,
            "dataset_fragments_deleted": 0,
            "manifest_versions_detached": 0,
            "validation_datasets_retired": 0,
            "validation_versions_retired": 0,
            "validation_jobs_cancelled": 0,
        }
        _fence_asset_upload_runs(upload_runs, now=datetime.now(timezone.utc))
        by_source: dict[str, list[BucketFile]] = {}
        for item in files:
            by_source.setdefault(item.data_source_id, []).append(item)
        for source_id, source_files in by_source.items():
            source = db.get(DataSource, source_id)
            if source is None or source.tenant_id != asset.tenant_id:
                raise catalog_service.CatalogError("资产文件存储归属无效")
            for item in source_files:
                cleanup_jobs.append(
                    object_deletion_service.enqueue_bucket_file_deletion(db, item, source)
                )
            result = datasource_service.detach_platform_catalog_references_for_deletion(
                db, source, [item.id for item in source_files]
            )
            for key, value in result.items():
                cleanup[key] = int(cleanup.get(key, 0)) + int(value)
            for item in source_files:
                db.delete(item)
        validation_cleanup = _retire_owned_validation_dataset_children(
            db,
            tenant_id=asset.tenant_id,
            agent_id=agent_id,
            dataset_ids=dependent_dataset_ids,
        )
        cleanup.update(validation_cleanup)
        asset.lifecycle_status = "retired"
        asset.retired_at = datetime.now(timezone.utc)
        db.commit()
    except (catalog_service.CatalogError, ValueError) as exc:
        db.rollback()
        raise _catalog_error(
            exc if isinstance(exc, catalog_service.CatalogError) else catalog_service.CatalogError(str(exc)),
            status_code=409,
        ) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="资产在删除期间被其他资源占用") from exc
    object_deletion_service.drain_jobs_best_effort(db, cleanup_jobs)
    return {
        "message": "资产已删除，已发布能力定义不受影响",
        "asset_id": asset.id,
        "cleanup_jobs": cleanup_jobs,
        **cleanup,
    }


@router.post(
    "/uploads",
    response_model=CatalogManagedUploadOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_managed_catalog_file(
    request: Request,
    file: UploadFile = File(...),
    file_bucket_id: str | None = Form(None),
    purpose: str = Form("managed_asset"),
    asset_key: str | None = Form(None),
    name: str | None = Form(None),
    description: str = Form(""),
    labels: str = Form("{}"),
    expires_in_seconds: int | None = Form(None),
    agent_id: str | None = Form(None),
    conversation_id: str | None = Form(None),
    db: Session = Depends(get_tenant_db),
) -> CatalogManagedUploadOut:
    """Upload one immutable catalog asset through a server-managed MinIO bucket.

    This endpoint is intentionally separate from legacy data-source upload and
    never creates runtime/scenario bindings.  Multipart requests cannot supply
    physical object coordinates or credentials, even as ignored extra fields.
    """
    form = await request.form()
    supplied_fields = {str(key).strip().lower() for key in form.keys()}
    disallowed = supplied_fields - _CATALOG_UPLOAD_FORM_FIELDS
    if disallowed:
        if disallowed.intersection(_CATALOG_UPLOAD_PHYSICAL_FIELDS) or any(
            any(marker in key for marker in ("password", "secret", "credential"))
            for key in disallowed
        ):
            raise HTTPException(
                status_code=400,
                detail="上传请求不得包含对象路径、存储配置或凭据字段",
            )
        raise HTTPException(status_code=422, detail="上传请求包含未知字段")
    if len(form.getlist("file")) != 1:
        raise HTTPException(status_code=422, detail="每次目录上传只能包含一个文件")
    agent_id = str(agent_id or "").strip() or None
    conversation_id = str(conversation_id or "").strip() or None
    try:
        label_document = json.loads(labels)
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="labels 必须是 JSON 对象") from exc
    if not isinstance(label_document, dict):
        raise HTTPException(status_code=422, detail="labels 必须是 JSON 对象")
    if agent_id and purpose not in {"invocation_attachment", "validation_asset"}:
        raise HTTPException(
            status_code=422,
            detail="Agent 作用域只允许上传验证资料或本次调用附件",
        )
    if agent_id:
        _authorize_asset_agent(
            db,
            agent_id,
            permission_verb="write",
            active_runtime=True,
        )
        if conversation_id:
            principal = permission_service.require_principal(db)
            conversation = db.scalar(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.agent_id == agent_id,
                    Conversation.created_by_user_id == principal.user_id,
                )
            )
            if conversation is None:
                raise HTTPException(status_code=404, detail="对话不存在或无权使用")
    elif conversation_id:
        raise HTTPException(status_code=422, detail="conversation_id 必须与 agent_id 一起提供")
    try:
        source = (
            catalog_ingestion_service.require_external_upload_bucket(
                db,
                owner_agent_id=agent_id,
            )
            if purpose in {"invocation_attachment", "validation_asset"}
            else catalog_ingestion_service.require_managed_file_bucket(
                db, str(file_bucket_id or "")
            )
        )
        metadata = CatalogManagedUploadMetadata(
            file_bucket_id=source.id,
            purpose=purpose,
            asset_key=asset_key,
            name=name,
            description=description,
            labels=label_document,
            expires_in_seconds=expires_in_seconds,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    except catalog_service.CatalogError as exc:
        db.rollback()
        raise _catalog_error(exc) from exc

    # Bucket resolution may create/repair the deterministic source row and
    # briefly lock it.  Commit that control-plane setup before staging bytes or
    # calling MinIO; the durable upload path reacquires Agent/asset fences at
    # publication, so a concurrent revoke/delete cannot be masked by a stale
    # source lock held across external I/O.
    db.commit()

    try:
        # Resolve ACL and managed-storage policy before parsing attacker-owned
        # bytes so an inaccessible bucket cannot be used as a profiling oracle.
        settings = get_settings()
        max_upload_bytes = int(
            getattr(settings, "catalog_max_upload_bytes", settings.max_upload_bytes)
        )
        chunk_bytes = int(getattr(settings, "upload_stream_chunk_bytes", 1024 * 1024))
        in_memory_bytes = int(
            getattr(settings, "catalog_in_memory_upload_bytes", settings.max_upload_bytes)
        )
        try:
            staged = await upload_staging_service.stage_upload(
                file,
                max_bytes=max_upload_bytes,
                chunk_bytes=chunk_bytes,
            )
        except upload_staging_service.UploadTooLargeError as exc:
            raise HTTPException(
                status_code=413,
                detail=(
                    "文件超过大小限制（"
                    f"{max_upload_bytes // (1024 * 1024)} MB）"
                ),
            ) from exc
        try:
            if staged.byte_size <= in_memory_bytes:
                result = catalog_ingestion_service.persist_managed_upload(
                    db,
                    source,
                    staged.path.read_bytes(),
                    file.filename or "file",
                    file.content_type,
                    metadata,
                    owner_agent_id=agent_id,
                )
            else:
                result = catalog_ingestion_service.persist_managed_upload_path(
                    db,
                    source,
                    staged.path,
                    file.filename or "file",
                    file.content_type,
                    metadata,
                    content_sha256=staged.content_sha256,
                    byte_size=staged.byte_size,
                    owner_agent_id=agent_id,
                )
        finally:
            staged.remove()
        asset = db.get(DataAsset, result.asset_id)
        version = db.get(DataAssetVersion, result.version_id)
        if asset is None or version is None:
            raise RuntimeError("目录上传结果未能重新加载")
        return _managed_upload_out(
            asset,
            version,
            fallback_purpose=metadata.purpose,
            created=result.created,
        )
    except HTTPException:
        db.rollback()
        raise
    except catalog_service.CatalogError as exc:
        db.rollback()
        raise _catalog_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="目录资产在上传期间发生并发冲突，请重试",
        ) from exc
    except object_deletion_service.UploadIntentLeaseLostError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="文件上传事务已失效") from exc
    except (RuntimeError, object_storage_service.ObjectStorageError) as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="受管对象存储写入失败") from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@router.get(
    "/assets/{asset_id}/versions",
    response_model=list[DataAssetVersionOut],
)
def list_asset_versions(
    asset_id: str,
    agent_id: str | None = None,
    db: Session = Depends(get_tenant_db),
) -> list[DataAssetVersionOut]:
    _authorize_asset_agent(db, agent_id)
    asset = catalog_service.require_asset(db, asset_id)
    # Deleted/retired assets are kept only as control-plane audit tombstones.
    # They must not be resurrected through this metadata endpoint (especially
    # after Agent cleanup clears ``owner_agent_id``), otherwise a guessed old
    # asset id could reveal attachment history outside the active catalog.
    if asset.lifecycle_status != "active" or (
        isinstance(asset.labels, dict)
        and asset.labels.get("lifecycle") == "agent_deleted"
    ):
        raise HTTPException(status_code=404, detail="资产不存在")
    if asset.owner_agent_id != agent_id:
        raise HTTPException(status_code=404, detail="资产不存在")
    return [DataAssetVersionOut.model_validate(item) for item in asset.versions]


@router.post(
    "/validation-datasets",
    response_model=ValidationDatasetOut,
    status_code=status.HTTP_201_CREATED,
)
def build_validation_dataset(
    payload: ValidationDatasetBuildIn,
    db: Session = Depends(get_tenant_db),
) -> ValidationDatasetOut:
    try:
        result = validation_dataset_service.build_validation_dataset(db, payload)
        return ValidationDatasetOut.model_validate(result)
    except validation_dataset_service.ValidationDatasetError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except object_deletion_service.UploadIntentLeaseLostError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="数据集物化事务已失效") from exc
    except object_storage_service.ObjectStorageError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="验证数据集对象存储不可用") from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="相同验证数据包正在生成，请稍后重试") from exc


@router.post(
    "/validation-dataset-jobs",
    response_model=ValidationDatasetJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def enqueue_validation_dataset_job(
    payload: ValidationDatasetBuildIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_tenant_db),
) -> ValidationDatasetJobOut:
    try:
        document = validation_dataset_service.enqueue_validation_dataset_job(
            db, payload
        )
        if document["status"] == "queued":
            background_tasks.add_task(
                validation_dataset_service.process_validation_dataset_job,
                str(document["id"]),
            )
        return ValidationDatasetJobOut.model_validate(document)
    except validation_dataset_service.ValidationDatasetError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="相同验证数据包正在排队") from exc


@router.get(
    "/validation-dataset-jobs/{job_id}",
    response_model=ValidationDatasetJobOut,
)
def get_validation_dataset_job(
    job_id: str,
    agent_id: str | None = None,
    db: Session = Depends(get_tenant_db),
) -> ValidationDatasetJobOut:
    try:
        return ValidationDatasetJobOut.model_validate(
            validation_dataset_service.get_validation_dataset_job(
                db, job_id, agent_id=agent_id
            )
        )
    except validation_dataset_service.ValidationDatasetError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/assets/{asset_id}/versions",
    response_model=DataAssetVersionOut,
    status_code=status.HTTP_201_CREATED,
)
def register_asset_version(
    asset_id: str,
    payload: DataAssetVersionRegister,
    agent_id: str | None = None,
    db: Session = Depends(get_tenant_db),
) -> DataAssetVersionOut:
    try:
        asset = catalog_service.require_asset(db, asset_id)
        # Agent-owned attachment assets may only be extended from the same
        # Agent scope; an unscoped catalog caller must not append versions.
        if asset.owner_agent_id:
            if not agent_id:
                raise catalog_service.CatalogError("该附件数据源需要 Agent 作用域")
            _authorize_asset_agent(
                db,
                agent_id,
                permission_verb="write",
                active_runtime=True,
            )
            if asset.owner_agent_id != agent_id:
                raise catalog_service.CatalogError("资产不存在")
        elif agent_id:
            # An explicit Agent context must never be used to append to a
            # tenant/global asset.  Otherwise a guessed legacy asset id would
            # become a write bridge across the attachment namespaces.
            _authorize_asset_agent(
                db,
                agent_id,
                permission_verb="write",
                active_runtime=True,
            )
            raise catalog_service.CatalogError("资产不存在")
        version = catalog_service.register_asset_version(db, asset, payload)
        _commit(db, conflict="该资产版本已登记")
        db.refresh(version)
        return DataAssetVersionOut.model_validate(version)
    except catalog_service.CatalogError as exc:
        db.rollback()
        raise _catalog_error(exc) from exc


@router.get("/datasets", response_model=list[LogicalDatasetOut])
def list_datasets(
    usage_plane: CatalogUsagePlane | None = None,
    scenario_id: str | None = None,
    agent_id: str | None = None,
    db: Session = Depends(get_tenant_db),
) -> list[LogicalDatasetOut]:
    try:
        return [
            _dataset_out(item)
            for item in catalog_service.list_datasets(
                db,
                usage_plane=usage_plane,
                scenario_id=scenario_id,
                agent_id=agent_id,
            )
        ]
    except catalog_service.CatalogError as exc:
        raise _catalog_error(exc) from exc


@router.post(
    "/datasets",
    response_model=LogicalDatasetOut,
    status_code=status.HTTP_201_CREATED,
)
def create_dataset(
    payload: LogicalDatasetCreate,
    db: Session = Depends(get_tenant_db),
) -> LogicalDatasetOut:
    try:
        item = catalog_service.create_dataset(db, payload)
        _commit(db, conflict="数据集 key 已存在")
        loaded = next(
            entry for entry in catalog_service.list_datasets(db) if entry.id == item.id
        )
        return _dataset_out(loaded)
    except catalog_service.CatalogError as exc:
        db.rollback()
        raise _catalog_error(exc) from exc


@router.get(
    "/datasets/{dataset_id}/schemas",
    response_model=list[DatasetSchemaOut],
)
def list_dataset_schemas(
    dataset_id: str,
    agent_id: str | None = None,
    db: Session = Depends(get_tenant_db),
) -> list[DatasetSchemaOut]:
    dataset = _require_catalog_dataset_read(db, dataset_id, agent_id=agent_id)
    return [
        _schema_out(
            catalog_service.load_schema(
                db,
                item.id,
                dataset_id=dataset.id,
                agent_id=agent_id,
            )
        )
        for item in dataset.schemas
    ]


@router.post(
    "/datasets/{dataset_id}/schemas",
    response_model=DatasetSchemaOut,
    status_code=status.HTTP_201_CREATED,
)
def create_dataset_schema(
    dataset_id: str,
    payload: DatasetSchemaCreate,
    db: Session = Depends(get_tenant_db),
) -> DatasetSchemaOut:
    try:
        dataset = catalog_service.require_dataset(db, dataset_id)
        schema = catalog_service.create_schema(db, dataset, payload)
        schema_id = schema.id
        _commit(db, conflict="相同的数据集 Schema 已存在")
        return _schema_out(
            catalog_service.load_schema(db, schema_id, dataset_id=dataset.id)
        )
    except catalog_service.CatalogError as exc:
        db.rollback()
        raise _catalog_error(exc) from exc


@router.get(
    "/datasets/{dataset_id}/versions",
    response_model=list[DatasetVersionOut],
)
def list_dataset_versions(
    dataset_id: str,
    agent_id: str | None = None,
    db: Session = Depends(get_tenant_db),
) -> list[DatasetVersionOut]:
    dataset = _require_catalog_dataset_read(db, dataset_id, agent_id=agent_id)
    return [DatasetVersionOut.model_validate(item) for item in dataset.versions]


@router.post(
    "/datasets/{dataset_id}/versions",
    response_model=DatasetVersionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_dataset_version(
    dataset_id: str,
    payload: DatasetVersionCreate,
    db: Session = Depends(get_tenant_db),
) -> DatasetVersionOut:
    try:
        dataset = catalog_service.require_dataset(db, dataset_id)
        version = catalog_service.create_dataset_version(db, dataset, payload)
        _commit(db, conflict="相同的数据集版本已存在")
        db.refresh(version)
        return DatasetVersionOut.model_validate(version)
    except catalog_service.CatalogError as exc:
        db.rollback()
        raise _catalog_error(exc) from exc


@router.get(
    "/datasets/{dataset_id}/heads",
    response_model=list[DatasetHeadOut],
)
def list_dataset_heads(
    dataset_id: str,
    agent_id: str | None = None,
    db: Session = Depends(get_tenant_db),
) -> list[DatasetHeadOut]:
    dataset = _require_catalog_dataset_read(db, dataset_id, agent_id=agent_id)
    return [DatasetHeadOut.model_validate(item) for item in dataset.heads]


@router.put(
    "/datasets/{dataset_id}/head",
    response_model=DatasetHeadOut,
)
def set_dataset_head(
    dataset_id: str,
    payload: DatasetHeadSet,
    db: Session = Depends(get_tenant_db),
) -> DatasetHeadOut:
    try:
        dataset = catalog_service.require_dataset(db, dataset_id)
        head = catalog_service.set_head(
            db,
            dataset,
            payload.dataset_version_id,
            expected_version_id=payload.expected_dataset_version_id,
        )
        _commit(db, conflict="数据集 Head 在更新期间发生冲突")
        db.refresh(head)
        return DatasetHeadOut.model_validate(head)
    except catalog_service.CatalogError as exc:
        db.rollback()
        raise _catalog_error(exc) from exc


@scenario_router.get(
    "/{scenario_id}/connector-bindings",
    response_model=list[ConnectorBindingOptionOut],
)
def list_scenario_connector_binding_options(
    scenario_id: str,
    db: Session = Depends(get_tenant_db),
) -> list[ConnectorBindingOptionOut]:
    """List portable binding keys without leaking connector ids or config."""
    scenario = tenant_service.require_scenario(db, scenario_id)
    permission_service.require_scenario_permission(db, scenario, "read")
    return [
        ConnectorBindingOptionOut(
            binding_key=str(item.get("binding_key") or ""),
            label=str(
                item.get("reference_label")
                or item.get("name")
                or item.get("binding_key")
                or ""
            ),
            connector_kind=item["kind"],
            ready=bool(item.get("ready", False)),
            blocking_reason=str(item.get("blocking_reason") or ""),
            capabilities=[
                str(value)
                for value in (item.get("capabilities") or [])
                if str(value).strip()
            ],
            updated_at=item.get("updated_at"),
        )
        for item in connector_service.list_bindings(
            db, scenario
        )
    ]


@scenario_router.get(
    "/{scenario_id}/dataset-bindings",
    response_model=list[ScenarioDatasetBindingOut],
)
def list_scenario_dataset_bindings(
    scenario_id: str,
    db: Session = Depends(get_tenant_db),
) -> list[ScenarioDatasetBindingOut]:
    return [
        _binding_out(item)
        for item in catalog_service.list_scenario_bindings(db, scenario_id)
    ]


@scenario_router.post(
    "/{scenario_id}/dataset-bindings",
    response_model=ScenarioDatasetBindingOut,
    status_code=status.HTTP_201_CREATED,
)
def create_scenario_dataset_binding(
    scenario_id: str,
    payload: ScenarioDatasetBindingCreate,
    db: Session = Depends(get_tenant_db),
) -> ScenarioDatasetBindingOut:
    try:
        binding = catalog_service.create_scenario_binding(db, scenario_id, payload)
        binding_id = binding.id
        _commit(db, conflict="该环境中的绑定 key 已存在")
        loaded = db.execute(
            select(ScenarioDatasetBinding)
            .options(selectinload(ScenarioDatasetBinding.dataset_head))
            .where(ScenarioDatasetBinding.id == binding_id)
        ).scalar_one()
        return _binding_out(loaded)
    except catalog_service.CatalogError as exc:
        db.rollback()
        raise _catalog_error(exc) from exc


@scenario_router.delete("/{scenario_id}/dataset-bindings/{binding_id}")
def delete_scenario_dataset_binding(
    scenario_id: str,
    binding_id: str,
    db: Session = Depends(get_tenant_db),
) -> dict[str, str]:
    scenario = tenant_service.require_scenario(db, scenario_id, writable=True)
    permission_service.require_scenario_permission(db, scenario, "write")
    binding = db.execute(
        select(ScenarioDatasetBinding).where(
            ScenarioDatasetBinding.id == binding_id,
            ScenarioDatasetBinding.scenario_id == scenario.id,
            ScenarioDatasetBinding.tenant_id == tenant_service.current_tenant_id(db),
        )
    ).scalar_one_or_none()
    if binding is None:
        raise HTTPException(status_code=404, detail="场景数据绑定不存在")
    referenced = db.execute(
        select(SemanticMapping.id)
        .where(SemanticMapping.scenario_dataset_binding_id == binding.id)
        .limit(1)
    ).scalar_one_or_none()
    if referenced is not None:
        raise HTTPException(status_code=409, detail="绑定仍被语义映射引用，不能删除")
    db.delete(binding)
    _commit(db, conflict="绑定仍被语义映射引用，不能删除")
    return {"message": "已删除场景绑定，目录资产保持不变"}


@scenario_router.get(
    "/{scenario_id}/capability-ports",
    response_model=list[ScenarioCapabilityPortOut],
)
def list_scenario_capability_ports(
    scenario_id: str,
    db: Session = Depends(get_tenant_db),
) -> list[ScenarioCapabilityPortOut]:
    return [
        _port_out(item)
        for item in catalog_service.list_capability_ports(db, scenario_id)
    ]


@scenario_router.post(
    "/{scenario_id}/capability-ports",
    response_model=ScenarioCapabilityPortOut,
    status_code=status.HTTP_201_CREATED,
)
def create_scenario_capability_port(
    scenario_id: str,
    payload: ScenarioCapabilityPortCreate,
    db: Session = Depends(get_tenant_db),
) -> ScenarioCapabilityPortOut:
    try:
        port = catalog_service.create_capability_port(db, scenario_id, payload)
        port_id = port.id
        _commit(db, conflict="能力端口 key 已存在")
        return _port_out(
            catalog_service.require_capability_port(db, scenario_id, port_id)
        )
    except catalog_service.CatalogError as exc:
        db.rollback()
        raise _catalog_error(exc) from exc


@scenario_router.get(
    "/{scenario_id}/capability-ports/{port_id}",
    response_model=ScenarioCapabilityPortOut,
)
def get_scenario_capability_port(
    scenario_id: str,
    port_id: str,
    db: Session = Depends(get_tenant_db),
) -> ScenarioCapabilityPortOut:
    try:
        return _port_out(
            catalog_service.require_capability_port(db, scenario_id, port_id)
        )
    except catalog_service.CatalogError as exc:
        raise _catalog_error(exc, status_code=404) from exc


@scenario_router.put(
    "/{scenario_id}/capability-ports/{port_id}",
    response_model=ScenarioCapabilityPortOut,
)
def update_scenario_capability_port(
    scenario_id: str,
    port_id: str,
    payload: ScenarioCapabilityPortCreate,
    db: Session = Depends(get_tenant_db),
) -> ScenarioCapabilityPortOut:
    try:
        catalog_service.update_capability_port(db, scenario_id, port_id, payload)
        _commit(db, conflict="能力端口 key 已存在")
        return _port_out(
            catalog_service.require_capability_port(db, scenario_id, port_id)
        )
    except catalog_service.CatalogError as exc:
        db.rollback()
        raise _catalog_error(exc) from exc


@scenario_router.delete("/{scenario_id}/capability-ports/{port_id}")
def delete_scenario_capability_port(
    scenario_id: str,
    port_id: str,
    db: Session = Depends(get_tenant_db),
) -> dict[str, str]:
    try:
        catalog_service.delete_capability_port(db, scenario_id, port_id)
        _commit(db, conflict="能力端口仍被调用审计引用，不能删除")
        return {"message": "已删除能力端口；历史发布快照保持不变"}
    except catalog_service.CatalogError as exc:
        db.rollback()
        raise _catalog_error(exc) from exc


@scenario_router.get(
    "/{scenario_id}/semantic-mappings",
    response_model=list[SemanticMappingOut],
)
def list_semantic_mappings(
    scenario_id: str,
    db: Session = Depends(get_tenant_db),
) -> list[SemanticMappingOut]:
    return [
        _mapping_out(item)
        for item in catalog_service.list_semantic_mappings(db, scenario_id)
    ]


@scenario_router.post(
    "/{scenario_id}/semantic-mappings",
    response_model=SemanticMappingOut,
    status_code=status.HTTP_201_CREATED,
)
def create_semantic_mapping(
    scenario_id: str,
    payload: SemanticMappingCreate,
    db: Session = Depends(get_tenant_db),
) -> SemanticMappingOut:
    try:
        mapping = catalog_service.create_semantic_mapping(db, scenario_id, payload)
        mapping_id = mapping.id
        _commit(db, conflict="语义映射 key 或对象绑定已存在")
        loaded = db.execute(
            select(SemanticMapping)
            .options(selectinload(SemanticMapping.field_mappings))
            .where(SemanticMapping.id == mapping_id)
        ).scalar_one()
        return _mapping_out(loaded)
    except catalog_service.CatalogError as exc:
        db.rollback()
        raise _catalog_error(exc) from exc
