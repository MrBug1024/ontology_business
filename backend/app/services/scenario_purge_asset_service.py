"""Scenario-owned storage asset checks and deletion preparation.

This module owns the downward-only boundary for modeling data sources and
their files. It intentionally does not commit; the scenario purge application
service and its router own the surrounding transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from ..approval_models import WorkflowApprovalEvidence
from ..models import (
    Agent,
    ArtifactTemplate,
    ArtifactTemplateVersion,
    BucketFile,
    BusinessScenario,
    ConnectorBinding,
    DataAsset,
    DataAssetVersion,
    DataMapping,
    DataSource,
    DatasetFragment,
    DatasetVersion,
    DatasetVersionAsset,
    DerivationEvidence,
    DerivationRun,
    DocumentChunk,
    DocumentIndexJob,
    IngestionRun,
    IngestionRunInput,
    LogicalDataset,
    ManagedUploadRun,
    RelationDataMapping,
    RunInputBinding,
)
from . import (
    datasource_service,
    modeling_contract_source_service,
    object_deletion_service,
    template_catalog_service,
)


_ACTIVE_INDEX_STATUSES = ("queued", "running", "retry_waiting")
_BLOCKER_MESSAGES = {
    "scope": "场景数据源包含非建模范围资源，无法安全永久删除",
    "owner": "场景数据源仍归属于 Agent，无法由场景删除",
    "external": "场景数据源或文件仍被其他场景或平台资源引用，无法永久删除",
    "index": "场景数据源仍有进行中的文档索引任务，请等待任务结束后重试",
    "template": "场景数据源仍被模板或发布快照引用，无法永久删除",
}


@dataclass(frozen=True)
class ScenarioPurgeSourceState:
    """Locked or previewed scenario assets and their deletion blockers."""

    sources: tuple[DataSource, ...]
    files: tuple[BucketFile, ...]
    blockers: tuple[str, ...]


def _exists(db: Session, statement: Any) -> bool:
    return db.scalar(statement.limit(1)) is not None


def _has_cross_scenario_source_reference(
    db: Session,
    scenario: BusinessScenario,
    source_id: str,
) -> bool:
    tenant_id = scenario.tenant_id
    mapping_reference = (
        select(DataMapping.id)
        .outerjoin(BusinessScenario, BusinessScenario.id == DataMapping.scenario_id)
        .where(
            DataMapping.data_source_id == source_id,
            or_(
                BusinessScenario.id.is_(None),
                BusinessScenario.tenant_id != tenant_id,
                DataMapping.scenario_id != scenario.id,
            ),
        )
    )
    relation_mapping_reference = (
        select(RelationDataMapping.id)
        .outerjoin(
            BusinessScenario,
            BusinessScenario.id == RelationDataMapping.scenario_id,
        )
        .where(
            RelationDataMapping.data_source_id == source_id,
            or_(
                BusinessScenario.id.is_(None),
                BusinessScenario.tenant_id != tenant_id,
                RelationDataMapping.scenario_id != scenario.id,
            ),
        )
    )
    connector_reference = select(ConnectorBinding.id).where(
        ConnectorBinding.connector_kind == "data_source",
        ConnectorBinding.connector_id == source_id,
        or_(
            ConnectorBinding.tenant_id != tenant_id,
            ConnectorBinding.scenario_id != scenario.id,
        ),
    )
    return any(
        _exists(db, statement)
        for statement in (
            mapping_reference,
            relation_mapping_reference,
            connector_reference,
        )
    )


def _catalog_source_reference_statements(
    source_id: str,
) -> tuple[Any, ...]:
    return (
        select(DataAssetVersion.id).where(
            DataAssetVersion.bucket_data_source_id == source_id,
        ),
        select(DatasetVersionAsset.id)
        .select_from(DatasetVersionAsset)
        .join(
            DataAssetVersion,
            DataAssetVersion.id == DatasetVersionAsset.asset_version_id,
        )
        .where(
            DataAssetVersion.bucket_data_source_id == source_id,
        ),
        select(IngestionRunInput.id)
        .select_from(IngestionRunInput)
        .join(
            DataAssetVersion,
            DataAssetVersion.id == IngestionRunInput.asset_version_id,
        )
        .where(
            DataAssetVersion.bucket_data_source_id == source_id,
        ),
        select(RunInputBinding.id)
        .select_from(RunInputBinding)
        .join(
            DataAssetVersion,
            DataAssetVersion.id == RunInputBinding.asset_version_id,
        )
        .where(
            DataAssetVersion.bucket_data_source_id == source_id,
        ),
        select(DatasetVersion.id).where(
            DatasetVersion.manifest_data_source_id == source_id,
        ),
        select(DatasetFragment.id).where(
            DatasetFragment.bucket_data_source_id == source_id,
        ),
        select(IngestionRun.id).where(
            IngestionRun.trace_data_source_id == source_id,
        ),
        select(DerivationRun.id).where(
            DerivationRun.trace_data_source_id == source_id,
        ),
        select(DerivationEvidence.id).where(
            DerivationEvidence.document_data_source_id == source_id,
        ),
        select(DerivationEvidence.id)
        .select_from(DerivationEvidence)
        .join(DocumentChunk, DocumentChunk.id == DerivationEvidence.document_chunk_id)
        .where(
            DocumentChunk.data_source_id == source_id,
        ),
    )


def _has_upload_source_reference(
    db: Session,
    source: DataSource,
    *,
    scenario_agent_ids: set[str],
) -> bool:
    upload_statement = select(ManagedUploadRun.id).where(
        ManagedUploadRun.data_source_id == source.id,
    )
    if source.resource_scope == "agent_runtime" and scenario_agent_ids:
        upload_statement = upload_statement.where(
            or_(
                ManagedUploadRun.tenant_id != source.tenant_id,
                ManagedUploadRun.owner_agent_id.is_(None),
                ~ManagedUploadRun.owner_agent_id.in_(sorted(scenario_agent_ids)),
            )
        )
    return _exists(db, upload_statement)


def _source_reference_kinds(
    db: Session,
    scenario: BusinessScenario,
    source: DataSource,
    *,
    scenario_agent_ids: set[str],
    check_template_refs: bool,
) -> set[str]:
    """Find upward references that make a scenario source non-removable."""

    kinds: set[str] = set()
    if _has_cross_scenario_source_reference(db, scenario, source.id):
        kinds.add("external")
    # Every BucketFile for an accepted file bucket is locked below and passed
    # to the governed catalog-detach operation before metadata deletion. The
    # immutable catalog rows therefore remain as retired evidence instead of
    # turning an owned physical payload into an external blocker.
    if source.type != "file_bucket" and any(
        _exists(db, statement)
        for statement in _catalog_source_reference_statements(
            source.id,
        )
    ):
        kinds.add("external")
    if _has_upload_source_reference(
        db,
        source,
        scenario_agent_ids=scenario_agent_ids,
    ):
        kinds.add("external")
    if _exists(
        db,
        select(DocumentIndexJob.id).where(
            DocumentIndexJob.data_source_id == source.id,
            DocumentIndexJob.tenant_id == scenario.tenant_id,
            DocumentIndexJob.status.in_(_ACTIVE_INDEX_STATUSES),
        ),
    ):
        kinds.add("index")
    if check_template_refs:
        try:
            template_catalog_service.assert_data_source_not_registered(
                db,
                source.id,
                exclude_scenario_id=scenario.id,
            )
        except template_catalog_service.TemplateCatalogError:
            kinds.add("template")
    return kinds


def _has_template_file_reference(
    db: Session,
    scenario: BusinessScenario,
    file_id: str,
) -> bool:
    return _exists(
        db,
        select(ArtifactTemplateVersion.id)
        .outerjoin(
            ArtifactTemplate,
            ArtifactTemplate.id == ArtifactTemplateVersion.template_id,
        )
        .where(
            ArtifactTemplateVersion.bucket_file_id == file_id,
            or_(
                ArtifactTemplate.id.is_(None),
                ArtifactTemplate.tenant_id != scenario.tenant_id,
                ArtifactTemplate.scenario_id.is_(None),
                ArtifactTemplate.scenario_id != scenario.id,
            ),
        ),
    )


def _protected_file_reference_statements(
    file_id: str,
) -> tuple[Any, ...]:
    return (
        select(WorkflowApprovalEvidence.id).where(
            WorkflowApprovalEvidence.bucket_file_id == file_id,
        ),
        select(ManagedUploadRun.id).where(
            ManagedUploadRun.bucket_file_id == file_id,
        ),
    )


def _dataset_is_downward_owned(
    dataset: LogicalDataset | None,
    scenario: BusinessScenario,
    source: DataSource,
    bucket_file: BucketFile,
    *,
    scenario_agent_ids: set[str],
) -> bool:
    """Prove that a catalog package is a child of the purged scenario.

    Dataset rows deliberately have no scenario foreign key.  Their labels are
    therefore only accepted when they carry the platform-generated ownership
    contract.  An unrecognised or incomplete package is treated as shared so
    the physical-file detach function can never rewrite it accidentally.
    """

    if dataset is None or dataset.tenant_id != scenario.tenant_id:
        return False
    labels = dataset.labels if isinstance(dataset.labels, dict) else {}
    purpose = str(labels.get("catalog_purpose") or "")
    if (
        purpose == modeling_contract_source_service.MODELING_CONTRACT_SOURCE_PURPOSE
        and dataset.usage_plane == "modeling_material"
        and labels.get("modeling_source_data_source_id") == source.id
    ):
        source_scenario_id = str(labels.get("modeling_source_scenario_id") or "")
        file_id = str(labels.get("modeling_source_bucket_file_id") or "")
        return source_scenario_id in ("", str(scenario.id)) and file_id in (
            "",
            str(bucket_file.id),
        )
    if purpose == "validation_dataset" and dataset.usage_plane == "invocation_input":
        owner_agent_id = str(labels.get("owner_agent_id") or "")
        return bool(owner_agent_id) and owner_agent_id in scenario_agent_ids
    return False


def _has_external_asset_reference(
    db: Session,
    scenario: BusinessScenario,
    source: DataSource,
    bucket_file: BucketFile,
    *,
    scenario_agent_ids: set[str],
) -> bool:
    """Return true when an asset/version cannot be proven scenario-owned."""

    rows = db.execute(
        select(
            DataAssetVersion.tenant_id,
            DataAssetVersion.asset_id,
            DataAsset.tenant_id,
            DataAsset.owner_agent_id,
            DataAsset.usage_plane,
            DataAsset.labels,
        )
        .select_from(DataAssetVersion)
        .outerjoin(DataAsset, DataAsset.id == DataAssetVersion.asset_id)
        .where(
            DataAssetVersion.bucket_file_id == bucket_file.id,
            DataAssetVersion.bucket_data_source_id == source.id,
        )
    ).all()
    for (
        tenant_id,
        _asset_id,
        asset_tenant_id,
        owner_agent_id,
        usage_plane,
        labels,
    ) in rows:
        if tenant_id != scenario.tenant_id or asset_tenant_id != scenario.tenant_id:
            return True
        if owner_agent_id:
            owner = db.scalar(
                select(Agent).where(
                    Agent.id == owner_agent_id,
                    Agent.tenant_id == scenario.tenant_id,
                )
            )
            if owner is None or owner.scenario_id != scenario.id:
                return True
            continue
        # Legacy ownerless modeling assets are intentionally retained as
        # catalog identities and detached with the source.  Any explicit
        # scope marker that disagrees with this scenario turns the row into a
        # fail-closed external reference.
        if usage_plane != "modeling_material":
            return True
        scope_labels = labels if isinstance(labels, dict) else {}
        label_owner = str(scope_labels.get("owner_agent_id") or "")
        label_scenario = str(scope_labels.get("scenario_id") or "")
        if label_owner and label_owner not in scenario_agent_ids:
            return True
        if label_scenario and label_scenario != str(scenario.id):
            return True
        label_source = str(
            scope_labels.get("modeling_source_data_source_id") or ""
        )
        label_file = str(scope_labels.get("modeling_source_bucket_file_id") or "")
        if label_source and label_source != str(source.id):
            return True
        if label_file and label_file != str(bucket_file.id):
            return True
    return False


def _has_external_catalog_file_reference(
    db: Session,
    scenario: BusinessScenario,
    source: DataSource,
    bucket_file: BucketFile,
    *,
    scenario_agent_ids: set[str],
) -> bool:
    """Prove every physical-file catalog/lineage reference is downward.

    ``detach_platform_catalog_references_for_deletion`` operates by physical
    ``(source_id, file_id)`` and consequently has no knowledge of Agent or
    Dataset ownership.  This guard is deliberately conservative: a row is
    allowed only when its complete parent chain is demonstrably owned by the
    scenario; unknown and malformed chains remain protected.
    """

    tenant_id = scenario.tenant_id
    file_id = str(bucket_file.id)
    source_id = str(source.id)
    dataset_cache: dict[str, bool] = {}

    def dataset_owned(dataset_id: str | None) -> bool:
        key = str(dataset_id or "")
        if not key:
            return False
        if key not in dataset_cache:
            dataset_cache[key] = _dataset_is_downward_owned(
                db,
                db.scalar(
                    select(LogicalDataset).where(
                        LogicalDataset.id == key,
                        LogicalDataset.tenant_id == tenant_id,
                    )
                ),
                scenario,
                source,
                bucket_file,
                scenario_agent_ids=scenario_agent_ids,
            )
        return dataset_cache[key]

    if _has_external_asset_reference(
        db,
        scenario,
        source,
        bucket_file,
        scenario_agent_ids=scenario_agent_ids,
    ):
        return True

    # A DatasetVersionAsset pins the immutable version into a dataset.  The
    # target scenario may retire its own validation/contract package, but an
    # ordinary or other-Agent package is an upward reference.
    asset_dataset_ids = db.scalars(
        select(DatasetVersion.dataset_id)
        .select_from(DatasetVersionAsset)
        .join(
            DataAssetVersion,
            DataAssetVersion.id == DatasetVersionAsset.asset_version_id,
        )
        .join(DatasetVersion, DatasetVersion.id == DatasetVersionAsset.dataset_version_id)
        .where(
            DataAssetVersion.bucket_file_id == file_id,
            DataAssetVersion.bucket_data_source_id == source_id,
        )
    ).all()
    if any(not dataset_owned(value) for value in asset_dataset_ids):
        return True

    manifest_dataset_ids = db.scalars(
        select(DatasetVersion.dataset_id).where(
            DatasetVersion.manifest_bucket_file_id == file_id,
            DatasetVersion.manifest_data_source_id == source_id,
        )
    ).all()
    if any(not dataset_owned(value) for value in manifest_dataset_ids):
        return True

    fragment_dataset_ids = db.scalars(
        select(DatasetVersion.dataset_id)
        .select_from(DatasetFragment)
        .outerjoin(
            DatasetVersion,
            DatasetVersion.id == DatasetFragment.dataset_version_id,
        )
        .where(
            DatasetFragment.bucket_file_id == file_id,
            DatasetFragment.bucket_data_source_id == source_id,
        )
    ).all()
    if any(not dataset_owned(value) for value in fragment_dataset_ids):
        return True

    # Ingestion traces have no scenario column; their dataset is the only
    # durable ownership proof.  A missing dataset is therefore external.
    ingestion_dataset_ids = db.scalars(
        select(IngestionRun.dataset_id).where(
            IngestionRun.trace_bucket_file_id == file_id,
            IngestionRun.trace_data_source_id == source_id,
        )
    ).all()
    if any(not dataset_owned(value) for value in ingestion_dataset_ids):
        return True

    # Derivation runs and invocation bindings carry an explicit scenario
    # scope, so only the target scenario can be cleaned as a child.
    derivation_scopes = db.execute(
        select(DerivationRun.tenant_id, DerivationRun.scenario_id).where(
            DerivationRun.trace_bucket_file_id == file_id,
            DerivationRun.trace_data_source_id == source_id,
        )
    ).all()
    if any(
        tenant != tenant_id or scenario_id != scenario.id
        for tenant, scenario_id in derivation_scopes
    ):
        return True

    ingestion_input_datasets = db.scalars(
        select(IngestionRun.dataset_id)
        .select_from(IngestionRunInput)
        .join(
            DataAssetVersion,
            DataAssetVersion.id == IngestionRunInput.asset_version_id,
        )
        .join(IngestionRun, IngestionRun.id == IngestionRunInput.ingestion_run_id)
        .where(
            DataAssetVersion.bucket_file_id == file_id,
            DataAssetVersion.bucket_data_source_id == source_id,
        )
    ).all()
    if any(not dataset_owned(value) for value in ingestion_input_datasets):
        return True

    invocation_scopes = db.execute(
        select(RunInputBinding.tenant_id, RunInputBinding.scenario_id)
        .where(
            RunInputBinding.asset_version_id.in_(
                select(DataAssetVersion.id).where(
                    DataAssetVersion.bucket_file_id == file_id,
                    DataAssetVersion.bucket_data_source_id == source_id,
                )
            )
        )
    ).all()
    if any(
        tenant != tenant_id or scenario_id != scenario.id
        for tenant, scenario_id in invocation_scopes
    ):
        return True

    approval_scopes = db.execute(
        select(WorkflowApprovalEvidence.tenant_id, WorkflowApprovalEvidence.scenario_id)
        .where(WorkflowApprovalEvidence.bucket_file_id == file_id)
    ).all()
    if any(
        tenant != tenant_id or scenario_id != scenario.id
        for tenant, scenario_id in approval_scopes
    ):
        return True

    # Document chunks are source-owned, but evidence may be an independent
    # scenario's durable assertion.  Only evidence with an explicit target
    # scenario/run/dataset chain is safe to remove with this scenario.
    chunk_ids = db.scalars(
        select(DocumentChunk.id).where(
            DocumentChunk.bucket_file_id == file_id,
            DocumentChunk.data_source_id == source_id,
        )
    ).all()
    if chunk_ids:
        evidence_rows = list(
            db.scalars(
                select(DerivationEvidence).where(
                    DerivationEvidence.document_chunk_id.in_(sorted(chunk_ids))
                )
            ).all()
        )
        for evidence in evidence_rows:
            if evidence.tenant_id != tenant_id:
                return True
            if evidence.action_scenario_id is not None:
                if evidence.action_scenario_id != scenario.id:
                    return True
                continue
            if evidence.derivation_run_id:
                run = db.scalar(
                    select(DerivationRun).where(
                        DerivationRun.id == evidence.derivation_run_id,
                        DerivationRun.tenant_id == tenant_id,
                    )
                )
                if run is None or run.scenario_id != scenario.id:
                    return True
                continue
            if evidence.dataset_version_id:
                version = db.scalar(
                    select(DatasetVersion).where(
                        DatasetVersion.id == evidence.dataset_version_id,
                        DatasetVersion.tenant_id == tenant_id,
                    )
                )
                if version is None or not dataset_owned(version.dataset_id):
                    return True
                continue
            # An unscoped/external locator cannot be proven to be a child.
            return True

    # The same MinIO identity can be represented by another BucketFile row.
    # Deleting this row would enqueue an object delete that invalidates the
    # other owner, even when all database FK checks pass.
    if not bucket_file.bucket_name or not bucket_file.object_key:
        return True
    duplicate_predicate = (
        (BucketFile.storage_provider == bucket_file.storage_provider)
        & (BucketFile.bucket_name == bucket_file.bucket_name)
        & (BucketFile.object_key == bucket_file.object_key)
        & (BucketFile.object_version_id == bucket_file.object_version_id)
    )
    if bucket_file.object_url:
        duplicate_predicate = duplicate_predicate | (
            BucketFile.object_url == bucket_file.object_url
        )
    if _exists(
        db,
        select(BucketFile.id).where(
            BucketFile.id != bucket_file.id,
            duplicate_predicate,
        ),
    ):
        return True
    return False


def _file_reference_kinds(
    db: Session,
    scenario: BusinessScenario,
    bucket_file: BucketFile,
    *,
    scenario_agent_ids: set[str],
) -> set[str]:
    """Return platform references that prohibit deleting one source file."""

    kinds: set[str] = set()
    if _has_template_file_reference(db, scenario, bucket_file.id):
        kinds.add("template")
    source = db.get(DataSource, bucket_file.data_source_id)
    if source is None or source.tenant_id != scenario.tenant_id:
        kinds.add("external")
    elif _has_external_catalog_file_reference(
        db,
        scenario,
        source,
        bucket_file,
        scenario_agent_ids=scenario_agent_ids,
    ):
        kinds.add("external")
    if any(
        _exists(db, statement)
        for statement in _protected_file_reference_statements(
            bucket_file.id,
        )
    ):
        kinds.add("external")
    if _exists(
        db,
        select(DocumentIndexJob.id).where(
            DocumentIndexJob.bucket_file_id == bucket_file.id,
            DocumentIndexJob.tenant_id == scenario.tenant_id,
            DocumentIndexJob.status.in_(_ACTIVE_INDEX_STATUSES),
        ),
    ):
        kinds.add("index")
    return kinds


def _load_scenario_sources(
    db: Session,
    scenario: BusinessScenario,
    *,
    lock: bool,
) -> list[DataSource]:
    statement = (
        select(DataSource)
        .where(
            DataSource.tenant_id == scenario.tenant_id,
            DataSource.scenario_id == scenario.id,
        )
        .order_by(DataSource.id)
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    return list(db.scalars(statement).all())


def _load_source_files(
    db: Session,
    scenario: BusinessScenario,
    sources: list[DataSource],
    *,
    lock: bool,
) -> list[BucketFile]:
    source_ids = [source.id for source in sources]
    if not source_ids:
        return []
    statement = (
        select(BucketFile)
        .join(DataSource, DataSource.id == BucketFile.data_source_id)
        .where(
            BucketFile.data_source_id.in_(source_ids),
            DataSource.tenant_id == scenario.tenant_id,
            DataSource.scenario_id == scenario.id,
            DataSource.resource_scope == "modeling",
            DataSource.owner_agent_id.is_(None),
        )
        .order_by(BucketFile.id)
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    return list(db.scalars(statement).all())


def inspect_scenario_sources(
    db: Session,
    scenario: BusinessScenario,
    *,
    lock: bool = False,
    check_template_refs: bool = False,
) -> ScenarioPurgeSourceState:
    """Load deletable modeling sources and fail-closed blocker messages."""

    sources = _load_scenario_sources(db, scenario, lock=lock)
    scenario_agent_ids = {
        str(value)
        for value in db.scalars(
            select(Agent.id).where(
                Agent.tenant_id == scenario.tenant_id,
                Agent.scenario_id == scenario.id,
            )
        ).all()
    }
    deletable: list[DataSource] = []
    for source in sources:
        if source.resource_scope != "modeling":
            is_owned_runtime_source = (
                source.resource_scope == "agent_runtime"
                and source.owner_agent_id is not None
                and str(source.owner_agent_id) in scenario_agent_ids
            )
            if not is_owned_runtime_source:
                continue
        if source.owner_agent_id is not None:
            continue
        reference_kinds = _source_reference_kinds(
            db,
            scenario,
            source,
            scenario_agent_ids=scenario_agent_ids,
            check_template_refs=check_template_refs,
        )
        if reference_kinds:
            continue
        deletable.append(source)

    files = _load_source_files(db, scenario, deletable, lock=lock)
    protected_source_ids: set[str] = set()
    for bucket_file in files:
        file_kinds = _file_reference_kinds(
            db,
            scenario,
            bucket_file,
            scenario_agent_ids=scenario_agent_ids,
        )
        if file_kinds:
            protected_source_ids.add(bucket_file.data_source_id)
    if protected_source_ids:
        deletable = [
            source for source in deletable if source.id not in protected_source_ids
        ]
        files = [
            bucket_file
            for bucket_file in files
            if bucket_file.data_source_id not in protected_source_ids
        ]

    return ScenarioPurgeSourceState(tuple(deletable), tuple(files), ())


def detach_cleaned_runtime_sources(
    db: Session,
    scenario: BusinessScenario,
    source_ids: set[str],
) -> set[str]:
    """Detach runtime sources fenced and redacted by Agent owner cleanup."""

    if not source_ids:
        return set()
    rows = list(
        db.scalars(
            select(DataSource)
            .where(
                DataSource.id.in_(sorted(source_ids)),
                DataSource.tenant_id == scenario.tenant_id,
                DataSource.scenario_id == scenario.id,
                DataSource.resource_scope == "agent_runtime",
            )
            .with_for_update()
        ).all()
    )
    detached: set[str] = set()
    for source in rows:
        if (
            source.owner_agent_id is None
            and source.status == "error"
            and source.last_error == "所属 Agent 已删除"
            and dict(source.config or {}) == {}
        ):
            source.scenario_id = None
            detached.add(str(source.id))
    if detached:
        db.flush()
    return detached


def enqueue_source_file_deletions(
    db: Session,
    state: ScenarioPurgeSourceState,
) -> list[str]:
    """Persist deletion intents before source file metadata is removed."""

    source_by_id = {source.id: source for source in state.sources}
    return [
        object_deletion_service.enqueue_bucket_file_deletion(
            db,
            bucket_file,
            source_by_id[bucket_file.data_source_id],
        )
        for bucket_file in state.files
    ]


def delete_scenario_sources(
    db: Session,
    scenario: BusinessScenario,
    state: ScenarioPurgeSourceState,
) -> None:
    """Remove only the locked modeling sources accepted by the asset scan."""

    source_ids = [source.id for source in state.sources]
    for source in state.sources:
        modeling_contract_source_service.retire_for_data_source_deletion(db, source)
        source_file_ids = [
            bucket_file.id
            for bucket_file in state.files
            if bucket_file.data_source_id == source.id
        ]
        if source_file_ids:
            datasource_service.detach_platform_catalog_references_for_deletion(
                db,
                source,
                source_file_ids,
            )
    if not source_ids:
        return
    # The detach service updates retained ORM rows and may stage dependent
    # evidence/fragment deletes. Flush those changes before the bulk SQL below
    # removes the RESTRICT-protected file and source rows.
    db.flush()
    db.execute(
        delete(ConnectorBinding).where(
            ConnectorBinding.tenant_id == scenario.tenant_id,
            ConnectorBinding.scenario_id == scenario.id,
            ConnectorBinding.connector_kind == "data_source",
            ConnectorBinding.connector_id.in_(source_ids),
        )
    )
    if state.files:
        db.execute(
            delete(BucketFile).where(
                BucketFile.id.in_([item.id for item in state.files]),
                BucketFile.data_source_id.in_(source_ids),
            )
        )
    db.execute(
        delete(DataSource).where(
            DataSource.id.in_(source_ids),
            DataSource.scenario_id == scenario.id,
            DataSource.tenant_id == scenario.tenant_id,
            DataSource.resource_scope == "modeling",
            DataSource.owner_agent_id.is_(None),
        )
    )
