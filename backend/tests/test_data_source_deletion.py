from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    BucketFile,
    DataAsset,
    DataAssetVersion,
    DataSource,
    DerivationRun,
    IngestionRun,
    ObjectDeletionJob,
    Tenant,
    User,
)
from app.routers import data_sources
from app.services import datasource_service, object_storage_service, permission_service


class TestDataSourceDeletion:
    def setup_method(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        tenant = Tenant(id="tenant-delete", name="Deletion tenant")
        user = User(
            id="user-delete",
            tenant_id=tenant.id,
            email="delete@example.test",
            password_hash="test-only",
            status="active",
        )
        self.db.add_all([tenant, user])
        self.db.commit()
        permission_service.ensure_organization(
            self.db, tenant.id, owner_user_id=user.id
        )
        self.db.commit()
        self.db.info["tenant_id"] = tenant.id
        self.db.info["user_id"] = user.id

    def teardown_method(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_dataset_connection_is_deleteable_but_not_editable(self) -> None:
        source = DataSource(
            id="dataset-deleteable",
            tenant_id="tenant-delete",
            name="Versioned dataset connector",
            type="dataset",
            config={"adapter": "dataset", "dataset_version_id": "version-1"},
        )
        self.db.add(source)
        self.db.commit()

        output = data_sources._out(source, self.db)

        assert output.can_write is False
        assert output.can_delete is True

        response = data_sources.delete_data_source(source.id, self.db)

        assert response.ok is True
        assert response.data == {
            "data_source_id": source.id,
            "files_deleted": 0,
            "cleanup_jobs": 0,
        }
        assert self.db.get(DataSource, source.id) is None

    def test_remote_database_deletion_only_removes_platform_configuration(self) -> None:
        source = DataSource(
            id="remote-db-deleteable",
            tenant_id="tenant-delete",
            name="Remote PostgreSQL",
            type="postgres",
            config={
                "host": "remote.example.test",
                "port": 5432,
                "database": "business",
                "username": "readonly",
            },
        )
        self.db.add(source)
        self.db.commit()

        with (
            patch.object(data_sources.datasource_service, "get_engine") as get_engine,
            patch.object(data_sources.datasource_service, "run_query") as run_query,
        ):
            response = data_sources.delete_data_source(source.id, self.db)

        assert response.ok is True
        assert self.db.get(DataSource, source.id) is None
        get_engine.assert_not_called()
        run_query.assert_not_called()

    def test_legacy_managed_minio_key_can_be_deleted(self) -> None:
        source = DataSource(
            id="legacy-bucket-delete",
            tenant_id="tenant-delete",
            name="Imported materials",
            type="file_bucket",
            config={
                "storage_backend": "minio",
                "bucket_name": "ontology",
                "prefix": "ontology-business",
            },
        )
        legacy_key = "ontology-business/migrations/import-1/datasets/bookkeeping/manifest.json"
        legacy_url = object_storage_service.stable_object_url("ontology", legacy_key)
        bucket_file = BucketFile(
            id="9" * 32,
            data_source_id=source.id,
            filename="manifest.json",
            stored_path=legacy_url,
            storage_provider="minio",
            bucket_name="ontology",
            object_key=legacy_key,
            object_url=legacy_url,
            size=8,
            mime="application/json",
            status="parsed",
        )
        self.db.add_all([source, bucket_file])
        self.db.commit()

        with (
            patch.object(
                object_storage_service,
                "require_configuration",
                return_value=object_storage_service.MinioConfiguration(
                    endpoint="minio.example.test",
                    access_key="access",
                    secret_key="secret",
                    bucket_name="ontology",
                    prefix="ontology-business",
                ),
            ),
            patch.object(data_sources.object_deletion_service, "drain_jobs_best_effort"),
        ):
            response = data_sources.delete_data_source(source.id, self.db)

        assert response.ok is True
        assert response.data["files_deleted"] == 1
        assert self.db.get(DataSource, source.id) is None
        assert self.db.get(BucketFile, bucket_file.id) is None
        deletion_jobs = list(
            self.db.scalars(
                select(ObjectDeletionJob).where(
                    ObjectDeletionJob.origin_type == "bucket_file"
                )
            ).all()
        )
        assert len(deletion_jobs) == 1
        assert deletion_jobs[0].object_key == legacy_key

    def test_owned_minio_delete_detaches_catalog_blob_references(self) -> None:
        source = DataSource(
            id="catalog-bucket-delete",
            tenant_id="tenant-delete",
            name="Catalog source",
            type="file_bucket",
            config={
                "storage_backend": "minio",
                "bucket_name": "ontology",
                "prefix": "ontology-business",
            },
        )
        object_key = "ontology-business/legacy/catalog/source.csv"
        object_url = object_storage_service.stable_object_url("ontology", object_key)
        bucket_file = BucketFile(
            id="7" * 32,
            data_source_id=source.id,
            filename="source.csv",
            stored_path=object_url,
            storage_provider="minio",
            bucket_name="ontology",
            object_key=object_key,
            object_url=object_url,
            size=8,
            mime="text/csv",
            status="parsed",
        )
        asset = DataAsset(
            id="6" * 32,
            tenant_id="tenant-delete",
            key="catalog-source",
            name="Catalog source",
            kind="file",
            lifecycle_status="active",
        )
        asset_version = DataAssetVersion(
            id="5" * 32,
            tenant_id="tenant-delete",
            asset_id=asset.id,
            version_number=1,
            bucket_file_id=bucket_file.id,
            bucket_data_source_id=source.id,
            provenance_kind="upload",
            status="ready",
            content_sha256="a" * 64,
            byte_size=8,
            source_locator={"bucket_name": "ontology", "object_key": object_key},
            version_document={},
        )
        self.db.add_all([source, bucket_file, asset, asset_version])
        self.db.commit()

        with (
            patch.object(
                object_storage_service,
                "require_configuration",
                return_value=object_storage_service.MinioConfiguration(
                    endpoint="minio.example.test",
                    access_key="access",
                    secret_key="secret",
                    bucket_name="ontology",
                    prefix="ontology-business",
                ),
            ),
            patch.object(data_sources.object_deletion_service, "drain_jobs_best_effort"),
        ):
            response = data_sources.delete_data_source(source.id, self.db)

        assert response.data["asset_versions_detached"] == 1
        detached = self.db.get(DataAssetVersion, asset_version.id)
        assert detached is not None
        assert detached.status == "retired"
        assert detached.bucket_file_id is None
        assert detached.bucket_data_source_id is None
        assert detached.source_locator == {}

    def test_owned_minio_delete_detaches_platform_run_traces(self) -> None:
        source = DataSource(
            id="run-trace-bucket-delete",
            tenant_id="tenant-delete",
            name="Run trace source",
            type="file_bucket",
            config={
                "storage_backend": "minio",
                "bucket_name": "ontology",
                "prefix": "ontology-business",
            },
        )
        object_key = (
            "ontology-business/tenants/tenant-delete/scenarios/global/"
            "data-sources/run-trace-bucket-delete/files/"
            + "r" * 32
            + "/uploads/"
            + "u" * 32
            + "/material.md"
        )
        object_url = object_storage_service.stable_object_url("ontology", object_key)
        bucket_file = BucketFile(
            id="r" * 32,
            data_source_id=source.id,
            filename="material.md",
            stored_path=object_url,
            storage_provider="minio",
            bucket_name="ontology",
            object_key=object_key,
            object_url=object_url,
            size=8,
            mime="text/markdown",
            status="parsed",
        )
        ingestion_run = IngestionRun(
            id="i" * 32,
            tenant_id="tenant-delete",
            dataset_id="dataset-not-materialized",
            pipeline_kind="test",
            trace_bucket_file_id=bucket_file.id,
            trace_data_source_id=source.id,
        )
        derivation_run = DerivationRun(
            id="d" * 32,
            tenant_id="tenant-delete",
            scenario_id=None,
            ontology_content_hash="a" * 64,
            mode="forward",
            engine="test",
            engine_version="test",
            rule_set_hash="b" * 64,
            input_fingerprint="c" * 64,
            trace_bucket_file_id=bucket_file.id,
            trace_data_source_id=source.id,
        )
        self.db.add_all([source, bucket_file, ingestion_run, derivation_run])
        self.db.commit()

        with (
            patch.object(
                object_storage_service,
                "require_configuration",
                return_value=object_storage_service.MinioConfiguration(
                    endpoint="minio.example.test",
                    access_key="access",
                    secret_key="secret",
                    bucket_name="ontology",
                    prefix="ontology-business",
                ),
            ),
            patch.object(data_sources.object_deletion_service, "drain_jobs_best_effort"),
        ):
            response = data_sources.delete_data_source(source.id, self.db)

        assert response.ok is True
        self.db.refresh(ingestion_run)
        self.db.refresh(derivation_run)
        assert ingestion_run.trace_bucket_file_id is None
        assert ingestion_run.trace_data_source_id is None
        assert derivation_run.trace_bucket_file_id is None
        assert derivation_run.trace_data_source_id is None

    def test_deletion_rejects_object_outside_managed_minio_prefix(self) -> None:
        source = DataSource(
            id="bucket-scope-check",
            tenant_id="tenant-delete",
            name="Managed bucket",
            type="file_bucket",
            config={
                "storage_backend": "minio",
                "bucket_name": "ontology",
                "prefix": "ontology-business",
            },
        )
        object_key = "unmanaged/file.md"
        object_url = object_storage_service.stable_object_url("ontology", object_key)
        bucket_file = BucketFile(
            id="8" * 32,
            data_source_id=source.id,
            filename="file.md",
            stored_path=object_url,
            storage_provider="minio",
            bucket_name="ontology",
            object_key=object_key,
            object_url=object_url,
        )

        with patch.object(
            object_storage_service,
            "require_configuration",
            return_value=object_storage_service.MinioConfiguration(
                endpoint="minio.example.test",
                access_key="access",
                secret_key="secret",
                bucket_name="ontology",
                prefix="ontology-business",
            ),
        ):
            try:
                datasource_service.bucket_file_deletion_identity(bucket_file, source)
            except ValueError as exc:
                assert "托管文件桶" in str(exc)
            else:
                raise AssertionError("unmanaged MinIO object must be rejected")

    def test_file_bucket_deletion_keeps_exact_minio_cleanup_audit(self) -> None:
        source = DataSource(
            id="bucket-deleteable",
            tenant_id="tenant-delete",
            name="Managed bucket",
            type="file_bucket",
            config={
                "storage_backend": "minio",
                "bucket_name": "ontology",
                "prefix": "ontology-business",
            },
        )
        bucket_file = BucketFile(
            id="f" * 32,
            data_source_id=source.id,
            filename="material.md",
            stored_path="minio://ontology/ontology-business/test/material.md",
            storage_provider="minio",
            bucket_name="ontology",
            object_key="ontology-business/tenants/tenant-delete/scenarios/global/data-sources/bucket-deleteable/files/"
            + "f" * 32
            + "/uploads/"
            + "a" * 32
            + "/material.md",
            object_version_id="version-1",
            object_url="minio://ontology/ontology-business/tenants/tenant-delete/scenarios/global/data-sources/bucket-deleteable/files/"
            + "f" * 32
            + "/uploads/"
            + "a" * 32
            + "/material.md",
            size=8,
            mime="text/markdown",
            status="parsed",
        )
        self.db.add_all([source, bucket_file])
        self.db.commit()

        with (
            patch.object(
                object_storage_service,
                "require_configuration",
                return_value=object_storage_service.MinioConfiguration(
                    endpoint="minio.example.test",
                    access_key="access",
                    secret_key="secret",
                    bucket_name="ontology",
                    prefix="ontology-business",
                ),
            ),
            patch.object(data_sources.object_deletion_service, "drain_jobs_best_effort"),
        ):
            response = data_sources.delete_data_source(source.id, self.db)

        assert response.data["files_deleted"] == 1
        assert response.data["cleanup_jobs"] == 1
        assert self.db.get(DataSource, source.id) is None
        assert self.db.get(BucketFile, bucket_file.id) is None
        jobs = list(self.db.scalars(select(ObjectDeletionJob)).all())
        assert {job.origin_type for job in jobs} == {
            "bucket_file_delete",
            "bucket_file",
        }
        assert any(job.object_version_id == "version-1" for job in jobs)
