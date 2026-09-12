"""Reference and lock checks used by Agent-owned resource deletion.

These helpers only determine whether an Agent-owned source or file can be
removed safely. The caller remains responsible for lifecycle transitions,
outbox creation, deletion, and the outer transaction.
"""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..approval_models import WorkflowApprovalEvidence
from ..models import (
    Agent,
    ArtifactTemplateVersion,
    BucketFile,
    ConnectorBinding,
    DataAsset,
    DataAssetVersion,
    DataMapping,
    DataSource,
    DatasetFragment,
    DatasetHead,
    DatasetLineageEdge,
    DatasetVersion,
    DatasetVersionAsset,
    DerivationEvidence,
    DerivationRun,
    DerivationRunInput,
    DocumentChunk,
    DocumentIndexJob,
    IngestionRun,
    IngestionRunInput,
    ManagedUploadRun,
    RelationDataMapping,
    RunInputBinding,
    ScenarioCapabilityPort,
    ScenarioDatasetBinding,
    SemanticFieldMapping,
    SemanticMapping,
    SemanticRelationMapping,
    ServingProjection,
)
from . import template_catalog_service


def generated_file_has_external_reference(
    db: Session,
    *,
    file: BucketFile,
    agent: Agent,
    owned_dataset_ids: set[str],
    owned_dataset_version_ids: set[str],
    owned_asset_ids: set[str],
    owned_run_ids: set[str],
) -> bool:
    """Conservatively retain a generated object if any upward row uses it."""
    tenant_id = agent.tenant_id
    file_id = str(file.id)

    fragment_dataset_ids = set(
        str(value)
        for value in db.scalars(
            select(DatasetFragment.dataset_id).where(
                DatasetFragment.id.is_not(None),
                DatasetFragment.bucket_file_id == file_id,
                DatasetFragment.tenant_id == tenant_id,
            )
        ).all()
    )
    if fragment_dataset_ids - owned_dataset_ids:
        return True

    manifest_dataset_ids = set(
        str(value)
        for value in db.scalars(
            select(DatasetVersion.dataset_id).where(
                DatasetVersion.manifest_bucket_file_id == file_id,
                DatasetVersion.tenant_id == tenant_id,
            )
        ).all()
    )
    if manifest_dataset_ids - owned_dataset_ids:
        return True

    asset_rows = db.execute(
        select(DataAssetVersion.asset_id, DataAsset.owner_agent_id)
        .join(DataAsset, DataAsset.id == DataAssetVersion.asset_id)
        .where(
            DataAssetVersion.bucket_file_id == file_id,
            DataAssetVersion.tenant_id == tenant_id,
            DataAsset.tenant_id == tenant_id,
        )
    ).all()
    if any(
        str(asset_id) not in owned_asset_ids or str(owner or "") != str(agent.id)
        for asset_id, owner in asset_rows
    ):
        return True

    # These rows pin the immutable asset version directly (without a
    # DatasetVersion hop).  Retiring the version or deleting its physical
    # file would invalidate an invocation/ingestion/approval record, so the
    # object must remain protected even when the referencing Agent owns it.
    if owned_asset_ids:
        owned_asset_version_ids = select(DataAssetVersion.id).where(
            DataAssetVersion.tenant_id == tenant_id,
            DataAssetVersion.asset_id.in_(sorted(owned_asset_ids)),
            DataAssetVersion.bucket_file_id == file_id,
        )
        direct_asset_version_references = (
            select(IngestionRunInput.id).where(
                IngestionRunInput.tenant_id == tenant_id,
                IngestionRunInput.asset_version_id.in_(owned_asset_version_ids),
            ),
            select(RunInputBinding.id).where(
                RunInputBinding.tenant_id == tenant_id,
                RunInputBinding.asset_version_id.in_(owned_asset_version_ids),
            ),
            select(WorkflowApprovalEvidence.id).where(
                WorkflowApprovalEvidence.tenant_id == tenant_id,
                WorkflowApprovalEvidence.asset_version_id.in_(
                    owned_asset_version_ids
                ),
            ),
        )
        if any(
            db.scalar(statement.limit(1)) is not None
            for statement in direct_asset_version_references
        ):
            return True

    run_rows = db.execute(
        select(ManagedUploadRun.id, ManagedUploadRun.owner_agent_id).where(
            ManagedUploadRun.bucket_file_id == file_id,
            ManagedUploadRun.tenant_id == tenant_id,
        )
    ).all()
    if any(
        str(run_id) not in owned_run_ids or str(owner or "") != str(agent.id)
        for run_id, owner in run_rows
    ):
        return True

    # A generated version can be selected by another scenario, invocation or
    # derivation.  Such references are independent of the Agent lifecycle.
    if owned_dataset_version_ids:
        if db.scalar(
            select(DatasetVersionAsset.id).where(
                DatasetVersionAsset.tenant_id == tenant_id,
                DatasetVersionAsset.dataset_version_id.in_(sorted(owned_dataset_version_ids)),
                DatasetVersionAsset.dataset_id.not_in(sorted(owned_dataset_ids)),
            ).limit(1)
        ):
            return True
        if db.scalar(
            select(RunInputBinding.id).where(
                RunInputBinding.tenant_id == tenant_id,
                (
                    (RunInputBinding.source_dataset_version_id.in_(sorted(owned_dataset_version_ids)))
                    | (RunInputBinding.resolved_dataset_version_id.in_(sorted(owned_dataset_version_ids)))
                ),
            ).limit(1)
        ):
            return True
        if db.scalar(
            select(DerivationRunInput.id).where(
                DerivationRunInput.tenant_id == tenant_id,
                DerivationRunInput.dataset_version_id.in_(sorted(owned_dataset_version_ids)),
            ).limit(1)
        ):
            return True
        if db.scalar(
            select(DerivationEvidence.id).where(
                DerivationEvidence.tenant_id == tenant_id,
                DerivationEvidence.dataset_version_id.in_(sorted(owned_dataset_version_ids)),
            ).limit(1)
        ):
            return True
        if db.scalar(
            select(DatasetLineageEdge.id).where(
                DatasetLineageEdge.tenant_id == tenant_id,
                (
                    (DatasetLineageEdge.upstream_version_id.in_(sorted(owned_dataset_version_ids)))
                    | (DatasetLineageEdge.downstream_version_id.in_(sorted(owned_dataset_version_ids)))
                ),
            ).limit(1)
        ):
            return True
        if db.scalar(
            select(ServingProjection.id).where(
                ServingProjection.tenant_id == tenant_id,
                ServingProjection.dataset_version_id.in_(sorted(owned_dataset_version_ids)),
            ).limit(1)
        ):
            return True
        if db.scalar(
            select(DatasetHead.id).where(
                DatasetHead.tenant_id == tenant_id,
                DatasetHead.dataset_version_id.in_(sorted(owned_dataset_version_ids)),
            ).limit(1)
        ):
            return True
        if db.scalar(
            select(IngestionRunInput.id).where(
                IngestionRunInput.tenant_id == tenant_id,
                IngestionRunInput.dataset_version_id.in_(
                    sorted(owned_dataset_version_ids)
                ),
            ).limit(1)
        ):
            return True
        if db.scalar(
            select(WorkflowApprovalEvidence.id).where(
                WorkflowApprovalEvidence.tenant_id == tenant_id,
                WorkflowApprovalEvidence.dataset_version_id.in_(
                    sorted(owned_dataset_version_ids)
                ),
            ).limit(1)
        ):
            return True
        if db.scalar(
            select(IngestionRun.id).where(
                IngestionRun.tenant_id == tenant_id,
                IngestionRun.output_version_id.in_(
                    sorted(owned_dataset_version_ids)
                ),
            ).limit(1)
        ):
            return True

    if db.scalar(
        select(ScenarioDatasetBinding.id).where(
            ScenarioDatasetBinding.tenant_id == tenant_id,
            ScenarioDatasetBinding.dataset_id.in_(sorted(owned_dataset_ids)),
        ).limit(1)
    ):
        return True
    if db.scalar(
        select(ScenarioCapabilityPort.id).where(
            ScenarioCapabilityPort.tenant_id == tenant_id,
            ScenarioCapabilityPort.dataset_id.in_(sorted(owned_dataset_ids)),
        ).limit(1)
    ):
        return True
    if db.scalar(
        select(SemanticMapping.id).where(
            SemanticMapping.tenant_id == tenant_id,
            SemanticMapping.dataset_id.in_(sorted(owned_dataset_ids)),
        ).limit(1)
    ):
        return True
    if db.scalar(
        select(SemanticFieldMapping.id).where(
            SemanticFieldMapping.tenant_id == tenant_id,
            SemanticFieldMapping.dataset_id.in_(sorted(owned_dataset_ids)),
        ).limit(1)
    ):
        return True
    if db.scalar(
        select(SemanticRelationMapping.id).where(
            SemanticRelationMapping.tenant_id == tenant_id,
            SemanticRelationMapping.dataset_id.in_(sorted(owned_dataset_ids)),
        ).limit(1)
    ):
        return True

    # Direct physical references and content-addressed duplicates are always
    # retained unless the row is proven private to this package.
    direct_checks = (
        select(DocumentChunk.id)
        .join(DataSource, DataSource.id == DocumentChunk.data_source_id)
        .where(
            DataSource.tenant_id == tenant_id,
            DocumentChunk.bucket_file_id == file_id,
        ),
        select(DocumentIndexJob.id).where(
            DocumentIndexJob.tenant_id == tenant_id,
            DocumentIndexJob.bucket_file_id == file_id,
        ),
        select(IngestionRun.id).where(
            IngestionRun.tenant_id == tenant_id,
            IngestionRun.trace_bucket_file_id == file_id,
            or_(
                IngestionRun.dataset_id.is_(None),
                ~IngestionRun.dataset_id.in_(sorted(owned_dataset_ids)),
            ),
        ),
        select(DerivationRun.id).where(
            DerivationRun.tenant_id == tenant_id,
            DerivationRun.trace_bucket_file_id == file_id,
        ),
        select(ArtifactTemplateVersion.id).where(
            ArtifactTemplateVersion.bucket_file_id == file_id,
        ),
        select(WorkflowApprovalEvidence.id).where(
            WorkflowApprovalEvidence.bucket_file_id == file_id,
        ),
    )
    if any(db.scalar(statement.limit(1)) is not None for statement in direct_checks):
        return True

    duplicate = db.scalar(
        select(BucketFile.id)
        .where(
            BucketFile.id != file.id,
            BucketFile.storage_provider == file.storage_provider,
            BucketFile.bucket_name == file.bucket_name,
            BucketFile.object_key == file.object_key,
            BucketFile.object_version_id == file.object_version_id,
        )
        .limit(1)
    )
    return duplicate is not None


