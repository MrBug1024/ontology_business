"""Downward-only lifecycle cleanup for a validation Agent.

An Agent is a mutable consumer of a scenario, not the owner of that
scenario's definition. The cleanup boundary in this module therefore only
touches rows whose ownership is explicit. Immutable catalog and execution
audit rows are retained on PostgreSQL where the runtime role is intentionally
denied ``DELETE``/``UPDATE``; their live references are detached or fenced so
a deleted Agent cannot be used to read or resume old work.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import (
    Agent,
    AgentTurnRun,
    BucketFile,
    ConnectorBinding,
    Conversation,
    DataAsset,
    DataAssetVersion,
    DataSource,
    DatasetFragment,
    DatasetVersion,
    IngestionRun,
    LogicalDataset,
    ManagedUploadRun,
    Message,
)
from . import (
    agent_deletion_reference_service,
    datasource_service,
    object_deletion_service,
    template_catalog_service,
)

# Keep the former private names available to in-repository callers while the
# implementation lives in the dedicated reference service.
_generated_file_has_external_reference = (
    agent_deletion_reference_service.generated_file_has_external_reference
)
_lock_files = agent_deletion_reference_service.lock_files
_protected_file_ids = agent_deletion_reference_service.protected_file_ids
_source_has_external_references = (
    agent_deletion_reference_service.source_has_external_references
)


ACTIVE_TURN_STATUSES = frozenset(
    {
        "accepted",
        "preparing_inputs",
        "validating_contracts",
        "planning",
        "invoking_tools",
        "responding",
        "cancel_requested",
    }
)


class AgentDeletionConflict(ValueError):
    """Deletion cannot proceed while a durable turn or shared source is live."""


@dataclass
class AgentCleanupResult:
    """Metadata needed by the caller after the DB transaction commits.

    ``*_deleted`` is kept for compatibility with the original service result.
    Immutable rows are deliberately not physically deleted by the runtime
    role, so the corresponding ``*_retired``/``*_cancelled`` counters report
    the durable lifecycle transition instead.
    """

    deletion_job_ids: list[str] = field(default_factory=list)
    conversations_deleted: int = 0
    turn_runs_deleted: int = 0
    turn_runs_detached: int = 0
    upload_runs_deleted: int = 0
    upload_runs_cancelled: int = 0
    assets_deleted: int = 0
    assets_retired: int = 0
    files_deleted: int = 0
    files_protected: int = 0
    data_sources_deleted: int = 0
    data_sources_detached: int = 0
    validation_datasets_retired: int = 0
    validation_jobs_cancelled: int = 0
    validation_dataset_files_deleted: int = 0
    validation_dataset_files_protected: int = 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lock_agent(db: Session, agent: Agent) -> Agent | None:
    return db.scalar(
        select(Agent)
        .where(Agent.id == agent.id, Agent.tenant_id == agent.tenant_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )


def _lock_upload_runs(db: Session, agent: Agent) -> list[ManagedUploadRun]:
    return list(
        db.scalars(
            select(ManagedUploadRun)
            .where(
                ManagedUploadRun.owner_agent_id == agent.id,
                ManagedUploadRun.tenant_id == agent.tenant_id,
            )
            .order_by(ManagedUploadRun.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).all()
    )


def _lock_assets(db: Session, agent: Agent) -> list[DataAsset]:
    return list(
        db.scalars(
            select(DataAsset)
            .where(
                DataAsset.owner_agent_id == agent.id,
                DataAsset.tenant_id == agent.tenant_id,
            )
            .order_by(DataAsset.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).all()
    )


def _lock_runtime_sources(db: Session, agent: Agent) -> list[DataSource]:
    return list(
        db.scalars(
            select(DataSource)
            .where(
                DataSource.owner_agent_id == agent.id,
                DataSource.tenant_id == agent.tenant_id,
                DataSource.resource_scope == "agent_runtime",
            )
            .order_by(DataSource.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).all()
    )


def _validation_dataset_owner(dataset: LogicalDataset) -> str | None:
    labels = dataset.labels if isinstance(dataset.labels, dict) else {}
    if labels.get("catalog_purpose") != "validation_dataset":
        return None
    owner = str(labels.get("owner_agent_id") or "").strip()
    return owner or None


def _lock_validation_datasets(db: Session, agent: Agent) -> list[LogicalDataset]:
    """Lock only validation data packages explicitly owned by this Agent.

    Ownership is currently carried in the generated package's labels for
    compatibility with pre-existing catalog rows.  The tenant and usage-plane
    predicates keep this scan bounded and prevent a same-id row in another
    tenant from becoming a deletion target.
    """
    rows = list(
        db.scalars(
            select(LogicalDataset)
            .where(
                LogicalDataset.tenant_id == agent.tenant_id,
                LogicalDataset.usage_plane == "invocation_input",
            )
            .order_by(LogicalDataset.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).all()
    )
    return [item for item in rows if _validation_dataset_owner(item) == str(agent.id)]


def _lock_validation_fragments(
    db: Session,
    *,
    dataset_ids: set[str],
    tenant_id: str,
) -> list[DatasetFragment]:
    if not dataset_ids:
        return []
    return list(
        db.scalars(
            select(DatasetFragment)
            .where(
                DatasetFragment.tenant_id == tenant_id,
                DatasetFragment.dataset_id.in_(sorted(dataset_ids)),
            )
            .order_by(DatasetFragment.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).all()
    )


def _cancel_validation_jobs(
    db: Session,
    *,
    dataset_ids: set[str],
    tenant_id: str,
    now: datetime,
    result: AgentCleanupResult,
) -> None:
    if not dataset_ids:
        return
    runs = list(
        db.scalars(
            select(IngestionRun)
            .where(
                IngestionRun.tenant_id == tenant_id,
                IngestionRun.dataset_id.in_(sorted(dataset_ids)),
                IngestionRun.pipeline_kind == "validation_dataset",
                IngestionRun.status.in_(("pending", "running")),
            )
            .order_by(IngestionRun.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).all()
    )
    for run in runs:
        # Clearing the lease token fences a worker that claimed this job before
        # Agent deletion acquired the row lock.  A late worker completion query
        # requires the old token and therefore becomes a no-op.
        run.status = "cancelled"
        run.error = "所属 Agent 已删除，验证数据包任务已取消"
        run.lease_token = ""
        run.lease_expires_at = None
        run.finished_at = now
        result.validation_jobs_cancelled += 1


def _mark_validation_datasets_deleted(
    datasets: list[LogicalDataset],
    *,
    agent: Agent,
    now: datetime,
    result: AgentCleanupResult,
    versions: list[DatasetVersion] | None = None,
) -> None:
    """Retire generated packages while retaining non-secret audit identity."""
    for dataset in datasets:
        labels = dict(dataset.labels or {}) if isinstance(dataset.labels, dict) else {}
        labels = {
            "catalog_purpose": "validation_dataset",
            # Keep the former owner marker so NULL (Global Assistant) cannot
            # accidentally treat a deleted Agent package as its own data.
            "owner_agent_id": str(agent.id),
            "lifecycle": "agent_deleted",
            "deleted_at": now.isoformat(),
        }
        dataset.lifecycle_status = "retired"
        dataset.retired_at = now
        dataset.name = "已删除验证附件数据源"
        dataset.description = ""
        dataset.labels = labels
    for version in versions or ():
        # Dataset versions are immutable inputs once published, but retirement
        # is the durable tombstone needed when their owning Agent disappears.
        version.status = "retired"
    result.validation_datasets_retired += len(datasets)


def _cleanup_validation_dataset_files(
    db: Session,
    *,
    agent: Agent,
    fragments: list[DatasetFragment],
    owned_dataset_ids: set[str],
    owned_dataset_version_ids: set[str],
    owned_asset_ids: set[str],
    owned_run_ids: set[str],
    result: AgentCleanupResult,
) -> None:
    file_ids = {str(item.bucket_file_id) for item in fragments if item.bucket_file_id}
    if not file_ids:
        return
    files = _lock_files(db, file_ids=file_ids, tenant_id=agent.tenant_id)
    for file in files:
        if not file.bucket_name or not file.object_key:
            result.validation_dataset_files_protected += 1
            continue
        source = db.get(DataSource, file.data_source_id)
        if source is None or source.tenant_id != agent.tenant_id or source.type != "file_bucket":
            result.validation_dataset_files_protected += 1
            continue
        try:
            datasource_service.bucket_file_deletion_identity(file, source)
        except ValueError:
            result.validation_dataset_files_protected += 1
            continue
        if _generated_file_has_external_reference(
            db,
            file=file,
            agent=agent,
            owned_dataset_ids=owned_dataset_ids,
            owned_dataset_version_ids=owned_dataset_version_ids,
            owned_asset_ids=owned_asset_ids,
            owned_run_ids=owned_run_ids,
        ):
            result.validation_dataset_files_protected += 1
            continue
        try:
            template_catalog_service.assert_bucket_files_not_registered(db, [file.id])
            result.deletion_job_ids.append(
                object_deletion_service.enqueue_bucket_file_deletion(db, file, source)
            )
            datasource_service.detach_platform_catalog_references_for_deletion(
                db, source, [file.id]
            )
            db.execute(delete(BucketFile).where(BucketFile.id == file.id))
            result.validation_dataset_files_deleted += 1
        except (ValueError, template_catalog_service.TemplateCatalogError):
            result.validation_dataset_files_protected += 1


def _cancel_upload_runs(
    runs: list[ManagedUploadRun],
    *,
    now: datetime,
    result: AgentCleanupResult,
) -> None:
    """Fence upload workers and remove every readable attachment pointer."""

    for run in runs:
        # ``lease_generation`` is part of the worker's final CAS predicate.
        # Bumping it invalidates a worker that claimed the row before Agent
        # deletion acquired the lock.
        run.status = "cancelled"
        run.revision = max(1, int(run.revision or 1) + 1)
        run.lease_generation = max(0, int(run.lease_generation or 0) + 1)
        run.lease_token = ""
        run.lease_expires_at = None
        run.available_at = now
        run.error_code = "agent_deleted"
        run.error_message = "所属 Agent 已删除，附件已清理"
        run.finished_at = now
        run.updated_at = now
        # Keep content hash/size as non-secret audit facts, but remove all
        # pointers that could resolve an object or catalog version later.
        run.bucket_file_id = None
        run.asset_id = None
        run.asset_version_id = None
        run.owner_agent_id = None
        run.metadata_document = {"lifecycle": "agent_deleted"}
    result.upload_runs_cancelled += len(runs)
    # This count represents user-visible removal from the active upload list;
    # hardened PostgreSQL retains the rows as controlled runtime audit state.
    result.upload_runs_deleted += len(runs)


def _retire_assets(
    assets: list[DataAsset],
    *,
    now: datetime,
    result: AgentCleanupResult,
) -> None:
    for asset in assets:
        asset.lifecycle_status = "retired"
        asset.retired_at = now
        # The owner FK is RESTRICT by design. Clear it only after the asset is
        # retired and physical references are handled, then the Agent row can
        # be removed without transferring private assets to another Agent.
        asset.owner_agent_id = None
        asset.name = "已删除附件数据源"
        asset.description = ""
        asset.labels = {
            "lifecycle": "agent_deleted",
            "deleted_at": now.isoformat(),
        }
    result.assets_retired += len(assets)
    result.assets_deleted += len(assets)


def _cleanup_files(
    db: Session,
    *,
    files: list[BucketFile],
    source_by_id: dict[str, DataSource],
    protected_ids: set[str],
    result: AgentCleanupResult,
) -> None:
    result.files_protected += len(protected_ids)
    by_source: dict[str, list[BucketFile]] = {}
    for item in files:
        # ``protected_ids`` is normalized to strings because PostgreSQL UUID
        # adapters and SQLite fixtures may expose different scalar types.
        if str(item.id) in protected_ids:
            continue
        source = source_by_id.get(item.data_source_id)
        if source is None or source.type != "file_bucket":
            result.files_protected += 1
            continue
        # Validate exact persisted identity before registering the outbox row.
        try:
            datasource_service.bucket_file_deletion_identity(item, source)
        except ValueError:
            result.files_protected += 1
            continue
        by_source.setdefault(source.id, []).append(item)

    for source_id, source_files in by_source.items():
        source = source_by_id[source_id]
        file_ids = [item.id for item in source_files]
        for item in source_files:
            result.deletion_job_ids.append(
                object_deletion_service.enqueue_bucket_file_deletion(db, item, source)
            )
        # This is the only path allowed to mutate immutable catalog pointers on
        # PostgreSQL. It is called only for files proven private above.
        datasource_service.detach_platform_catalog_references_for_deletion(
            db,
            source,
            file_ids,
        )
        result.files_deleted += int(
            db.execute(delete(BucketFile).where(BucketFile.id.in_(file_ids))).rowcount
            or 0
        )


def _cleanup_runtime_sources(
    db: Session,
    *,
    agent: Agent,
    sources: list[DataSource],
    owned_run_ids: set[str],
    owned_asset_ids: set[str],
    result: AgentCleanupResult,
) -> None:
    """Delete only private runtime connectors; preserve shared scenario inputs."""

    for source in sources:
        bindings = list(
            db.scalars(
                select(ConnectorBinding).where(
                    ConnectorBinding.tenant_id == agent.tenant_id,
                    ConnectorBinding.connector_kind == "data_source",
                    ConnectorBinding.connector_id == source.id,
                )
            ).all()
        )
        expected_binding_key = (
            f"agent:{agent.id}:database:{source.id}"
            if agent.scenario_id
            else ""
        )
        agent_bindings = [
            binding
            for binding in bindings
            if (
                agent.scenario_id is not None
                and binding.scenario_id == agent.scenario_id
                and str(binding.binding_key or "") == expected_binding_key
            )
        ]
        for binding in agent_bindings:
            db.delete(binding)

        external = _source_has_external_references(
            db,
            source=source,
            agent=agent,
            owned_run_ids=owned_run_ids,
            owned_asset_ids=owned_asset_ids,
        )
        if external:
            # Preserve a connector needed by an independently governed
            # scenario, but remove Agent ownership so the parent FK does not
            # cascade. It is not exposed as another Agent's runtime source.
            source.owner_agent_id = None
            result.data_sources_detached += 1
            continue

        # Retained upload-run audit rows may still point at this source. Keep a
        # redacted tombstone rather than violating the RESTRICT FK.
        has_run_reference = db.scalar(
            select(ManagedUploadRun.id)
            .where(
                ManagedUploadRun.tenant_id == agent.tenant_id,
                ManagedUploadRun.data_source_id == source.id,
            )
            .limit(1)
        )
        if has_run_reference is not None:
            source.owner_agent_id = None
            source.status = "error"
            source.last_error = "所属 Agent 已删除"
            source.config = {}
            result.data_sources_detached += 1
            continue

        # A protected file can remain because a dataset, approval, template,
        # or audit record still owns the physical object.  Never delete the
        # source while such a child exists: its CASCADE would otherwise erase
        # an independently governed file and invalidate the reference that
        # caused it to be protected in the first place.
        has_remaining_file = db.scalar(
            select(BucketFile.id)
            .where(BucketFile.data_source_id == source.id)
            .limit(1)
        )
        if has_remaining_file is not None:
            source.owner_agent_id = None
            source.status = "error"
            source.last_error = "所属 Agent 已删除，仍有受保护附件引用"
            source.config = {}
            result.data_sources_detached += 1
            continue

        datasource_service.invalidate_engine(source)
        # ``source.files`` was loaded while collecting candidates. Refresh it
        # after the bulk BucketFile delete so ORM delete-orphan processing does
        # not act on already-removed child identities.
        if "files" in source.__dict__:
            db.expire(source, ["files"])
        db.delete(source)
        result.data_sources_deleted += 1


def cleanup_agent_owned_records(
    db: Session,
    agent: Agent,
    *,
    delete_agent: bool = True,
) -> AgentCleanupResult:
    """Clean explicit Agent-owned rows without touching its scenario.

    The function intentionally does not commit. Routers and scenario purge own
    the outer transaction and can atomically combine this cleanup with other
    metadata changes. Storage objects are represented by durable outbox rows
    before their metadata is removed.
    """

    locked_agent = _lock_agent(db, agent)
    if locked_agent is None:
        return AgentCleanupResult()
    result = AgentCleanupResult()
    now = _now()

    # Validation datasets are generated from this Agent's private upload
    # versions. Lock the explicit owner marker before touching any child rows;
    # this prevents a concurrent builder from publishing another fragment while
    # deletion is in progress and keeps the scenario definition untouched.
    validation_datasets = _lock_validation_datasets(db, locked_agent)
    validation_dataset_ids = {str(item.id) for item in validation_datasets}
    validation_versions = (
        list(
            db.scalars(
                select(DatasetVersion)
                .where(
                    DatasetVersion.tenant_id == locked_agent.tenant_id,
                    DatasetVersion.dataset_id.in_(sorted(validation_dataset_ids)),
                )
                .order_by(DatasetVersion.id)
                .execution_options(populate_existing=True)
                .with_for_update()
            ).all()
        )
        if validation_dataset_ids
        else []
    )
    validation_version_ids = {str(item.id) for item in validation_versions}
    validation_fragments = _lock_validation_fragments(
        db,
        dataset_ids=validation_dataset_ids,
        tenant_id=locked_agent.tenant_id,
    )
    _cancel_validation_jobs(
        db,
        dataset_ids=validation_dataset_ids,
        tenant_id=locked_agent.tenant_id,
        now=now,
        result=result,
    )
    _mark_validation_datasets_deleted(
        validation_datasets,
        agent=locked_agent,
        now=now,
        result=result,
        versions=validation_versions,
    )
    db.flush()

    active_runs = list(
        db.scalars(
            select(AgentTurnRun)
        .where(
            AgentTurnRun.agent_id == locked_agent.id,
            AgentTurnRun.tenant_id == locked_agent.tenant_id,
            AgentTurnRun.status.in_(ACTIVE_TURN_STATUSES),
        )
        .with_for_update()
            .order_by(AgentTurnRun.id)
    )
        .all()
    )
    for run in active_runs:
        run.status = "cancelled"
        run.revision += 1
        run.lease_token = ""
        run.lease_expires_at = None
        run.lease_generation += 1
        run.error_code = "agent_deleted"
        run.error_message = "Agent 已删除，任务已取消"
        run.cancel_requested_at = run.cancel_requested_at or now
        run.finished_at = now
    if active_runs:
        db.flush()

    upload_runs = _lock_upload_runs(db, locked_agent)
    assets = _lock_assets(db, locked_agent)
    runtime_sources = _lock_runtime_sources(db, locked_agent)
    owned_run_ids = {str(item.id) for item in upload_runs}
    owned_asset_ids = {str(item.id) for item in assets}
    versions = (
        list(
            db.scalars(
                select(DataAssetVersion)
                .where(
                    DataAssetVersion.asset_id.in_(sorted(owned_asset_ids)),
                    DataAssetVersion.tenant_id == locked_agent.tenant_id,
                )
                .order_by(DataAssetVersion.asset_id, DataAssetVersion.version_number)
                .execution_options(populate_existing=True)
                .with_for_update()
            ).all()
        )
        if owned_asset_ids
        else []
    )
    owned_version_ids = {str(item.id) for item in versions}
    file_ids = {
        str(run.bucket_file_id) for run in upload_runs if run.bucket_file_id
    }
    file_ids.update(
        str(version.bucket_file_id) for version in versions if version.bucket_file_id
    )
    for source in runtime_sources:
        file_ids.update(str(item.id) for item in source.files)
    files = _lock_files(
        db,
        file_ids=file_ids,
        tenant_id=locked_agent.tenant_id,
    )
    source_by_id = {source.id: source for source in runtime_sources}
    source_ids = {item.data_source_id for item in files}
    if source_ids:
        source_rows = db.scalars(
            select(DataSource).where(
                DataSource.id.in_(sorted(source_ids)),
                DataSource.tenant_id == locked_agent.tenant_id,
            )
        ).all()
        source_by_id.update({source.id: source for source in source_rows})

    protected_ids = _protected_file_ids(
        db,
        agent=locked_agent,
        files=files,
        owned_run_ids=owned_run_ids,
        owned_asset_ids=owned_asset_ids,
        owned_version_ids=owned_version_ids,
        owned_dataset_ids=validation_dataset_ids,
    )

    _cancel_upload_runs(upload_runs, now=_now(), result=result)
    # ManagedUploadRun has RESTRICT FKs to its source/file/version columns.
    # Flush the fencing update before the physical BucketFile deletion path.
    db.flush()
    _cleanup_validation_dataset_files(
        db,
        agent=locked_agent,
        fragments=validation_fragments,
        owned_dataset_ids=validation_dataset_ids,
        owned_dataset_version_ids=validation_version_ids,
        owned_asset_ids=owned_asset_ids,
        owned_run_ids=owned_run_ids,
        result=result,
    )
    _cleanup_files(
        db,
        files=files,
        source_by_id=source_by_id,
        protected_ids=protected_ids,
        result=result,
    )
    _retire_assets(assets, now=now, result=result)
    db.flush()

    # Turn runs/events are append-only audit state. Detach every composite
    # reference before deleting private transcript rows; this is the same
    # invariant used by the conversation-retention migration.
    turn_runs = list(
        db.scalars(
            select(AgentTurnRun)
            .where(
                AgentTurnRun.agent_id == locked_agent.id,
                AgentTurnRun.tenant_id == locked_agent.tenant_id,
            )
            .order_by(AgentTurnRun.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).all()
    )
    now = _now()
    for run in turn_runs:
        run.agent_id = None
        run.conversation_id = None
        run.user_message_id = None
        run.assistant_message_id = None
        run.lease_token = ""
        run.lease_expires_at = None
        run.lease_generation = max(0, int(run.lease_generation or 0) + 1)
        run.updated_at = now
    result.turn_runs_detached = len(turn_runs)
    # Composite FKs on AgentTurnRun must observe detached values before
    # transcript rows are removed.
    db.flush()

    conversation_ids = list(
        db.scalars(
            select(Conversation.id)
            .join(Agent, Agent.id == Conversation.agent_id)
            .where(
                Conversation.agent_id == locked_agent.id,
                Agent.id == locked_agent.id,
                Agent.tenant_id == locked_agent.tenant_id,
            )
        ).all()
    )
    if conversation_ids:
        db.execute(
            delete(Message).where(
                Message.conversation_id.in_(conversation_ids),
            )
        )
        result.conversations_deleted = int(
            db.execute(
                delete(Conversation).where(
                    Conversation.id.in_(conversation_ids),
                    Conversation.agent_id == locked_agent.id,
                )
            ).rowcount
            or 0
        )

    _cleanup_runtime_sources(
        db,
        agent=locked_agent,
        sources=runtime_sources,
        owned_run_ids=owned_run_ids,
        owned_asset_ids=owned_asset_ids,
        result=result,
    )
    db.flush()

    if delete_agent:
        # Explicit owner FKs have been cleared above. Remaining Agent cascades
        # (MCP publication rows, private invocation metadata, etc.) are
        # downward rows and do not point back to the scenario definition.
        db.delete(locked_agent)
    return result


__all__ = [
    "ACTIVE_TURN_STATUSES",
    "AgentCleanupResult",
    "AgentDeletionConflict",
    "cleanup_agent_owned_records",
]
