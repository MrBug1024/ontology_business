"""Recoverable, tenant-scoped backfill for legacy catalog facts.

The migration is deliberately conservative.  A legacy file can be registered
as an immutable asset when its managed-object identity and digest are already
provable.  A legacy semantic mapping can be reconstructed only when it points
at an existing catalog relation and one explicit scenario binding supplies the
data role.  Ambiguous roles and incomplete mappings become structured report
items; they are never converted into runtime inputs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..catalog_schemas import (
    DataAssetCreate,
    DataAssetVersionRegister,
    SemanticFieldMappingCreate,
    SemanticMappingCreate,
)
from ..models import (
    Agent,
    BucketFile,
    BusinessScenario,
    DataAsset,
    DataAssetVersion,
    DataMapping,
    DataSource,
    DatasetField,
    DatasetRelation,
    OntologyProperty,
    PlatformMigrationCheckpoint,
    PlatformMigrationRun,
    ScenarioDatasetBinding,
    SemanticMapping,
)
from . import catalog_service, datasource_service, permission_service, tenant_service
from .capability_contracts import canonical_hash


MIGRATION_CONTRACT = "legacy-catalog-backfill/v1"
_PLAN_DIGEST = canonical_hash(
    {
        "contract": MIGRATION_CONTRACT,
        "facts": [
            "managed_file_digest",
            "catalog_relation",
            "explicit_scenario_binding_role",
            "exact_property_field_mapping",
        ],
        "unknown_role_policy": "report_only",
        "agent_policy": "preserve_legacy",
    },
    domain="platform-migration-plan-v1",
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_BACKFILL_STAGE = "catalog_backfill"


class PlatformMigrationError(RuntimeError):
    """A safe, structured migration failure."""

    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = str(code or "platform_migration_error")
        self.message = str(message or "Platform migration failed")
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class _BackfillItem:
    key: str
    kind: str
    source_id: str
    scenario_id: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tenant(db: Session) -> str:
    return tenant_service.current_tenant_id(db)


def _migration_name(tenant_id: str) -> str:
    name = f"legacy-catalog:{tenant_id}"
    if len(name) > 120:
        name = f"legacy-catalog:{hashlib.sha256(tenant_id.encode()).hexdigest()}"
    return name


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, (PlatformMigrationError, catalog_service.CatalogError)):
        return str(exc)[:2_000]
    return "Migration item failed; inspect server logs using the run id"


def _source_fingerprint(db: Session, tenant_id: str) -> str:
    files = db.execute(
        select(BucketFile, DataSource)
        .join(DataSource, DataSource.id == BucketFile.data_source_id)
        .where(
            DataSource.tenant_id == tenant_id,
            DataSource.type == "file_bucket",
        )
        .order_by(BucketFile.id)
    ).all()
    mappings = db.scalars(
        select(DataMapping)
        .join(BusinessScenario, BusinessScenario.id == DataMapping.scenario_id)
        .where(BusinessScenario.tenant_id == tenant_id)
        .order_by(DataMapping.id)
    ).all()
    agents = db.scalars(
        select(Agent).where(Agent.tenant_id == tenant_id).order_by(Agent.id)
    ).all()
    facts = {
        "files": [
            {
                "id": item.id,
                "source_id": source.id,
                "digest": str(item.content_sha256 or "").lower(),
                "status": str(item.status or ""),
            }
            for item, source in files
        ],
        "mappings": [
            {
                "id": item.id,
                "scenario_id": item.scenario_id,
                "entity_id": item.entity_id,
                "relation_id": item.dataset_relation_id,
                "column_map_hash": canonical_hash(
                    item.column_map or {}, domain="legacy-column-map-v1"
                ),
                "transform_hash": canonical_hash(
                    item.transform_rules or {}, domain="legacy-transform-rules-v1"
                ),
            }
            for item in mappings
        ],
        "agents": [
            {
                "id": item.id,
                "scenario_id": item.scenario_id,
                "source_ids_hash": canonical_hash(
                    sorted(str(value) for value in (item.data_source_ids or [])),
                    domain="legacy-agent-source-ids-v1",
                ),
            }
            for item in agents
        ],
    }
    return canonical_hash(facts, domain="legacy-catalog-source-v1")


def _collect_items(db: Session, tenant_id: str) -> list[_BackfillItem]:
    items: list[_BackfillItem] = []
    sources = db.scalars(
        select(DataSource)
        .where(DataSource.tenant_id == tenant_id)
        .order_by(DataSource.id)
    ).all()
    agents = db.scalars(
        select(Agent).where(Agent.tenant_id == tenant_id).order_by(Agent.id)
    ).all()
    scenarios_by_source: dict[str, set[str | None]] = {}
    for source in sources:
        if source.scenario_id:
            scenarios_by_source.setdefault(source.id, set()).add(source.scenario_id)
    for agent in agents:
        for raw_source_id in agent.data_source_ids or []:
            source_id = str(raw_source_id or "").strip()
            if source_id:
                scenarios_by_source.setdefault(source_id, set()).add(agent.scenario_id)

    file_rows = db.execute(
        select(BucketFile, DataSource)
        .join(DataSource, DataSource.id == BucketFile.data_source_id)
        .where(
            DataSource.tenant_id == tenant_id,
            DataSource.type == "file_bucket",
        )
        .order_by(BucketFile.id)
    ).all()
    file_source_ids: set[str] = set()
    for bucket_file, source in file_rows:
        items.append(
            _BackfillItem(
                key=f"asset:{bucket_file.id}",
                kind="asset",
                source_id=bucket_file.id,
            )
        )
        file_source_ids.add(source.id)
    for source_id in sorted(file_source_ids):
        usages = scenarios_by_source.get(source_id) or {None}
        for scenario_id in sorted(usages, key=lambda value: value or ""):
            scope_key = scenario_id or "tenant"
            items.append(
                _BackfillItem(
                    key=f"classification:{source_id}:{scope_key}",
                    kind="classification",
                    source_id=source_id,
                    scenario_id=scenario_id,
                )
            )

    mapping_ids = db.scalars(
        select(DataMapping.id)
        .join(BusinessScenario, BusinessScenario.id == DataMapping.scenario_id)
        .where(BusinessScenario.tenant_id == tenant_id)
        .order_by(DataMapping.id)
    ).all()
    items.extend(
        _BackfillItem(
            key=f"semantic:{mapping_id}",
            kind="semantic",
            source_id=mapping_id,
        )
        for mapping_id in mapping_ids
    )
    return sorted(items, key=lambda item: item.key)


def _load_run(db: Session, *, required: bool = True) -> PlatformMigrationRun | None:
    tenant_id = _tenant(db)
    run = db.execute(
        select(PlatformMigrationRun)
        .where(
            PlatformMigrationRun.migration_name == _migration_name(tenant_id),
            PlatformMigrationRun.plan_digest == _PLAN_DIGEST,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if run is None:
        if required:
            raise PlatformMigrationError(
                "migration_not_started", "Legacy catalog migration has not been started", status_code=404
            )
        return None
    if str((run.manifest or {}).get("tenant_id") or "") != tenant_id:
        raise PlatformMigrationError(
            "migration_tenant_mismatch", "Migration ledger does not belong to this tenant", status_code=403
        )
    return run


def start_catalog_backfill(db: Session) -> dict[str, Any]:
    """Create or resume the one stable backfill ledger for the current tenant."""

    permission_service.require_tenant_permission(db, "manage")
    tenant_id = _tenant(db)
    fingerprint = _source_fingerprint(db, tenant_id)
    run = _load_run(db, required=False)
    clock = _now()
    if run is None:
        protected_agents = list(
            db.scalars(
                select(Agent.id).where(
                    Agent.tenant_id == tenant_id,
                    Agent.runtime_binding_mode == "legacy",
                ).order_by(Agent.id)
            ).all()
        )
        run = PlatformMigrationRun(
            id=uuid4().hex,
            migration_name=_migration_name(tenant_id),
            plan_digest=_PLAN_DIGEST,
            source_fingerprint=fingerprint,
            status="running",
            current_phase="plan",
            manifest={
                "contract": MIGRATION_CONTRACT,
                "tenant_id": tenant_id,
                "control": {"paused": False, "reason": "", "updated_by": ""},
                "protected_legacy_agent_ids": protected_agents,
                "legacy_agent_verification_completed": False,
                "attempt": 1,
                "source_fingerprint_history": [fingerprint],
            },
            started_at=clock,
            updated_at=clock,
            completed_at=None,
            last_error="",
        )
        db.add(run)
        db.flush()
    else:
        manifest = dict(run.manifest or {})
        if fingerprint != run.source_fingerprint:
            history = list(manifest.get("source_fingerprint_history") or [])
            if fingerprint not in history:
                history.append(fingerprint)
            manifest["source_fingerprint_history"] = history[-100:]
            run.source_fingerprint = fingerprint
            run.status = "running"
            run.current_phase = "plan"
            run.completed_at = None
        if run.status == "failed":
            run.status = "running"
            run.last_error = ""
            manifest["attempt"] = int(manifest.get("attempt") or 1) + 1
        run.manifest = manifest
        run.updated_at = clock
        db.flush()
    return catalog_backfill_status(db, run=run)


def _set_control(
    db: Session,
    *,
    paused: bool,
    reason: str,
) -> dict[str, Any]:
    permission_service.require_tenant_permission(db, "manage")
    run = _load_run(db)
    normalized_reason = str(reason or "").strip()
    if not normalized_reason:
        raise PlatformMigrationError("migration_reason_required", "A migration control reason is required")
    if len(normalized_reason) > 2_000:
        raise PlatformMigrationError("migration_reason_too_long", "Migration reason is too long")
    manifest = dict(run.manifest or {})
    manifest["control"] = {
        "paused": paused,
        "reason": normalized_reason,
        "updated_by": str(db.info.get("user_id") or ""),
        "updated_at": _now().isoformat(),
    }
    run.manifest = manifest
    if not paused and run.status == "failed":
        run.status = "running"
        run.last_error = ""
        manifest["attempt"] = int(manifest.get("attempt") or 1) + 1
        run.manifest = manifest
    run.updated_at = _now()
    db.flush()
    return catalog_backfill_status(db, run=run)


def pause_catalog_backfill(db: Session, *, reason: str) -> dict[str, Any]:
    return _set_control(db, paused=True, reason=reason)


def resume_catalog_backfill(db: Session, *, reason: str) -> dict[str, Any]:
    return _set_control(db, paused=False, reason=reason)


def retry_catalog_backfill(db: Session, *, reason: str) -> dict[str, Any]:
    permission_service.require_tenant_permission(db, "manage")
    run = _load_run(db)
    if run.status != "failed":
        return catalog_backfill_status(db, run=run)
    return _set_control(db, paused=False, reason=reason)


def _checkpoint(
    db: Session,
    run: PlatformMigrationRun,
    item: _BackfillItem,
    payload: dict[str, Any],
    *,
    row_count: int | None = None,
) -> PlatformMigrationCheckpoint:
    existing = db.get(
        PlatformMigrationCheckpoint,
        {"run_id": run.id, "stage": _BACKFILL_STAGE, "item_key": item.key},
    )
    digest = canonical_hash(payload, domain="platform-migration-checkpoint-v1")
    if existing is not None:
        if existing.payload_sha256 != digest:
            raise PlatformMigrationError(
                "checkpoint_content_conflict",
                "A completed migration item no longer matches its durable checkpoint",
            )
        return existing
    checkpoint = PlatformMigrationCheckpoint(
        run_id=run.id,
        stage=_BACKFILL_STAGE,
        item_key=item.key,
        status="complete",
        payload_sha256=digest,
        row_count=row_count,
        payload=payload,
        completed_at=_now(),
    )
    db.add(checkpoint)
    db.flush()
    return checkpoint


def _report(item: _BackfillItem, code: str, message: str, **facts: Any) -> dict[str, Any]:
    return {
        "contract": "legacy-catalog-report-item/v1",
        "outcome": "unclassified",
        "item_kind": item.kind,
        "source_id": item.source_id,
        "scenario_id": item.scenario_id,
        "code": code,
        "message": message,
        "facts": facts,
    }


def _asset_payload(db: Session, tenant_id: str, item: _BackfillItem) -> tuple[dict[str, Any], int]:
    row = db.execute(
        select(BucketFile, DataSource)
        .join(DataSource, DataSource.id == BucketFile.data_source_id)
        .where(
            BucketFile.id == item.source_id,
            DataSource.tenant_id == tenant_id,
            DataSource.type == "file_bucket",
        )
    ).one_or_none()
    if row is None:
        return _report(item, "legacy_file_missing", "The legacy file no longer exists"), 0
    bucket_file, source = row
    existing_version = db.execute(
        select(DataAssetVersion)
        .where(
            DataAssetVersion.bucket_file_id == bucket_file.id,
            DataAssetVersion.bucket_data_source_id == source.id,
            DataAssetVersion.tenant_id == tenant_id,
        )
        .order_by(DataAssetVersion.version_number)
    ).scalars().first()
    if existing_version is not None:
        return {
            "contract": "legacy-catalog-result/v1",
            "outcome": "already_cataloged",
            "item_kind": item.kind,
            "source_id": item.source_id,
            "asset_id": existing_version.asset_id,
            "asset_version_id": existing_version.id,
        }, 0
    digest = str(bucket_file.content_sha256 or "").strip().lower()
    if _SHA256_RE.fullmatch(digest) is None:
        return _report(
            item,
            "file_digest_unproven",
            "The file has no verified SHA-256 and was not cataloged automatically",
        ), 0
    if not datasource_service.is_managed_minio_file(bucket_file):
        return _report(
            item,
            "file_identity_unmanaged",
            "The file has no managed immutable object identity",
        ), 0
    asset_key = "legacy.file." + hashlib.sha256(
        f"{tenant_id}:{source.id}:{bucket_file.id}".encode()
    ).hexdigest()[:40]
    asset = db.execute(
        select(DataAsset).where(DataAsset.tenant_id == tenant_id, DataAsset.key == asset_key)
    ).scalar_one_or_none()
    created_asset = False
    if asset is None:
        asset = catalog_service.create_asset(
            db,
            DataAssetCreate(
                key=asset_key,
                name=str(bucket_file.filename or "Legacy managed file")[:300],
                description="Reconstructed from a verified managed legacy file.",
                kind="file",
                media_type=str(bucket_file.mime or "")[:200],
                labels={
                    "migration": {
                        "contract": MIGRATION_CONTRACT,
                        "source_kind": "bucket_file",
                    }
                },
            ),
        )
        created_asset = True
    elif asset.kind != "file" or asset.lifecycle_status != "active":
        return _report(
            item,
            "asset_key_conflict",
            "The deterministic asset key is occupied by an incompatible asset",
        ), 0
    version = catalog_service.register_asset_version(
        db,
        asset,
        DataAssetVersionRegister(
            bucket_file_id=bucket_file.id,
            provenance_kind="reconstruction",
            version_document={
                "format": "legacy-catalog-reconstruction/v1",
                "source": {"kind": "bucket_file"},
                "parse": {"status": str(bucket_file.status or "unknown")[:40]},
            },
        ),
    )
    return {
        "contract": "legacy-catalog-result/v1",
        "outcome": "created" if created_asset else "version_registered",
        "item_kind": item.kind,
        "source_id": item.source_id,
        "asset_id": asset.id,
        "asset_version_id": version.id,
        "content_sha256": version.content_sha256,
    }, 1


def _classification_payload(
    db: Session, tenant_id: str, item: _BackfillItem
) -> tuple[dict[str, Any], int]:
    source = db.execute(
        select(DataSource).where(
            DataSource.id == item.source_id,
            DataSource.tenant_id == tenant_id,
        )
    ).scalar_one_or_none()
    if source is None:
        return _report(item, "legacy_source_missing", "The legacy source no longer exists"), 0
    return _report(
        item,
        "data_role_unclassified",
        "Legacy usage does not prove whether historical files are modeling evidence or test fixtures",
        allowed_manual_roles=["modeling_evidence", "test_fixture"],
        forbidden_inference="invocation_input",
        source_type=str(source.type or "unknown"),
    ), 0


def _semantic_payload(
    db: Session, tenant_id: str, item: _BackfillItem
) -> tuple[dict[str, Any], int]:
    mapping = db.execute(
        select(DataMapping)
        .join(BusinessScenario, BusinessScenario.id == DataMapping.scenario_id)
        .where(
            DataMapping.id == item.source_id,
            BusinessScenario.tenant_id == tenant_id,
        )
    ).scalar_one_or_none()
    if mapping is None:
        return _report(item, "legacy_mapping_missing", "The legacy mapping no longer exists"), 0
    scoped_item = _BackfillItem(item.key, item.kind, item.source_id, mapping.scenario_id)
    if not mapping.dataset_relation_id:
        return _report(
            scoped_item,
            "catalog_relation_unproven",
            "The legacy mapping has no explicit catalog relation",
        ), 0
    relation = db.execute(
        select(DatasetRelation).where(
            DatasetRelation.id == mapping.dataset_relation_id,
            DatasetRelation.tenant_id == tenant_id,
        )
    ).scalar_one_or_none()
    if relation is None:
        return _report(
            scoped_item,
            "catalog_relation_unavailable",
            "The declared catalog relation is missing or outside the tenant",
        ), 0
    bindings = list(
        db.scalars(
            select(ScenarioDatasetBinding).where(
                ScenarioDatasetBinding.tenant_id == tenant_id,
                ScenarioDatasetBinding.scenario_id == mapping.scenario_id,
                ScenarioDatasetBinding.dataset_id == relation.dataset_id,
                ScenarioDatasetBinding.status == "active",
            ).order_by(ScenarioDatasetBinding.id)
        ).all()
    )
    if len(bindings) != 1:
        return _report(
            scoped_item,
            "data_role_unclassified",
            "Exactly one explicit scenario dataset binding is required before semantic reconstruction",
            candidate_binding_ids=[binding.id for binding in bindings],
            candidate_roles=sorted({str(binding.role) for binding in bindings}),
        ), 0
    binding = bindings[0]
    existing = db.execute(
        select(SemanticMapping).where(
            SemanticMapping.tenant_id == tenant_id,
            SemanticMapping.scenario_id == mapping.scenario_id,
            SemanticMapping.entity_id == mapping.entity_id,
            SemanticMapping.scenario_dataset_binding_id == binding.id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {
            "contract": "legacy-catalog-result/v1",
            "outcome": "already_cataloged",
            "item_kind": item.kind,
            "source_id": item.source_id,
            "scenario_id": mapping.scenario_id,
            "semantic_mapping_id": existing.id,
        }, 0
    if mapping.transform_rules:
        return _report(
            scoped_item,
            "transform_requires_review",
            "Legacy transform rules require expert review and were not copied implicitly",
        ), 0
    raw_column_map = mapping.column_map or {}
    if not isinstance(raw_column_map, dict) or not raw_column_map:
        return _report(
            scoped_item,
            "field_mapping_unproven",
            "The legacy mapping has no explicit property-to-field map",
        ), 0
    properties = list(
        db.scalars(
            select(OntologyProperty).where(OntologyProperty.entity_id == mapping.entity_id)
        ).all()
    )
    fields = list(
        db.scalars(
            select(DatasetField).where(
                DatasetField.tenant_id == tenant_id,
                DatasetField.dataset_relation_id == relation.id,
                DatasetField.schema_id == relation.schema_id,
            )
        ).all()
    )
    resolved: list[SemanticFieldMappingCreate] = []
    used_properties: set[str] = set()
    used_fields: set[str] = set()
    unresolved: list[dict[str, str]] = []
    for raw_property, raw_field in sorted(raw_column_map.items(), key=lambda pair: str(pair[0])):
        property_key = str(raw_property or "").strip()
        field_key = str(raw_field or "").strip()
        property_candidates = {
            prop.id: prop
            for prop in properties
            if property_key in {str(prop.name or ""), str(prop.api_name or "")}
        }
        field_candidates = {
            field.id: field
            for field in fields
            if field_key in {str(field.source_name or ""), str(field.field_key or "")}
        }
        if len(property_candidates) != 1 or len(field_candidates) != 1:
            unresolved.append(
                {
                    "property": property_key[:180],
                    "field": field_key[:300],
                    "reason": "not_exactly_one_match",
                }
            )
            continue
        prop = next(iter(property_candidates.values()))
        field = next(iter(field_candidates.values()))
        if prop.id in used_properties or field.id in used_fields:
            unresolved.append(
                {
                    "property": property_key[:180],
                    "field": field_key[:300],
                    "reason": "duplicate_target",
                }
            )
            continue
        used_properties.add(prop.id)
        used_fields.add(field.id)
        resolved.append(
            SemanticFieldMappingCreate(
                ontology_property_id=prop.id,
                dataset_field_id=field.id,
                direction="input",
                is_required=bool(prop.is_required),
                transform={},
            )
        )
    if unresolved or len(resolved) != len(raw_column_map):
        return _report(
            scoped_item,
            "field_mapping_ambiguous",
            "Not every legacy property and catalog field has one exact match",
            unresolved=unresolved[:100],
            declared_count=len(raw_column_map),
            resolved_count=len(resolved),
        ), 0
    semantic = catalog_service.create_semantic_mapping(
        db,
        mapping.scenario_id,
        SemanticMappingCreate(
            scenario_dataset_binding_id=binding.id,
            entity_id=mapping.entity_id,
            dataset_schema_id=relation.schema_id,
            dataset_relation_id=relation.id,
            mapping_key=f"legacy.{mapping.id}",
            status="draft",
            identifier_strategy={},
            filter_expression={},
            fields=resolved,
        ),
    )
    return {
        "contract": "legacy-catalog-result/v1",
        "outcome": "created",
        "item_kind": item.kind,
        "source_id": item.source_id,
        "scenario_id": mapping.scenario_id,
        "binding_id": binding.id,
        "binding_role": binding.role,
        "semantic_mapping_id": semantic.id,
        "status": semantic.status,
    }, 1


_PROCESSORS: dict[
    str, Callable[[Session, str, _BackfillItem], tuple[dict[str, Any], int]]
] = {
    "asset": _asset_payload,
    "classification": _classification_payload,
    "semantic": _semantic_payload,
}


def _verify_protected_agents(db: Session, run: PlatformMigrationRun) -> None:
    if bool((run.manifest or {}).get("legacy_agent_verification_completed")):
        return
    tenant_id = _tenant(db)
    protected = {
        str(value)
        for value in (run.manifest or {}).get("protected_legacy_agent_ids", [])
        if str(value or "").strip()
    }
    if not protected:
        return
    changed = list(
        db.execute(
            select(Agent.id, Agent.runtime_binding_mode).where(
                Agent.id.in_(protected),
                Agent.tenant_id == tenant_id,
                Agent.runtime_binding_mode != "legacy",
            )
        ).all()
    )
    if changed:
        raise PlatformMigrationError(
            "protected_agent_mode_changed",
            "A stock Agent left legacy mode before catalog backfill verification",
        )


def run_catalog_backfill_batch(db: Session, *, batch_size: int = 100) -> dict[str, Any]:
    permission_service.require_tenant_permission(db, "manage")
    if isinstance(batch_size, bool) or not 1 <= int(batch_size) <= 500:
        raise PlatformMigrationError("invalid_batch_size", "Batch size must be between 1 and 500")
    run = _load_run(db)
    if bool(((run.manifest or {}).get("control") or {}).get("paused")):
        return catalog_backfill_status(db, run=run)
    if run.status == "failed":
        raise PlatformMigrationError(
            "migration_retry_required", "Failed migration must be explicitly retried"
        )
    if run.status == "verified":
        return catalog_backfill_status(db, run=run)
    tenant_id = _tenant(db)
    run.status = "running"
    run.current_phase = "import"
    run.updated_at = _now()
    items = _collect_items(db, tenant_id)
    completed = set(
        db.scalars(
            select(PlatformMigrationCheckpoint.item_key).where(
                PlatformMigrationCheckpoint.run_id == run.id,
                PlatformMigrationCheckpoint.stage == _BACKFILL_STAGE,
            )
        ).all()
    )
    pending = [item for item in items if item.key not in completed]
    for item in pending[: int(batch_size)]:
        try:
            with db.begin_nested():
                payload, row_count = _PROCESSORS[item.kind](db, tenant_id, item)
                _checkpoint(db, run, item, payload, row_count=row_count)
        except Exception as exc:  # noqa: BLE001 - preserve a retryable durable failure.
            run.status = "failed"
            run.last_error = _safe_error(exc)
            run.updated_at = _now()
            db.flush()
            return catalog_backfill_status(db, run=run)

    completed = set(
        db.scalars(
            select(PlatformMigrationCheckpoint.item_key).where(
                PlatformMigrationCheckpoint.run_id == run.id,
                PlatformMigrationCheckpoint.stage == _BACKFILL_STAGE,
            )
        ).all()
    )
    if all(item.key in completed for item in items):
        try:
            _verify_protected_agents(db, run)
        except PlatformMigrationError as exc:
            run.status = "failed"
            run.last_error = exc.message
        else:
            run.status = "verified"
            run.current_phase = "verify"
            run.completed_at = _now()
            run.last_error = ""
            manifest = dict(run.manifest or {})
            manifest["legacy_agent_verification_completed"] = True
            run.manifest = manifest
    run.updated_at = _now()
    db.flush()
    return catalog_backfill_status(db, run=run)


def catalog_backfill_status(
    db: Session,
    *,
    run: PlatformMigrationRun | None = None,
) -> dict[str, Any]:
    permission_service.require_tenant_permission(db, "manage")
    active_run = run or _load_run(db)
    tenant_id = _tenant(db)
    items = _collect_items(db, tenant_id)
    checkpoints = list(
        db.scalars(
            select(PlatformMigrationCheckpoint)
            .where(
                PlatformMigrationCheckpoint.run_id == active_run.id,
                PlatformMigrationCheckpoint.stage == _BACKFILL_STAGE,
            )
            .order_by(PlatformMigrationCheckpoint.item_key)
        ).all()
    )
    reports = [
        dict(checkpoint.payload or {})
        for checkpoint in checkpoints
        if str((checkpoint.payload or {}).get("outcome") or "") == "unclassified"
    ]
    created_count = sum(
        1
        for checkpoint in checkpoints
        if str((checkpoint.payload or {}).get("outcome") or "")
        in {"created", "version_registered"}
    )
    return {
        "contract": MIGRATION_CONTRACT,
        "run_id": active_run.id,
        "status": active_run.status,
        "phase": active_run.current_phase,
        "paused": bool(((active_run.manifest or {}).get("control") or {}).get("paused")),
        "attempt": int((active_run.manifest or {}).get("attempt") or 1),
        "plan_digest": active_run.plan_digest,
        "source_fingerprint": active_run.source_fingerprint,
        "counts": {
            "planned": len(items),
            "completed": len(checkpoints),
            "pending": max(0, len(items) - len(checkpoints)),
            "created": created_count,
            "unclassified": len(reports),
        },
        "unclassified": reports,
        "last_error": str(active_run.last_error or ""),
        "started_at": active_run.started_at,
        "updated_at": active_run.updated_at,
        "completed_at": active_run.completed_at,
    }


__all__ = [
    "MIGRATION_CONTRACT",
    "PlatformMigrationError",
    "catalog_backfill_status",
    "pause_catalog_backfill",
    "resume_catalog_backfill",
    "retry_catalog_backfill",
    "run_catalog_backfill_batch",
    "start_catalog_backfill",
]
