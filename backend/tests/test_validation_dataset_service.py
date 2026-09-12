from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from io import BytesIO
import hashlib
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

import pyarrow.parquet as parquet
from fastapi import HTTPException
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.catalog_schemas import ValidationDatasetBuildIn
from app.database import Base
from app.models import (
    Agent,
    AuthorizationGrant,
    BusinessScenario,
    BucketFile,
    DataAsset,
    DataAssetVersion,
    DataSource,
    DatasetFragment,
    DatasetHead,
    DatasetSchema,
    DatasetVersion,
    DocumentChunk,
    IngestionRun,
    LogicalDataset,
    Tenant,
    User,
)
from app.services import (
    catalog_ingestion_service,
    catalog_service,
    dataset_query_service,
    datasource_service,
    object_deletion_service,
    object_storage_service,
    permission_service,
    managed_attachment_access,
    validation_dataset_service,
)
from app.routers import catalog as catalog_router


class ValidationDatasetServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        Base.metadata.create_all(self.engine)
        self.raw_objects: dict[tuple[str, str], bytes] = {}
        self.parquet_objects: dict[tuple[str, str], bytes] = {}
        content = (
            "claim_id,amount,approved\n"
            "12345678901234567890,12.50,true\n"
            "00000123,7,false\n"
        ).encode()
        digest = hashlib.sha256(content).hexdigest()
        with self.Session() as db:
            tenant = Tenant(id="tenant-validation-data", name="Validation data")
            user = User(
                id="user-validation-data",
                tenant_id=tenant.id,
                email="validation-data@example.test",
                password_hash="test-only",
                status="active",
            )
            source = DataSource(
                id=catalog_service.external_upload_source_id(tenant.id),
                tenant_id=tenant.id,
                name="Validation managed bucket",
                type="file_bucket",
                resource_scope="agent_runtime",
                owner_agent_id=None,
                config={
                    "storage_backend": "minio",
                    "bucket_name": "validation-test",
                    "prefix": "platform",
                },
            )
            bucket_file = BucketFile(
                id="raw-validation-csv",
                data_source_id=source.id,
                filename="claims.csv",
                stored_path="minio://validation-test/platform/raw/claims.csv",
                storage_provider="minio",
                bucket_name="validation-test",
                object_key="platform/raw/claims.csv",
                object_version_id="raw-v1",
                object_url="minio://validation-test/platform/raw/claims.csv",
                size=len(content),
                mime="text/csv",
                content_sha256=digest,
                status="parsed",
            )
            asset = DataAsset(
                id="validation-csv-asset",
                tenant_id=tenant.id,
                key="validation.claims",
                name="claims.csv",
                media_type="text/csv",
                usage_plane="invocation_input",
                labels={"catalog_purpose": "validation_asset"},
                created_by_user_id=user.id,
            )
            version = DataAssetVersion(
                id="validation-csv-version",
                tenant_id=tenant.id,
                asset_id=asset.id,
                version_number=1,
                bucket_file_id=bucket_file.id,
                bucket_data_source_id=source.id,
                status="ready",
                content_sha256=digest,
                byte_size=len(content),
                version_document={
                    "profile": {
                        "category": "table",
                        "extension": ".csv",
                        "media_type": "text/csv",
                        "tables": [
                            {
                                "name": "claims",
                                "header_row_index": 0,
                                "columns": [
                                    {"name": "claim_id", "logical_type": "integer"},
                                    {"name": "amount", "logical_type": "number"},
                                    {"name": "approved", "logical_type": "boolean"},
                                ],
                            }
                        ],
                    }
                },
                created_by_user_id=user.id,
            )
            db.add_all([tenant, user, source, bucket_file, asset, version])
            db.commit()
            permission_service.ensure_organization(
                db,
                tenant.id,
                owner_user_id=user.id,
            )
            db.commit()
        self.source_id = catalog_service.external_upload_source_id(
            "tenant-validation-data"
        )
        self.asset_version_id = "validation-csv-version"
        self.raw_objects[("validation-test", "platform/raw/claims.csv")] = content
        self._storage_config = patch.object(
            object_storage_service,
            "require_configuration",
            return_value=SimpleNamespace(bucket_name="validation-test", prefix="platform"),
        )
        self._storage_config.start()

    def tearDown(self) -> None:
        self._storage_config.stop()
        self.engine.dispose()

    def _database(self):
        db = self.Session()
        db.info["tenant_id"] = "tenant-validation-data"
        db.info["user_id"] = "user-validation-data"
        return db

    def test_modeling_asset_cannot_be_promoted_to_validation_input(self) -> None:
        with self._database() as db:
            asset = db.get(DataAsset, "validation-csv-asset")
            asset.usage_plane = "modeling_material"
            db.commit()

            with self.assertRaises(validation_dataset_service.ValidationDatasetError):
                validation_dataset_service.enqueue_validation_dataset_job(
                    db,
                    ValidationDatasetBuildIn(
                        asset_version_ids=[self.asset_version_id],
                        name="Must stay modeling-only",
                    ),
                )

    def test_validation_dataset_input_is_agent_scoped(self) -> None:
        with self._database() as db:
            agent_one = Agent(
                id="validation-agent-one",
                tenant_id="tenant-validation-data",
                name="Agent one",
            )
            agent_two = Agent(
                id="validation-agent-two",
                tenant_id="tenant-validation-data",
                name="Agent two",
            )
            asset = db.get(DataAsset, "validation-csv-asset")
            asset.owner_agent_id = agent_one.id
            db.add_all([agent_one, agent_two])
            db.commit()

            first = validation_dataset_service.enqueue_validation_dataset_job(
                db,
                ValidationDatasetBuildIn(
                    asset_version_ids=[self.asset_version_id],
                    name="Agent one package",
                    agent_id=agent_one.id,
                ),
            )
            self.assertEqual(first["status"], "queued")
            with self.assertRaises(validation_dataset_service.ValidationDatasetError):
                validation_dataset_service.enqueue_validation_dataset_job(
                    db,
                    ValidationDatasetBuildIn(
                        asset_version_ids=[self.asset_version_id],
                        name="Agent two package",
                        agent_id=agent_two.id,
                    ),
                )

    def test_scenario_acl_viewer_can_enqueue_and_job_read_rechecks_acl(self) -> None:
        with self._database() as db:
            scenario = BusinessScenario(
                id="validation-viewer-scenario",
                tenant_id="tenant-validation-data",
                name="Validation viewer scenario",
                status="active",
            )
            agent_one = Agent(
                id="validation-viewer-agent",
                tenant_id="tenant-validation-data",
                scenario_id=scenario.id,
                name="Validation viewer Agent",
            )
            agent_two = Agent(
                id="validation-viewer-other-agent",
                tenant_id="tenant-validation-data",
                scenario_id=scenario.id,
                name="Other validation viewer Agent",
            )
            viewer = User(
                id="validation-viewer-user",
                tenant_id="tenant-validation-data",
                email="validation-viewer@example.test",
                password_hash="test-only",
                status="active",
            )
            modeling_dataset = LogicalDataset(
                id="validation-viewer-modeling-dataset",
                tenant_id="tenant-validation-data",
                key="validation.viewer.modeling",
                name="Viewer modeling dataset",
                lifecycle_status="active",
                usage_plane="modeling_material",
                labels={},
                created_by_user_id="user-validation-data",
            )
            db.add_all(
                [scenario, agent_one, agent_two, viewer, modeling_dataset]
            )
            db.flush()
            organization = permission_service.ensure_organization(
                db,
                "tenant-validation-data",
            )
            permission_service.assign_member_role(
                db,
                organization,
                user_id=viewer.id,
                role_key="viewer",
            )
            db.add(
                AuthorizationGrant(
                    organization_id=organization.id,
                    user_id=viewer.id,
                    resource_type="scenario",
                    resource_id=scenario.id,
                    verb="write",
                    effect="allow",
                    created_by_user_id="user-validation-data",
                )
            )
            db.get(DataAsset, "validation-csv-asset").owner_agent_id = agent_one.id
            db.commit()
            db.info["user_id"] = viewer.id
            permission_service.refresh_request_authorization(db)

            self.assertFalse(
                permission_service.check_tenant_permission(db, "write").allowed
            )
            self.assertTrue(
                permission_service.check_scenario(db, scenario, "write").allowed
            )
            with self.assertRaises(HTTPException) as global_denied:
                validation_dataset_service.enqueue_validation_dataset_job(
                    db,
                    ValidationDatasetBuildIn(
                        asset_version_ids=[self.asset_version_id],
                        name="Global package",
                    ),
                )
            self.assertEqual(global_denied.exception.status_code, 403)

            queued = validation_dataset_service.enqueue_validation_dataset_job(
                db,
                ValidationDatasetBuildIn(
                    asset_version_ids=[self.asset_version_id],
                    name="Viewer Agent package",
                    agent_id=agent_one.id,
                ),
            )
            loaded = validation_dataset_service.get_validation_dataset_job(
                db,
                queued["id"],
                agent_id=agent_one.id,
            )
            self.assertEqual(loaded["id"], queued["id"])
            run = db.get(IngestionRun, queued["id"])
            dataset_id = str(run.dataset_id)
            schema = DatasetSchema(
                id="validation-viewer-schema",
                tenant_id="tenant-validation-data",
                dataset_id=dataset_id,
                schema_version=1,
                schema_hash="a" * 64,
                compatibility="none",
                schema_document={"visibility_marker": "agent-one-only"},
                created_by_user_id="user-validation-data",
            )
            version = DatasetVersion(
                id="validation-viewer-version",
                tenant_id="tenant-validation-data",
                dataset_id=dataset_id,
                schema_id=schema.id,
                version_number=1,
                status="ready",
                content_hash="b" * 64,
                manifest={"visibility_marker": "agent-one-only"},
                created_by_user_id="user-validation-data",
                ready_at=datetime.now(timezone.utc),
            )
            head = DatasetHead(
                id="validation-viewer-head",
                tenant_id="tenant-validation-data",
                dataset_id=dataset_id,
                dataset_version_id=version.id,
                updated_by_user_id="user-validation-data",
            )
            db.add_all([schema, version, head])
            db.commit()
            db.expire_all()
            self.assertEqual(
                [item.id for item in catalog_service.list_datasets(db)],
                [modeling_dataset.id],
            )
            self.assertEqual(
                [
                    item.id
                    for item in catalog_service.list_datasets(
                        db,
                        agent_id=agent_one.id,
                    )
                ],
                [dataset_id],
            )
            self.assertEqual(
                catalog_service.list_datasets(db, agent_id=agent_two.id),
                [],
            )
            with self.assertRaises(catalog_service.CatalogError):
                catalog_service.require_dataset(db, dataset_id)
            self.assertEqual(
                catalog_service.require_dataset(
                    db,
                    dataset_id,
                    agent_id=agent_one.id,
                ).id,
                dataset_id,
            )
            self.assertEqual(
                [
                    item.id
                    for item in catalog_router.list_dataset_schemas(
                        dataset_id,
                        agent_id=agent_one.id,
                        db=db,
                    )
                ],
                [schema.id],
            )
            self.assertEqual(
                [
                    item.id
                    for item in catalog_router.list_dataset_versions(
                        dataset_id,
                        agent_id=agent_one.id,
                        db=db,
                    )
                ],
                [version.id],
            )
            self.assertEqual(
                [
                    item.id
                    for item in catalog_router.list_dataset_heads(
                        dataset_id,
                        agent_id=agent_one.id,
                        db=db,
                    )
                ],
                [head.id],
            )
            with self.assertRaises(HTTPException) as unscoped_dataset:
                catalog_router.list_dataset_versions(dataset_id, db=db)
            self.assertEqual(unscoped_dataset.exception.status_code, 404)
            with self.assertRaises(HTTPException) as wrong_agent_dataset:
                catalog_router.list_dataset_versions(
                    dataset_id,
                    agent_id=agent_two.id,
                    db=db,
                )
            self.assertEqual(wrong_agent_dataset.exception.status_code, 404)
            with self.assertRaisesRegex(
                validation_dataset_service.ValidationDatasetError,
                "不属于当前 Agent",
            ):
                validation_dataset_service.get_validation_dataset_job(
                    db,
                    queued["id"],
                    agent_id=agent_two.id,
                )

            db.add(
                AuthorizationGrant(
                    organization_id=organization.id,
                    user_id=viewer.id,
                    resource_type="scenario",
                    resource_id=scenario.id,
                    verb="read",
                    effect="deny",
                    created_by_user_id="user-validation-data",
                )
            )
            db.commit()
            permission_service.refresh_request_authorization(db)
            with self.assertRaises(HTTPException) as revoked:
                validation_dataset_service.get_validation_dataset_job(
                    db,
                    queued["id"],
                    agent_id=agent_one.id,
                )
            self.assertEqual(revoked.exception.status_code, 403)

    def test_enqueue_does_not_requeue_retired_validation_dataset(self) -> None:
        """A tombstoned package cannot be resurrected by a repeated enqueue."""

        agent_id = "validation-retired-agent"
        with self._database() as db:
            agent = Agent(
                id=agent_id,
                tenant_id="tenant-validation-data",
                name="Retired package agent",
            )
            db.get(DataAsset, "validation-csv-asset").owner_agent_id = agent_id
            db.add(agent)
            db.commit()

            payload = ValidationDatasetBuildIn(
                asset_version_ids=[self.asset_version_id],
                name="Retired package",
                agent_id=agent_id,
            )
            queued = validation_dataset_service.enqueue_validation_dataset_job(
                db, payload
            )
            dataset = db.get(LogicalDataset, queued["result"]["dataset_id"]) if queued.get("result") else None
            if dataset is None:
                dataset = db.scalar(
                    select(LogicalDataset).where(
                        LogicalDataset.labels["owner_agent_id"].as_string() == agent_id
                    )
                )
            self.assertIsNotNone(dataset)
            assert dataset is not None
            run = db.get(IngestionRun, queued["id"])
            self.assertIsNotNone(run)
            assert run is not None
            run.status = "failed"
            run.error = "synthetic failure"
            dataset.lifecycle_status = "retired"
            db.commit()

            with self.assertRaisesRegex(
                validation_dataset_service.ValidationDatasetError,
                "已被删除",
            ):
                validation_dataset_service.enqueue_validation_dataset_job(
                    db, payload
                )
            db.refresh(run)
            self.assertEqual(run.status, "failed")
            self.assertEqual(run.error, "synthetic failure")

            with self.assertRaisesRegex(
                validation_dataset_service.ValidationDatasetError,
                "已被删除",
            ):
                validation_dataset_service.get_validation_dataset_job(
                    db,
                    queued["id"],
                    agent_id=agent_id,
                )
            run.status = "pending"
            run.error = ""
            db.commit()

        with patch.object(validation_dataset_service, "SessionLocal", self.Session):
            self.assertFalse(
                validation_dataset_service.process_validation_dataset_job(
                    queued["id"]
                )
            )

        with self._database() as db:
            run = db.get(IngestionRun, queued["id"])
            self.assertEqual(run.status, "failed")
            self.assertIn("已被删除", run.error)

    def test_agent_job_rejects_retired_validation_dataset_version(self) -> None:
        from contextlib import ExitStack

        agent_id = "validation-retired-version-agent"
        payload = ValidationDatasetBuildIn(
            asset_version_ids=[self.asset_version_id],
            name="Retired version package",
            agent_id=agent_id,
        )
        with self._database() as db:
            db.add(
                Agent(
                    id=agent_id,
                    tenant_id="tenant-validation-data",
                    name="Retired version Agent",
                )
            )
            db.get(DataAsset, "validation-csv-asset").owner_agent_id = agent_id
            db.commit()
            queued = validation_dataset_service.enqueue_validation_dataset_job(
                db, payload
            )

        with ExitStack() as stack:
            for active_patch in self._patches():
                stack.enter_context(active_patch)
            stack.enter_context(
                patch.object(validation_dataset_service, "SessionLocal", self.Session)
            )
            self.assertTrue(
                validation_dataset_service.process_validation_dataset_job(
                    queued["id"]
                )
            )

        with self._database() as db:
            run = db.get(IngestionRun, queued["id"])
            self.assertIsNotNone(run.output_version_id)
            output_version = db.get(DatasetVersion, run.output_version_id)
            output_version.status = "retired"
            db.commit()

            with self.assertRaisesRegex(
                validation_dataset_service.ValidationDatasetError,
                "版本已被删除",
            ):
                validation_dataset_service.get_validation_dataset_job(
                    db,
                    queued["id"],
                    agent_id=agent_id,
                )
            with self.assertRaisesRegex(
                validation_dataset_service.ValidationDatasetError,
                "版本已被删除",
            ):
                validation_dataset_service.enqueue_validation_dataset_job(
                    db, payload
                )
            run.status = "pending"
            run.error = ""
            db.commit()

        with patch.object(validation_dataset_service, "SessionLocal", self.Session):
            self.assertFalse(
                validation_dataset_service.process_validation_dataset_job(
                    queued["id"]
                )
            )

        with self._database() as db:
            run = db.get(IngestionRun, queued["id"])
            self.assertEqual(run.status, "failed")
            self.assertIn("版本已被删除", run.error)

    def test_enqueue_and_worker_do_not_reuse_old_validation_run(self) -> None:
        payload = ValidationDatasetBuildIn(
            asset_version_ids=[self.asset_version_id],
            name="Old pipeline package",
        )
        with self._database() as db:
            queued = validation_dataset_service.enqueue_validation_dataset_job(
                db, payload
            )
            run = db.get(IngestionRun, queued["id"])
            run.pipeline_version = "validation-dataset/v1"
            run.status = "failed"
            db.commit()

            with self.assertRaisesRegex(
                validation_dataset_service.ValidationDatasetError,
                "旧验证数据集任务不可复用",
            ):
                validation_dataset_service.enqueue_validation_dataset_job(
                    db, payload
                )
            with self.assertRaisesRegex(
                validation_dataset_service.ValidationDatasetError,
                "任务不存在",
            ):
                validation_dataset_service.get_validation_dataset_job(
                    db,
                    queued["id"],
                )
            run.status = "pending"
            db.commit()

        with patch.object(validation_dataset_service, "SessionLocal", self.Session):
            self.assertFalse(
                validation_dataset_service.process_validation_dataset_job(
                    queued["id"]
                )
            )

        with self._database() as db:
            self.assertEqual(db.get(IngestionRun, queued["id"]).status, "pending")

    def _download(
        self,
        bucket_name: str,
        object_key: str,
        destination: str | Path,
        **_kwargs,
    ):
        content = self.raw_objects.get((bucket_name, object_key))
        if content is None:
            content = self.parquet_objects[(bucket_name, object_key)]
        Path(destination).write_bytes(content)
        return SimpleNamespace(size=len(content))

    def _save_parquet(
        self,
        source: DataSource,
        filename: str,
        source_path: str | Path,
        *,
        mime: str,
        stable_file_id: str,
        upload_object_key: str,
        content_sha256: str,
    ) -> BucketFile:
        content = Path(source_path).read_bytes()
        self.parquet_objects[("validation-test", upload_object_key)] = content
        return BucketFile(
            id=stable_file_id,
            data_source_id=source.id,
            filename=filename,
            stored_path=f"minio://validation-test/{upload_object_key}",
            storage_provider="minio",
            bucket_name="validation-test",
            object_key=upload_object_key,
            object_version_id="parquet-v1",
            etag=content_sha256,
            object_url=f"minio://validation-test/{upload_object_key}",
            size=len(content),
            mime=mime,
            content_sha256=content_sha256,
        )

    def _patches(self):
        with self._database() as db:
            source = db.get(DataSource, self.source_id)
            db.expunge(source)

        def prepare_claim(data_source, file_id, filename):
            return SimpleNamespace(
                object_key=f"platform/files/{file_id}/generations/{'a' * 32}/{filename}"
            )

        return (
            patch.object(
                catalog_ingestion_service,
                "require_external_upload_bucket",
                return_value=source,
            ),
            patch.object(
                object_storage_service,
                "download_object_to_file",
                side_effect=self._download,
            ),
            patch.object(
                datasource_service,
                "save_bucket_file_path",
                side_effect=self._save_parquet,
            ),
            patch.object(
                object_deletion_service,
                "prepare_bucket_file_upload",
                side_effect=prepare_claim,
            ),
            patch.object(
                object_deletion_service,
                "heartbeat_upload_intent",
                side_effect=lambda _claim: nullcontext(SimpleNamespace()),
            ),
            patch.object(object_deletion_service, "begin_upload_put"),
            patch.object(object_deletion_service, "assert_upload_active"),
            patch.object(object_deletion_service, "retain_bucket_file_upload"),
        )

    def test_materialization_uses_profile_format_for_renamed_csv(self) -> None:
        content = b"record_id;amount\nA-1;12.5\n"
        _media_type, profile = catalog_ingestion_service.build_profile(
            content,
            "renamed.payload",
            "application/octet-stream",
        )

        with tempfile.TemporaryDirectory(prefix="validation-profile-") as temp_dir:
            work = Path(temp_dir)
            raw_path = work / "raw.payload"
            raw_path.write_bytes(content)
            relations = validation_dataset_service._materialize_raw_file(
                raw_path,
                "renamed.payload",
                profile,
                work,
                set(),
            )

        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]["row_count"], 1)
        self.assertEqual(
            [field["name"] for field in relations[0]["fields"]],
            ["record_id", "amount"],
        )

    def test_materializes_queryable_minio_parquet_without_postgresql_rows(self) -> None:
        from contextlib import ExitStack

        payload = ValidationDatasetBuildIn(
            asset_version_ids=[self.asset_version_id],
            name="Claims validation package",
        )
        with ExitStack() as stack:
            for active_patch in self._patches():
                stack.enter_context(active_patch)
            with self._database() as db:
                first = validation_dataset_service.build_validation_dataset(db, payload)
                second = validation_dataset_service.build_validation_dataset(db, payload)

        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["dataset_version_id"], second["dataset_version_id"])
        self.assertEqual(first["record_count"], 2)
        self.assertEqual(first["relation_names"], ["claims"])
        self.assertEqual(len(self.parquet_objects), 1)
        stored_table = parquet.read_table(BytesIO(next(iter(self.parquet_objects.values()))))
        self.assertEqual(
            stored_table.column("claim_id").to_pylist(),
            ["12345678901234567890", "00000123"],
        )

        with self._database() as db:
            self.assertEqual(db.scalar(select(func.count(DocumentChunk.id))), 0)
            self.assertEqual(db.scalar(select(func.count(LogicalDataset.id))), 1)
            self.assertEqual(db.scalar(select(func.count(DatasetVersion.id))), 1)
            self.assertEqual(db.scalar(select(func.count(DatasetFragment.id))), 1)
            query_source = DataSource(
                id="validation-query-source",
                tenant_id="tenant-validation-data",
                name="Validation query source",
                type="dataset",
                config={
                    "dataset_id": first["dataset_id"],
                    "dataset_version_id": first["dataset_version_id"],
                },
            )
            db.add(query_source)
            db.commit()

        with patch.object(dataset_query_service, "SessionLocal", self.Session):
            catalog = dataset_query_service._load_catalog(query_source)
        self.assertEqual(catalog.dataset_version_id, first["dataset_version_id"])
        self.assertEqual(catalog.relations[0].row_count, 2)

    def test_materialized_validation_dataset_cannot_cross_agent_scope(self) -> None:
        from contextlib import ExitStack

        with self._database() as db:
            agent_one = Agent(
                id="validation-scope-agent-one",
                tenant_id="tenant-validation-data",
                name="Agent one",
            )
            agent_two = Agent(
                id="validation-scope-agent-two",
                tenant_id="tenant-validation-data",
                name="Agent two",
            )
            db.get(DataAsset, "validation-csv-asset").owner_agent_id = agent_one.id
            db.add_all([agent_one, agent_two])
            db.commit()

        payload = ValidationDatasetBuildIn(
            asset_version_ids=[self.asset_version_id],
            name="Scoped package",
            agent_id="validation-scope-agent-one",
        )
        with ExitStack() as stack:
            for active_patch in self._patches():
                stack.enter_context(active_patch)
            with self._database() as db:
                result = validation_dataset_service.build_validation_dataset(db, payload)

        attachment = SimpleNamespace(
            upload_run_id=None,
            asset_version_id=None,
            dataset_version_id=result["dataset_version_id"],
            expected_signature=result["content_hash"],
        )
        with self._database() as db:
            managed_attachment_access.validate_attachments(
                db,
                [attachment],
                user_id="user-validation-data",
                agent_id="validation-scope-agent-one",
            )
            with self.assertRaises(managed_attachment_access.AttachmentAccessError):
                managed_attachment_access.validate_attachments(
                    db,
                    [attachment],
                    user_id="user-validation-data",
                    agent_id="validation-scope-agent-two",
                )

    def test_agent_disappearing_after_materialization_cannot_publish_orphan_dataset(self) -> None:
        from contextlib import ExitStack

        agent_id = "validation-race-agent"
        with self._database() as db:
            agent = Agent(
                id=agent_id,
                tenant_id="tenant-validation-data",
                name="Race agent",
            )
            db.get(DataAsset, "validation-csv-asset").owner_agent_id = agent_id
            db.add(agent)
            db.commit()

        payload = ValidationDatasetBuildIn(
            asset_version_ids=[self.asset_version_id],
            name="Race package",
            agent_id=agent_id,
        )

        # This callback runs after raw download and table materialization but
        # before the final publication fence.  The separate session commits
        # the simulated concurrent delete while the builder is still doing
        # CPU work; no generated metadata has been staged yet.
        original_materialize = validation_dataset_service._materialize_raw_file

        def remove_agent_before_publication(*args, **kwargs):
            result = original_materialize(*args, **kwargs)
            with self._database() as deleting_db:
                deleting_db.execute(delete(Agent).where(Agent.id == agent_id))
                deleting_db.commit()
            return result

        with ExitStack() as stack:
            for active_patch in self._patches():
                stack.enter_context(active_patch)
            stack.enter_context(
                patch.object(
                    validation_dataset_service,
                    "_materialize_raw_file",
                    side_effect=remove_agent_before_publication,
                )
            )
            with self._database() as db:
                with self.assertRaisesRegex(
                    validation_dataset_service.ValidationDatasetError,
                    "Agent 已删除",
                ):
                    validation_dataset_service.build_validation_dataset(db, payload)

        with self._database() as db:
            self.assertIsNone(db.get(Agent, agent_id))
            self.assertEqual(
                db.scalar(
                    select(func.count(LogicalDataset.id)).where(
                        LogicalDataset.labels["owner_agent_id"].as_string() == agent_id
                    )
                ),
                0,
            )
            self.assertEqual(db.scalar(select(func.count(DatasetFragment.id))), 0)

    def test_publication_rejects_stale_validation_job_lease(self) -> None:
        from contextlib import ExitStack

        payload = ValidationDatasetBuildIn(
            asset_version_ids=[self.asset_version_id],
            name="Stale lease package",
        )
        with self._database() as db:
            queued = validation_dataset_service.enqueue_validation_dataset_job(
                db, payload
            )
            run = db.get(IngestionRun, queued["id"])
            run.status = "running"
            run.lease_token = "current-lease-token"
            run.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            db.commit()

        with ExitStack() as stack:
            for active_patch in self._patches():
                stack.enter_context(active_patch)
            with self._database() as db:
                with self.assertRaisesRegex(
                    validation_dataset_service.ValidationDatasetError,
                    "租约已失效",
                ):
                    validation_dataset_service.build_validation_dataset(
                        db,
                        payload,
                        _publication_job_id=queued["id"],
                        _publication_lease_token="stale-lease-token",
                    )

        with self._database() as db:
            run = db.get(IngestionRun, queued["id"])
            self.assertEqual(run.status, "running")
            self.assertEqual(run.lease_token, "current-lease-token")
            # Enqueue owns the empty logical-dataset row; publication must not
            # add a version or fragment after the stale lease is observed.
            self.assertEqual(db.scalar(select(func.count(LogicalDataset.id))), 1)
            self.assertEqual(db.scalar(select(func.count(DatasetFragment.id))), 0)

    def test_durable_job_materializes_and_exposes_reusable_result(self) -> None:
        from contextlib import ExitStack

        payload = ValidationDatasetBuildIn(
            asset_version_ids=[self.asset_version_id],
            name="Durable validation package",
        )
        with self._database() as db:
            queued = validation_dataset_service.enqueue_validation_dataset_job(
                db,
                payload,
            )
            duplicate = validation_dataset_service.enqueue_validation_dataset_job(
                db,
                payload,
            )
            global_dataset_id = str(db.get(IngestionRun, queued["id"]).dataset_id)
            self.assertIn(
                global_dataset_id,
                {item.id for item in catalog_service.list_datasets(db)},
            )
            self.assertEqual(
                catalog_service.require_dataset(db, global_dataset_id).id,
                global_dataset_id,
            )
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(duplicate["id"], queued["id"])

        with ExitStack() as stack:
            for active_patch in self._patches():
                stack.enter_context(active_patch)
            stack.enter_context(
                patch.object(validation_dataset_service, "SessionLocal", self.Session)
            )
            self.assertTrue(
                validation_dataset_service.process_validation_dataset_job(queued["id"])
            )

        with self._database() as db:
            completed = validation_dataset_service.get_validation_dataset_job(
                db,
                queued["id"],
            )
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["result"]["record_count"], 2)
        self.assertEqual(
            completed["result"]["source_asset_version_ids"],
            [self.asset_version_id],
        )

    def test_active_validation_job_lease_is_not_recovered_or_reclaimed(self) -> None:
        payload = ValidationDatasetBuildIn(
            asset_version_ids=[self.asset_version_id],
            name="Active lease validation package",
        )
        with self._database() as db:
            queued = validation_dataset_service.enqueue_validation_dataset_job(
                db, payload
            )
            run = db.get(IngestionRun, queued["id"])
            run.status = "running"
            run.lease_token = "active-validation-lease"
            run.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            db.commit()

        with patch.object(validation_dataset_service, "SessionLocal", self.Session):
            self.assertEqual(
                validation_dataset_service.recover_validation_dataset_jobs(), 0
            )
            self.assertFalse(
                validation_dataset_service.process_validation_dataset_job(queued["id"])
            )

        with self._database() as db:
            run = db.get(IngestionRun, queued["id"])
            self.assertEqual(run.status, "running")
            self.assertEqual(run.lease_token, "active-validation-lease")

    def test_only_expired_validation_job_lease_is_requeued(self) -> None:
        payload = ValidationDatasetBuildIn(
            asset_version_ids=[self.asset_version_id],
            name="Expired lease validation package",
        )
        with self._database() as db:
            queued = validation_dataset_service.enqueue_validation_dataset_job(
                db, payload
            )
            run = db.get(IngestionRun, queued["id"])
            run.status = "running"
            run.lease_token = "expired-validation-lease"
            run.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()

        with patch.object(validation_dataset_service, "SessionLocal", self.Session):
            self.assertEqual(
                validation_dataset_service.recover_validation_dataset_jobs(), 1
            )

        with self._database() as db:
            run = db.get(IngestionRun, queued["id"])
            self.assertEqual(run.status, "pending")
            self.assertEqual(run.lease_token, "")
            self.assertIsNone(run.lease_expires_at)

    def test_releases_catalog_transaction_before_download_and_materialization(self) -> None:
        from contextlib import ExitStack

        payload = ValidationDatasetBuildIn(
            asset_version_ids=[self.asset_version_id],
            name="Transaction boundary validation package",
        )
        transaction_states: list[bool] = []
        with ExitStack() as stack:
            for active_patch in self._patches():
                stack.enter_context(active_patch)
            with self._database() as db:
                def download_without_open_catalog_transaction(*args, **kwargs):
                    transaction_states.append(db.in_transaction())
                    return self._download(*args, **kwargs)

                stack.enter_context(
                    patch.object(
                        object_storage_service,
                        "download_object_to_file",
                        side_effect=download_without_open_catalog_transaction,
                    )
                )
                validation_dataset_service.build_validation_dataset(db, payload)

        self.assertEqual(transaction_states, [False])

    def test_streaming_xlsx_without_dimension_materializes_all_profiled_sheets(self) -> None:
        from contextlib import ExitStack
        from openpyxl import Workbook

        workbook = Workbook()
        charge = workbook.active
        charge.title = "charge"
        charge.append(["charge_id", "amount"])
        charge.append(["C-1", 12.5])
        encounter = workbook.create_sheet("encounter")
        encounter.append(["encounter_id", "days"])
        encounter.append(["E-1", 3])
        original = BytesIO()
        workbook.save(original)
        workbook.close()

        rewritten = BytesIO()
        with ZipFile(BytesIO(original.getvalue())) as source, ZipFile(
            rewritten,
            "w",
            ZIP_DEFLATED,
        ) as destination:
            for member in source.infolist():
                content = source.read(member.filename)
                if member.filename.startswith("xl/worksheets/sheet"):
                    content = re.sub(rb"<dimension\b[^>]*/>", b"", content)
                destination.writestr(member, content)
        xlsx = rewritten.getvalue()
        digest = hashlib.sha256(xlsx).hexdigest()
        with self._database() as db:
            source = db.get(DataSource, self.source_id)
            bucket_file = BucketFile(
                id="raw-validation-xlsx",
                data_source_id=source.id,
                filename="validation.xlsx",
                stored_path="minio://validation-test/platform/raw/validation.xlsx",
                storage_provider="minio",
                bucket_name="validation-test",
                object_key="platform/raw/validation.xlsx",
                object_version_id="raw-xlsx-v1",
                object_url="minio://validation-test/platform/raw/validation.xlsx",
                size=len(xlsx),
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                content_sha256=digest,
                status="parsed",
            )
            asset = DataAsset(
                id="validation-xlsx-asset",
                tenant_id="tenant-validation-data",
                key="validation.workbook",
                name="validation.xlsx",
                media_type=bucket_file.mime,
                usage_plane="invocation_input",
                labels={"catalog_purpose": "validation_asset"},
                created_by_user_id="user-validation-data",
            )
            version = DataAssetVersion(
                id="validation-xlsx-version",
                tenant_id="tenant-validation-data",
                asset_id=asset.id,
                version_number=1,
                bucket_file_id=bucket_file.id,
                bucket_data_source_id=source.id,
                status="ready",
                content_sha256=digest,
                byte_size=len(xlsx),
                version_document={
                    "profile": {
                        "category": "table",
                        "extension": ".xlsx",
                        "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "tables": [
                            {
                                "name": "charge",
                                "relation_name": "validation__charge",
                                "header_row_index": 0,
                                "columns": [
                                    {"name": "charge_id", "logical_type": "string"},
                                    {"name": "amount", "logical_type": "number"},
                                ],
                            },
                            {
                                "name": "encounter",
                                "relation_name": "validation__encounter",
                                "header_row_index": 0,
                                "columns": [
                                    {"name": "encounter_id", "logical_type": "string"},
                                    {"name": "days", "logical_type": "integer"},
                                ],
                            },
                        ],
                    }
                },
                created_by_user_id="user-validation-data",
            )
            db.add_all([bucket_file, asset, version])
            db.commit()
        self.raw_objects[("validation-test", "platform/raw/validation.xlsx")] = xlsx

        payload = ValidationDatasetBuildIn(
            asset_version_ids=[version.id],
            name="Dimensionless workbook validation package",
        )
        with ExitStack() as stack:
            for active_patch in self._patches():
                stack.enter_context(active_patch)
            with self._database() as db:
                result = validation_dataset_service.build_validation_dataset(db, payload)

        self.assertEqual(
            result["relation_names"],
            ["validation__charge", "validation__encounter"],
        )
        self.assertEqual(result["record_count"], 2)

    def test_package_keeps_queryable_tables_when_an_output_template_has_no_rows(self) -> None:
        from contextlib import ExitStack
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "result"
        sheet.append(["finding_id", "reason"])
        output = BytesIO()
        workbook.save(output)
        workbook.close()

        template = output.getvalue()
        digest = hashlib.sha256(template).hexdigest()
        with self._database() as db:
            source = db.get(DataSource, self.source_id)
            bucket_file = BucketFile(
                id="raw-validation-output-template",
                data_source_id=source.id,
                filename="result-template.xlsx",
                stored_path="minio://validation-test/platform/raw/result-template.xlsx",
                storage_provider="minio",
                bucket_name="validation-test",
                object_key="platform/raw/result-template.xlsx",
                object_version_id="raw-output-template-v1",
                object_url="minio://validation-test/platform/raw/result-template.xlsx",
                size=len(template),
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                content_sha256=digest,
                status="parsed",
            )
            asset = DataAsset(
                id="validation-output-template-asset",
                tenant_id="tenant-validation-data",
                key="validation.output-template",
                name="result-template.xlsx",
                media_type=bucket_file.mime,
                usage_plane="invocation_input",
                labels={"catalog_purpose": "validation_asset"},
                created_by_user_id="user-validation-data",
            )
            version = DataAssetVersion(
                id="validation-output-template-version",
                tenant_id="tenant-validation-data",
                asset_id=asset.id,
                version_number=1,
                bucket_file_id=bucket_file.id,
                bucket_data_source_id=source.id,
                status="ready",
                content_sha256=digest,
                byte_size=len(template),
                version_document={
                    "profile": {
                        "category": "table",
                        "extension": ".xlsx",
                        "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "tables": [
                            {
                                "name": "result",
                                "relation_name": "result_template",
                                "header_row_index": 0,
                                "columns": [
                                    {"name": "finding_id", "logical_type": "string"},
                                    {"name": "reason", "logical_type": "string"},
                                ],
                            }
                        ],
                    }
                },
                created_by_user_id="user-validation-data",
            )
            db.add_all([bucket_file, asset, version])
            db.commit()
        self.raw_objects[
            ("validation-test", "platform/raw/result-template.xlsx")
        ] = template

        payload = ValidationDatasetBuildIn(
            asset_version_ids=[self.asset_version_id, version.id],
            name="Validation package with output template",
        )
        with ExitStack() as stack:
            for active_patch in self._patches():
                stack.enter_context(active_patch)
            with self._database() as db:
                result = validation_dataset_service.build_validation_dataset(db, payload)
            with self._database() as db:
                with self.assertRaisesRegex(
                    validation_dataset_service.ValidationDatasetError,
                    "没有可物化的表格数据",
                ):
                    validation_dataset_service.build_validation_dataset(
                        db,
                        ValidationDatasetBuildIn(
                            asset_version_ids=[version.id],
                            name="Output template only",
                        ),
                    )

        self.assertEqual(result["relation_names"], ["claims"])
        self.assertEqual(result["record_count"], 2)

    def test_validation_builder_rejects_cross_source_bucket_file_lineage(self) -> None:
        """A forged version->file pointer cannot smuggle modeling data into an Agent package."""
        agent_id = "validation-lineage-agent"
        with self._database() as db:
            agent = Agent(
                id=agent_id,
                tenant_id="tenant-validation-data",
                name="Lineage Agent",
            )
            source = DataSource(
                id="validation-lineage-source",
                tenant_id="tenant-validation-data",
                resource_scope="modeling",
                owner_agent_id=None,
                name="Modeling source",
                type="file_bucket",
                config={
                    "storage_backend": "minio",
                    "bucket_name": "validation-test",
                    "prefix": "platform",
                },
                status="ok",
            )
            file = BucketFile(
                id="validation-lineage-file",
                data_source_id=source.id,
                filename="forged.csv",
                stored_path="minio://validation-test/platform/raw/forged.csv",
                storage_provider="minio",
                bucket_name="validation-test",
                object_key="platform/raw/forged.csv",
                object_version_id="forged-v1",
                object_url="minio://validation-test/platform/raw/forged.csv",
                size=1,
                mime="text/csv",
                content_sha256="a" * 64,
                status="parsed",
            )
            version = db.get(DataAssetVersion, self.asset_version_id)
            assert version is not None
            db.add_all([agent, source, file])
            db.flush()
            db.get(DataAsset, "validation-csv-asset").owner_agent_id = agent_id
            # Keep the composite file/source FK valid while simulating a legacy
            # row written before the registration scope fence existed.
            version.bucket_file_id = file.id
            version.bucket_data_source_id = source.id
            db.commit()

            with self.assertRaisesRegex(
                validation_dataset_service.ValidationDatasetError,
                "来源作用域无效",
            ):
                validation_dataset_service.enqueue_validation_dataset_job(
                    db,
                    ValidationDatasetBuildIn(
                        asset_version_ids=[self.asset_version_id],
                        name="Reject forged lineage",
                        agent_id=agent_id,
                    ),
                )


if __name__ == "__main__":
    unittest.main()
