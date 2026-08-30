from __future__ import annotations

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Agent,
    BucketFile,
    BusinessScenario,
    DataAsset,
    DataAssetVersion,
    DataMapping,
    DataSource,
    DatasetField,
    DatasetRelation,
    DatasetSchema,
    DatasetVersion,
    LogicalDataset,
    OntologyEntity,
    OntologyProperty,
    PlatformMigrationCheckpoint,
    ScenarioDatasetBinding,
    SemanticMapping,
    Tenant,
    User,
)
from app.services import permission_service, platform_migration_service


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    session.info["test_engine"] = engine
    return session


def _tenant_world(db: Session, key: str):
    tenant = Tenant(id=f"tenant-{key}", name=f"Tenant {key}")
    user = User(
        id=f"user-{key}",
        tenant_id=tenant.id,
        email=f"{key}@example.test",
        password_hash="test-only",
        status="active",
    )
    scenario = BusinessScenario(
        id=f"scenario-{key}", tenant_id=tenant.id, name=f"Scenario {key}"
    )
    db.add_all([tenant, user, scenario])
    db.commit()
    permission_service.ensure_organization(db, tenant.id, owner_user_id=user.id)
    db.commit()
    db.info["tenant_id"] = tenant.id
    db.info["user_id"] = user.id
    return tenant, user, scenario


def _close(db: Session) -> None:
    engine = db.info["test_engine"]
    db.close()
    engine.dispose()


def test_backfill_pauses_reports_unknown_role_and_reruns_idempotently() -> None:
    db = _db()
    try:
        tenant, _user, scenario = _tenant_world(db, "backfill")
        source = DataSource(
            id="source-backfill",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            name="Legacy managed files",
            type="file_bucket",
            config={},
        )
        bucket_file = BucketFile(
            id="file-backfill",
            data_source_id=source.id,
            filename="historic.csv",
            stored_path="minio://managed/historic.csv",
            storage_provider="minio",
            bucket_name="managed",
            object_key="historic.csv",
            object_url="minio://managed/historic.csv",
            content_sha256="a" * 64,
            size=42,
            mime="text/csv",
            status="parsed",
        )
        agent = Agent(
            id="agent-backfill",
            tenant_id=tenant.id,
            name="Stock Agent",
            scenario_id=scenario.id,
            data_source_ids=[source.id],
            capability_scope={},
            runtime_binding_mode="legacy",
        )
        db.add_all([source, bucket_file, agent])
        db.commit()

        started = platform_migration_service.start_catalog_backfill(db)
        assert started["status"] == "running"
        paused = platform_migration_service.pause_catalog_backfill(
            db, reason="Await maintenance window"
        )
        assert paused["paused"] is True
        unchanged = platform_migration_service.run_catalog_backfill_batch(db)
        assert unchanged["counts"]["completed"] == 0

        platform_migration_service.resume_catalog_backfill(
            db, reason="Maintenance window opened"
        )
        completed = platform_migration_service.run_catalog_backfill_batch(
            db, batch_size=20
        )
        db.commit()
        assert completed["status"] == "verified"
        assert completed["counts"]["unclassified"] == 1
        report = completed["unclassified"][0]
        assert report["code"] == "data_role_unclassified"
        assert report["facts"]["allowed_manual_roles"] == [
            "modeling_evidence",
            "test_fixture",
        ]
        assert report["facts"]["forbidden_inference"] == "invocation_input"
        assert db.get(Agent, agent.id).runtime_binding_mode == "legacy"
        counts_before = (
            db.scalar(select(func.count()).select_from(DataAsset)),
            db.scalar(select(func.count()).select_from(DataAssetVersion)),
            db.scalar(select(func.count()).select_from(PlatformMigrationCheckpoint)),
        )

        rerun = platform_migration_service.start_catalog_backfill(db)
        rerun = platform_migration_service.run_catalog_backfill_batch(db, batch_size=20)
        db.commit()
        assert rerun["status"] == "verified"
        counts_after = (
            db.scalar(select(func.count()).select_from(DataAsset)),
            db.scalar(select(func.count()).select_from(DataAssetVersion)),
            db.scalar(select(func.count()).select_from(PlatformMigrationCheckpoint)),
        )
        assert counts_after == counts_before == (1, 1, 2)

        # A later, explicitly governed Agent cutover is outside this completed
        # catalog migration and must not be reverted or invalidate a no-op rerun.
        agent.runtime_binding_mode = "shadow"
        db.commit()
        no_op = platform_migration_service.start_catalog_backfill(db)
        no_op = platform_migration_service.run_catalog_backfill_batch(db, batch_size=20)
        assert no_op["status"] == "verified"
        assert db.get(Agent, agent.id).runtime_binding_mode == "shadow"
    finally:
        _close(db)