def lock_files(
    db: Session,
    *,
    file_ids: set[str],
    tenant_id: str,
) -> list[BucketFile]:
    if not file_ids:
        return []
    return list(
        db.scalars(
            select(BucketFile)
            .join(DataSource, DataSource.id == BucketFile.data_source_id)
            .where(
                BucketFile.id.in_(sorted(file_ids)),
                DataSource.tenant_id == tenant_id,
            )
            .order_by(BucketFile.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).all()
    )


def protected_file_ids(
    db: Session,
    *,
    agent: Agent,
    files: list[BucketFile],
    owned_run_ids: set[str],
    owned_asset_ids: set[str],
    owned_version_ids: set[str],
    owned_dataset_ids: set[str] | None = None,
) -> set[str]:
    """Return files that another owner can still reach.

    The physical upload bucket is tenant shared. A file is removable only if
    no other upload run, catalog asset, template, approval evidence, dataset,
    or durable input/run record points at it. Dataset and run references are
    intentionally treated as upward references: deleting an Agent must not
    retire or rewrite an independently governed dataset or audit record.
    """

    candidate_ids = {str(item.id) for item in files}
    if not candidate_ids:
        return set()
    protected: set[str] = set()
    owned_dataset_ids = set(owned_dataset_ids or ())

    run_statement = select(ManagedUploadRun.bucket_file_id).where(
        ManagedUploadRun.tenant_id == agent.tenant_id,
        ManagedUploadRun.bucket_file_id.in_(sorted(candidate_ids)),
    )
    if owned_run_ids:
        run_statement = run_statement.where(~ManagedUploadRun.id.in_(sorted(owned_run_ids)))
    protected.update(
        str(value) for value in db.scalars(run_statement).all() if value
    )

    # A retry/upload row can retain only an asset/version pointer after its
    # physical upload column has been cleared.  Protect the version's object
    # when that pointer belongs to another run; otherwise a later cleanup could
    # remove an object still addressable through the retained run audit row.
    if owned_version_ids:
        external_run_versions = (
            select(DataAssetVersion.bucket_file_id)
            .join(
                ManagedUploadRun,
                ManagedUploadRun.asset_version_id == DataAssetVersion.id,
            )
            .where(
                ManagedUploadRun.tenant_id == agent.tenant_id,
                ManagedUploadRun.asset_version_id.in_(sorted(owned_version_ids)),
                DataAssetVersion.bucket_file_id.in_(sorted(candidate_ids)),
            )
        )
        if owned_run_ids:
            external_run_versions = external_run_versions.where(
                ~ManagedUploadRun.id.in_(sorted(owned_run_ids))
            )
        protected.update(
            str(value)
            for value in db.scalars(external_run_versions).all()
            if value
        )

    asset_statement = (
        select(DataAssetVersion.bucket_file_id)
        .join(DataAsset, DataAsset.id == DataAssetVersion.asset_id)
        .where(
            DataAssetVersion.tenant_id == agent.tenant_id,
            DataAssetVersion.bucket_file_id.in_(sorted(candidate_ids)),
        )
    )
    if owned_asset_ids:
        asset_statement = asset_statement.where(
            ~DataAssetVersion.asset_id.in_(sorted(owned_asset_ids))
        )
    protected.update(
        str(value) for value in db.scalars(asset_statement).all() if value
    )

    # Immutable catalog and invocation records pin asset versions.  Their
    # owner is not the Agent being deleted, so the physical payload must stay
    # available even when the logical asset itself is retired as a tombstone.
    if owned_version_ids:
        pinned_version_statements = (
            select(DataAssetVersion.bucket_file_id)
            .join(
                DatasetVersionAsset,
                DatasetVersionAsset.asset_version_id == DataAssetVersion.id,
            )
            .where(
                DatasetVersionAsset.tenant_id == agent.tenant_id,
                DatasetVersionAsset.asset_version_id.in_(sorted(owned_version_ids)),
                DataAssetVersion.bucket_file_id.in_(sorted(candidate_ids)),
                ~DatasetVersionAsset.dataset_id.in_(sorted(owned_dataset_ids))
                if owned_dataset_ids
                else True,
            ),
            select(DataAssetVersion.bucket_file_id)
            .join(
                IngestionRunInput,
                IngestionRunInput.asset_version_id == DataAssetVersion.id,
            )
            .join(IngestionRun, IngestionRun.id == IngestionRunInput.ingestion_run_id)
            .where(
                IngestionRunInput.tenant_id == agent.tenant_id,
                IngestionRunInput.asset_version_id.in_(sorted(owned_version_ids)),
                DataAssetVersion.bucket_file_id.in_(sorted(candidate_ids)),
                ~IngestionRun.dataset_id.in_(sorted(owned_dataset_ids))
                if owned_dataset_ids
                else True,
            ),
            select(DataAssetVersion.bucket_file_id)
            .join(
                RunInputBinding,
                RunInputBinding.asset_version_id == DataAssetVersion.id,
            )
            .where(
                RunInputBinding.tenant_id == agent.tenant_id,
                RunInputBinding.asset_version_id.in_(sorted(owned_version_ids)),
                DataAssetVersion.bucket_file_id.in_(sorted(candidate_ids)),
            ),
        )
        for statement in pinned_version_statements:
            protected.update(
                str(value) for value in db.scalars(statement).all() if value
            )

    protected.update(
        str(value)
        for value in db.scalars(
            select(ArtifactTemplateVersion.bucket_file_id).where(
                ArtifactTemplateVersion.bucket_file_id.in_(sorted(candidate_ids))
            )
        ).all()
        if value
    )
    protected.update(
        str(value)
        for value in db.scalars(
            select(WorkflowApprovalEvidence.bucket_file_id).where(
                WorkflowApprovalEvidence.bucket_file_id.in_(sorted(candidate_ids))
            )
        ).all()
        if value
    )
    if owned_version_ids:
        protected.update(
            str(value)
            for value in db.scalars(
                select(DataAssetVersion.bucket_file_id)
                .join(
                    WorkflowApprovalEvidence,
                    WorkflowApprovalEvidence.asset_version_id == DataAssetVersion.id,
                )
                .where(
                    DataAssetVersion.id.in_(sorted(owned_version_ids)),
                    DataAssetVersion.bucket_file_id.in_(sorted(candidate_ids)),
                )
            ).all()
            if value
        )

    # These rows point directly at a physical upload rather than through an
    # asset version.  Keep them intact so a deleted Agent cannot invalidate a
    # dataset manifest, materialized fragment, or durable execution trace.
    direct_reference_statements = (
        select(DatasetVersion.manifest_bucket_file_id).where(
            DatasetVersion.tenant_id == agent.tenant_id,
            DatasetVersion.manifest_bucket_file_id.in_(sorted(candidate_ids)),
        ),
        select(DatasetFragment.bucket_file_id).where(
            DatasetFragment.tenant_id == agent.tenant_id,
            DatasetFragment.bucket_file_id.in_(sorted(candidate_ids)),
        ),
        select(IngestionRun.trace_bucket_file_id).where(
            IngestionRun.tenant_id == agent.tenant_id,
            IngestionRun.trace_bucket_file_id.in_(sorted(candidate_ids)),
            ~IngestionRun.dataset_id.in_(sorted(owned_dataset_ids))
            if owned_dataset_ids
            else True,
        ),
        select(DerivationRun.trace_bucket_file_id).where(
            DerivationRun.tenant_id == agent.tenant_id,
            DerivationRun.trace_bucket_file_id.in_(sorted(candidate_ids)),
        ),
        select(DocumentChunk.bucket_file_id)
        .select_from(DerivationEvidence)
        .join(DocumentChunk, DocumentChunk.id == DerivationEvidence.document_chunk_id)
        .where(
            DerivationEvidence.tenant_id == agent.tenant_id,
            DocumentChunk.bucket_file_id.in_(sorted(candidate_ids)),
        ),
        select(DocumentIndexJob.bucket_file_id).where(
            DocumentIndexJob.tenant_id == agent.tenant_id,
            DocumentIndexJob.bucket_file_id.in_(sorted(candidate_ids)),
            DocumentIndexJob.status.in_(
                ("queued", "running", "retry_waiting")
            ),
        ),
    )
    for statement in direct_reference_statements:
        protected.update(
            str(value) for value in db.scalars(statement).all() if value
        )

    # Legacy template references are JSON and therefore have no FK. Reuse the
    # template service's check and retain protected files instead of allowing
    # an opaque IntegrityError after an outbox row is created.
    for file_id in sorted(candidate_ids):
        try:
            template_catalog_service.assert_bucket_files_not_registered(db, [file_id])
        except template_catalog_service.TemplateCatalogError:
            protected.add(file_id)

    # Content-addressed rows can share an object identity even when their
    # logical catalog owners differ. Never enqueue a delete for such a key.
    for item in files:
        file_id = str(item.id)
        if not item.bucket_name or not item.object_key:
            # Legacy rows with no complete identity are retained. Guessing an
            # object path would violate the storage deletion boundary.
            protected.add(file_id)
            continue
        duplicate = db.scalar(
            select(BucketFile.id)
            .where(
                BucketFile.id != item.id,
                BucketFile.storage_provider == item.storage_provider,
                BucketFile.bucket_name == item.bucket_name,
                BucketFile.object_key == item.object_key,
                BucketFile.object_version_id == item.object_version_id,
            )
            .limit(1)
        )
        if duplicate is not None:
            protected.add(file_id)
    return protected


def source_has_external_references(
    db: Session,
    *,
    source: DataSource,
    agent: Agent,
    owned_run_ids: set[str],
    owned_asset_ids: set[str],
) -> bool:
    """Whether deleting a runtime source could remove an upward resource."""

    source_id = source.id
    if db.scalar(select(DataMapping.id).where(DataMapping.data_source_id == source_id).limit(1)):
        return True
    if db.scalar(
        select(RelationDataMapping.id)
        .where(RelationDataMapping.data_source_id == source_id)
        .limit(1)
    ):
        return True

    bindings = list(
        db.scalars(
            select(ConnectorBinding).where(
                ConnectorBinding.tenant_id == agent.tenant_id,
                ConnectorBinding.connector_kind == "data_source",
                ConnectorBinding.connector_id == source_id,
            )
        ).all()
    )
    expected_binding_key = (
        f"agent:{agent.id}:database:{source.id}"
        if agent.scenario_id
        else ""
    )
    # A binding is owned by this Agent only when both its scenario scope and
    # generated key match exactly.  A same-key row in another scenario is an
    # independent upward reference and must keep the source alive.
    if any(
        binding.scenario_id != agent.scenario_id
        or str(binding.binding_key or "") != expected_binding_key
        for binding in bindings
    ):
        return True

    run_statement = select(ManagedUploadRun.id).where(
        ManagedUploadRun.tenant_id == agent.tenant_id,
        ManagedUploadRun.data_source_id == source_id,
    )
    if owned_run_ids:
        run_statement = run_statement.where(~ManagedUploadRun.id.in_(sorted(owned_run_ids)))
    if db.scalar(run_statement.limit(1)):
        return True

    asset_source_reference = (
        select(DataAssetVersion.id)
        .join(DataAsset, DataAsset.id == DataAssetVersion.asset_id)
        .where(
            DataAssetVersion.tenant_id == agent.tenant_id,
            DataAssetVersion.bucket_data_source_id == source_id,
        )
    )
    if db.scalar(asset_source_reference.limit(1)):
        return True

    # A modeling dataset/trace that still points at this source is an
    # independently governed object. Keep the source rather than cascading
    # through its RESTRICT/CASCADE graph.
    for statement in (
        select(DatasetVersion.id).where(DatasetVersion.manifest_data_source_id == source_id),
        select(DatasetFragment.id).where(DatasetFragment.bucket_data_source_id == source_id),
        select(IngestionRun.id).where(IngestionRun.trace_data_source_id == source_id),
        select(DerivationRun.id).where(DerivationRun.trace_data_source_id == source_id),
        select(DerivationEvidence.id).where(
            DerivationEvidence.document_data_source_id == source_id
        ),
        select(DocumentIndexJob.id).where(
            DocumentIndexJob.data_source_id == source_id,
            DocumentIndexJob.status.in_(
                ("queued", "running", "retry_waiting")
            ),
        ),
        select(DatasetVersionAsset.id)
        .join(
            DataAssetVersion,
            DatasetVersionAsset.asset_version_id == DataAssetVersion.id,
        )
        .where(
            DatasetVersionAsset.tenant_id == agent.tenant_id,
            DataAssetVersion.bucket_data_source_id == source_id,
        ),
        select(IngestionRunInput.id)
        .join(
            DataAssetVersion,
            IngestionRunInput.asset_version_id == DataAssetVersion.id,
        )
        .where(
            IngestionRunInput.tenant_id == agent.tenant_id,
            DataAssetVersion.bucket_data_source_id == source_id,
        ),
        select(RunInputBinding.id)
        .join(
            DataAssetVersion,
            RunInputBinding.asset_version_id == DataAssetVersion.id,
        )
        .where(
            RunInputBinding.tenant_id == agent.tenant_id,
            DataAssetVersion.bucket_data_source_id == source_id,
        ),
    ):
        if db.scalar(statement.limit(1)):
            return True
    return False


__all__ = [
    "generated_file_has_external_reference",
    "lock_files",
    "protected_file_ids",
    "source_has_external_references",
]