def test_backfill_recovers_failed_item_without_duplicate_writes(monkeypatch) -> None:
    db = _db()
    try:
        tenant, _user, scenario = _tenant_world(db, "recover")
        source = DataSource(
            id="source-recover",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            name="Managed files",
            type="file_bucket",
            config={},
        )
        bucket_file = BucketFile(
            id="file-recover",
            data_source_id=source.id,
            filename="sample.csv",
            stored_path="minio://managed/sample.csv",
            storage_provider="minio",
            bucket_name="managed",
            object_key="sample.csv",
            object_url="minio://managed/sample.csv",
            content_sha256="b" * 64,
            size=1,
            mime="text/csv",
        )
        db.add_all([source, bucket_file])
        db.commit()
        platform_migration_service.start_catalog_backfill(db)
        original = platform_migration_service._PROCESSORS["asset"]

        def fail_once(*_args, **_kwargs):
            raise RuntimeError("sensitive backend detail")

        monkeypatch.setitem(platform_migration_service._PROCESSORS, "asset", fail_once)
        failed = platform_migration_service.run_catalog_backfill_batch(db, batch_size=1)
        db.commit()
        assert failed["status"] == "failed"
        assert "sensitive backend detail" not in failed["last_error"]
        assert db.scalar(select(func.count()).select_from(DataAsset)) == 0

        monkeypatch.setitem(platform_migration_service._PROCESSORS, "asset", original)
        retried = platform_migration_service.retry_catalog_backfill(
            db, reason="Transient worker failure resolved"
        )
        assert retried["status"] == "running"
        completed = platform_migration_service.run_catalog_backfill_batch(db, batch_size=20)
        db.commit()
        assert completed["status"] == "verified"
        assert db.scalar(select(func.count()).select_from(DataAsset)) == 1
        assert db.scalar(select(func.count()).select_from(DataAssetVersion)) == 1
    finally:
        _close(db)


def test_semantic_mapping_backfills_only_from_explicit_catalog_facts() -> None:
    db = _db()
    try:
        tenant, _user, scenario = _tenant_world(db, "semantic")
        source = DataSource(
            id="source-semantic",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            name="Legacy connector",
            type="postgres",
            config={},
        )
        entity = OntologyEntity(
            id="entity-semantic", scenario_id=scenario.id, name="Business record"
        )
        prop = OntologyProperty(
            id="property-semantic",
            entity_id=entity.id,
            name="Record ID",
            api_name="record_id",
            data_type="string",
            is_required=True,
        )
        dataset = LogicalDataset(
            id="dataset-semantic",
            tenant_id=tenant.id,
            key="business.records",
            name="Business records",
        )
        schema = DatasetSchema(
            id="schema-semantic",
            tenant_id=tenant.id,
            dataset_id=dataset.id,
            schema_version=1,
            schema_hash="c" * 64,
            compatibility="none",
        )
        relation = DatasetRelation(
            id="relation-semantic",
            tenant_id=tenant.id,
            dataset_id=dataset.id,
            schema_id=schema.id,
            relation_key="records",
            display_name="Records",
            kind="table",
            ordinal=0,
        )
        field = DatasetField(
            id="field-semantic",
            tenant_id=tenant.id,
            dataset_id=dataset.id,
            schema_id=schema.id,
            dataset_relation_id=relation.id,
            field_key="record_id",
            source_name="record_id",
            logical_type="string",
            nullable=False,
            ordinal=0,
        )
        version = DatasetVersion(
            id="version-semantic",
            tenant_id=tenant.id,
            dataset_id=dataset.id,
            schema_id=schema.id,
            version_number=1,
            status="ready",
            content_hash="d" * 64,
        )
        binding = ScenarioDatasetBinding(
            id="binding-semantic",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            dataset_id=dataset.id,
            binding_key="records.evidence",
            environment="dev",
            role="modeling_evidence",
            binding_mode="pinned",
            dataset_version_id=version.id,
            is_required=False,
            status="active",
            config={},
        )
        mapping = DataMapping(
            id="mapping-semantic",
            scenario_id=scenario.id,
            entity_id=entity.id,
            data_source_id=source.id,
            dataset_relation_id=relation.id,
            table_name="records",
            column_map={"Record ID": "record_id"},
            transform_rules={},
        )
        db.add_all([source, entity, prop, dataset])
        db.flush()
        db.add(schema)
        db.flush()
        db.add(relation)
        db.flush()
        db.add(field)
        db.add(version)
        db.flush()
        db.add_all([binding, mapping])
        db.commit()

        platform_migration_service.start_catalog_backfill(db)
        status = platform_migration_service.run_catalog_backfill_batch(db, batch_size=20)
        db.commit()
        assert status["status"] == "verified"
        semantic = db.execute(select(SemanticMapping)).scalar_one()
        assert semantic.status == "draft"
        assert semantic.scenario_dataset_binding_id == binding.id
        assert semantic.dataset_relation_id == relation.id
        assert [(item.ontology_property_id, item.dataset_field_id) for item in semantic.field_mappings] == [
            (prop.id, field.id)
        ]
        assert db.get(Agent, "missing") is None
    finally:
        _close(db)


def test_backfill_ledger_is_tenant_scoped() -> None:
    db = _db()
    try:
        tenant_a, user_a, _scenario_a = _tenant_world(db, "acl-a")
        started_a = platform_migration_service.start_catalog_backfill(db)
        db.commit()
        tenant_b, user_b, _scenario_b = _tenant_world(db, "acl-b")
        started_b = platform_migration_service.start_catalog_backfill(db)
        db.commit()
        assert started_a["run_id"] != started_b["run_id"]
        db.info["tenant_id"] = tenant_a.id
        db.info["user_id"] = user_a.id
        assert platform_migration_service.catalog_backfill_status(db)["run_id"] == started_a[
            "run_id"
        ]
        db.info["tenant_id"] = tenant_b.id
        db.info["user_id"] = user_b.id
        assert platform_migration_service.catalog_backfill_status(db)["run_id"] == started_b[
            "run_id"
        ]
    finally:
        _close(db)
