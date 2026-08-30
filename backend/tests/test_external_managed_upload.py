from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    BucketFile,
    DataAsset,
    DataAssetVersion,
    DataSource,
    ObjectDeletionJob,
    Tenant,
    User,
)
from app.routers import external_capabilities
from app.services import (
    catalog_ingestion_service,
    datasource_service,
    external_api_service,
    object_deletion_service,
    object_storage_service,
    operations_service,
    permission_service,
)
from sdk import CapabilityClient
from tests.test_object_storage import _FakeMinio


_DETACH_BLOB_REVISION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260829_11_detach_expired_asset_blobs.py"
)
_OPERATIONS_SERVICE = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "services"
    / "operations_service.py"
)


class ExternalManagedUploadMigrationContractTests(unittest.TestCase):
    def test_expiry_transition_uses_a_narrow_security_definer_contract(self) -> None:
        source = _DETACH_BLOB_REVISION.read_text(encoding="utf-8")
        upgrade_source = source.split("def downgrade()", 1)[0]
        downgrade_source = source.split("def downgrade()", 1)[1]

        for required in (
            "SECURITY DEFINER",
            "SET search_path = pg_catalog, public",
            "FOR UPDATE",
            "clock_timestamp()",
            "REVOKE ALL ON FUNCTION",
            "GRANT EXECUTE ON FUNCTION",
            "version.status = 'ready'",
            "'{lifecycle,purpose}'",
            "'{lifecycle,temporary}'",
            "detached_bucket_file_id",
            "detached_source_id",
        ):
            self.assertIn(required, upgrade_source)
        self.assertNotIn("GRANT UPDATE", upgrade_source)
        self.assertLess(
            downgrade_source.index("REVOKE EXECUTE ON FUNCTION"),
            downgrade_source.index(
                "DROP FUNCTION public.detach_expired_catalog_asset_blob"
            ),
        )

    def test_postgresql_service_defers_asset_version_lock_to_guarded_function(self) -> None:
        source = _OPERATIONS_SERVICE.read_text(encoding="utf-8")
        lifecycle_source = source.split(
            "def purge_expired_catalog_attachments", 1
        )[1].split("def worker_tick", 1)[0]

        self.assertIn('postgresql = db.get_bind().dialect.name == "postgresql"', lifecycle_source)
        self.assertIn("if not postgresql:", lifecycle_source)
        self.assertEqual(lifecycle_source.count(".with_for_update("), 1)
        self.assertLess(
            lifecycle_source.index("if not postgresql:"),
            lifecycle_source.index(".with_for_update("),
        )
        self.assertIn(
            "FROM public.detach_expired_catalog_asset_blob(:version_id)",
            lifecycle_source,
        )


class ExternalManagedUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.object_client = _FakeMinio()
        configuration = object_storage_service.MinioConfiguration(
            endpoint="minio.example.test",
            access_key="access",
            secret_key="secret",
            bucket_name="ontology",
            prefix="ontology-business",
        )
        for target, attribute, value in (
            (object_storage_service, "configuration", configuration),
            (object_storage_service, "get_client", self.object_client),
        ):
            active_patch = patch.object(target, attribute, return_value=value)
            active_patch.start()
            self.addCleanup(active_patch.stop)

        def prepare_test_upload(data_source, file_id, filename):
            return SimpleNamespace(
                object_key=datasource_service.build_bucket_object_key(
                    data_source,
                    file_id,
                    filename,
                    upload_id=uuid.uuid4().hex,
                )
            )

        for lifecycle_patch in (
            patch.object(
                object_deletion_service,
                "prepare_bucket_file_upload",
                side_effect=prepare_test_upload,
            ),
            patch.object(
                object_deletion_service,
                "heartbeat_upload_intent",
                side_effect=lambda _claim: nullcontext(),
            ),
            patch.object(object_deletion_service, "begin_upload_put"),
            patch.object(object_deletion_service, "assert_upload_active"),
            patch.object(object_deletion_service, "retain_bucket_file_upload"),
        ):
            lifecycle_patch.start()
            self.addCleanup(lifecycle_patch.stop)
        abandoned_patch = patch.object(
            object_deletion_service,
            "schedule_abandoned_upload_best_effort",
        )
        self.abandoned_upload = abandoned_patch.start()
        self.addCleanup(abandoned_patch.stop)

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
        with self.Session() as db:
            tenant = Tenant(id="tenant-external-upload", name="External upload tenant")
            user = User(
                id="user-external-upload",
                tenant_id=tenant.id,
                email="external-upload@example.test",
                password_hash="test-only",
                status="active",
            )
            db.add_all([tenant, user])
            db.commit()
            permission_service.ensure_organization(
                db,
                tenant.id,
                owner_user_id=user.id,
            )
            db.info["tenant_id"] = tenant.id
            db.info["user_id"] = user.id
            _key, self.upload_token = external_api_service.issue_key(
                db,
                tenant_id=tenant.id,
                user_id=user.id,
                issued_by_user_id=user.id,
                name="upload integration",
                scopes=["assets:write", "capabilities:invoke"],
                expires_in_days=30,
            )
            _narrow, self.narrow_token = external_api_service.issue_key(
                db,
                tenant_id=tenant.id,
                user_id=user.id,
                issued_by_user_id=user.id,
                name="invoke only",
                scopes=["capabilities:invoke"],
                expires_in_days=30,
            )
            db.commit()

        self.app = FastAPI()
        self.app.include_router(external_capabilities.router, prefix="/api")

        def override_db():
            session = self.Session()
            try:
                yield session
            finally:
                session.close()

        self.app.dependency_overrides[get_db] = override_db
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()

    def _headers(self, token: str | None = None) -> dict[str, str]:
        return {"X-API-Key": token or self.upload_token}

    def test_api_key_upload_creates_only_safe_temporary_asset_reference(self) -> None:
        response = self.client.post(
            "/api/external/v2/assets/upload",
            headers=self._headers(),
            data={"name": "requirements", "expires_in_seconds": "900"},
            files={"file": ("requirements.md", b"# Scope\nNew input", "text/markdown")},
        )
        self.assertEqual(response.status_code, 201, response.text)
        document = response.json()
        self.assertTrue(document["created"])
        self.assertTrue(document["temporary"])
        self.assertEqual(document["purpose"], "invocation_attachment")
        self.assertIsNotNone(document["expires_at"])
        self.assertEqual(len(document["version"]["content_sha256"]), 64)
        serialized = response.text.lower()
        for forbidden in (
            "minio://",
            "object_key",
            "bucket_name",
            "endpoint",
            "access_key",
            "secret_key",
        ):
            self.assertNotIn(forbidden, serialized)

        with self.Session() as db:
            source = db.scalar(select(DataSource))
            version = db.get(DataAssetVersion, document["version"]["id"])
            asset = db.get(DataAsset, document["asset"]["id"])
            bucket_file = db.get(BucketFile, version.bucket_file_id)
            self.assertIsNotNone(source)
            self.assertIsNone(source.scenario_id)
            self.assertEqual(source.type, "file_bucket")
            self.assertEqual(asset.tenant_id, "tenant-external-upload")
            self.assertEqual(version.tenant_id, "tenant-external-upload")
            self.assertEqual(
                datasource_service.read_bucket_file(bucket_file, source)[0],
                b"# Scope\nNew input",
            )

    def test_upload_scope_is_independent_from_capability_invoke(self) -> None:
        response = self.client.post(
            "/api/external/v2/assets/upload",
            headers=self._headers(self.narrow_token),
            files={"file": ("requirements.md", b"new input", "text/markdown")},
        )
        self.assertEqual(response.status_code, 403, response.text)
        with self.Session() as db:
            self.assertEqual(db.scalar(select(func.count(DataSource.id))), 0)

    def test_physical_storage_fields_are_rejected_before_bucket_creation(self) -> None:
        response = self.client.post(
            "/api/external/v2/assets/upload",
            headers=self._headers(),
            data={"file_bucket_id": "attacker-selected-source"},
            files={"file": ("requirements.md", b"new input", "text/markdown")},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("不得包含", response.json()["detail"])
        with self.Session() as db:
            self.assertEqual(db.scalar(select(func.count(DataSource.id))), 0)

    def test_same_content_reuses_the_same_temporary_version(self) -> None:
        request = {
            "headers": self._headers(),
            "files": {"file": ("same.md", b"same content", "text/markdown")},
        }
        first = self.client.post("/api/external/v2/assets/upload", **request)
        second = self.client.post("/api/external/v2/assets/upload", **request)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 201, second.text)
        self.assertTrue(first.json()["created"])
        self.assertFalse(second.json()["created"])
        self.assertEqual(first.json()["asset"]["id"], second.json()["asset"]["id"])
        self.assertEqual(
            first.json()["version"]["id"], second.json()["version"]["id"]
        )
        with self.Session() as db:
            self.assertEqual(db.scalar(select(func.count(DataAsset.id))), 1)
            self.assertEqual(db.scalar(select(func.count(DataAssetVersion.id))), 1)
            self.assertEqual(db.scalar(select(func.count(BucketFile.id))), 1)

    def test_expired_same_content_creates_a_new_immutable_version(self) -> None:
        request = {
            "headers": self._headers(),
            "files": {"file": ("same.md", b"same content", "text/markdown")},
        }
        first = self.client.post("/api/external/v2/assets/upload", **request)
        self.assertEqual(first.status_code, 201, first.text)
        with self.Session() as db:
            version = db.get(DataAssetVersion, first.json()["version"]["id"])
            document = dict(version.version_document)
            lifecycle = dict(document["lifecycle"])
            lifecycle["expires_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat()
            document["lifecycle"] = lifecycle
            version.version_document = document
            db.commit()

        second = self.client.post("/api/external/v2/assets/upload", **request)
        self.assertEqual(second.status_code, 201, second.text)
        self.assertTrue(second.json()["created"])
        self.assertEqual(first.json()["asset"]["id"], second.json()["asset"]["id"])
        self.assertNotEqual(
            first.json()["version"]["id"], second.json()["version"]["id"]
        )
        with self.Session() as db:
            versions = list(
                db.scalars(
                    select(DataAssetVersion)
                    .where(DataAssetVersion.asset_id == first.json()["asset"]["id"])
                    .order_by(DataAssetVersion.version_number)
                )
            )
            self.assertEqual([item.version_number for item in versions], [1, 2])
            self.assertEqual(versions[0].content_sha256, versions[1].content_sha256)
            self.assertEqual(db.scalar(select(func.count(BucketFile.id))), 2)

    def test_expiry_detaches_payload_but_retains_logical_audit_identity(self) -> None:
        response = self.client.post(
            "/api/external/v2/assets/upload",
            headers=self._headers(),
            files={"file": ("audit.md", b"audited input", "text/markdown")},
        )
        self.assertEqual(response.status_code, 201, response.text)
        version_id = response.json()["version"]["id"]
        asset_id = response.json()["asset"]["id"]
        with self.Session() as db:
            version = db.get(DataAssetVersion, version_id)
            bucket_file_id = version.bucket_file_id
            digest = version.content_sha256
            document = dict(version.version_document)
            lifecycle = dict(document["lifecycle"])
            lifecycle["expires_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat()
            document["lifecycle"] = lifecycle
            version.version_document = document
            db.commit()

            purged = operations_service.purge_expired_catalog_attachments(db)
            db.commit()

            retained = db.get(DataAssetVersion, version_id)
            self.assertEqual(purged, 1)
            self.assertIsNotNone(retained)
            self.assertEqual(retained.status, "retired")
            self.assertEqual(retained.content_sha256, digest)
            self.assertIsNone(retained.bucket_file_id)
            self.assertIsNone(retained.bucket_data_source_id)
            self.assertEqual(retained.source_locator, {})
            self.assertIsNone(db.get(BucketFile, bucket_file_id))
            self.assertEqual(db.get(DataAsset, asset_id).lifecycle_status, "active")
            deletion_job = db.scalar(
                select(ObjectDeletionJob).where(
                    ObjectDeletionJob.origin_id == bucket_file_id,
                    ObjectDeletionJob.origin_type == "bucket_file",
                )
            )
            self.assertIsNotNone(deletion_job)

    def test_late_duplicate_upload_schedules_exact_abandoned_object_cleanup(self) -> None:
        request = {
            "headers": self._headers(),
            "files": {"file": ("same.md", b"same content", "text/markdown")},
        }
        first = self.client.post("/api/external/v2/assets/upload", **request)
        self.assertEqual(first.status_code, 201, first.text)
        asset_id = first.json()["asset"]["id"]
        version_id = first.json()["version"]["id"]

        def stale_lookup(db, _prepared):
            return db.get(DataAsset, asset_id), None, False, False

        def concurrent_winner(db, _asset, _file_id, _prepared, **_kwargs):
            return db.get(DataAssetVersion, version_id)

        with (
            patch.object(
                catalog_ingestion_service,
                "find_or_create_asset",
                side_effect=stale_lookup,
            ),
            patch.object(
                catalog_ingestion_service,
                "register_prepared_version",
                side_effect=concurrent_winner,
            ),
        ):
            second = self.client.post("/api/external/v2/assets/upload", **request)

        self.assertEqual(second.status_code, 201, second.text)
        self.assertFalse(second.json()["created"])
        self.assertEqual(second.json()["version"]["id"], version_id)
        self.abandoned_upload.assert_called_once()
        abandoned_file = self.abandoned_upload.call_args.args[1]
        with self.Session() as db:
            self.assertIsNone(db.get(BucketFile, abandoned_file.id))
            self.assertEqual(db.scalar(select(func.count(BucketFile.id))), 1)

    def test_sdk_returns_asset_version_ready_for_managed_input(self) -> None:
        client = CapabilityClient(
            "https://testserver/api/external/v2",
            self.upload_token,
            http_client=self.client,
        )
        uploaded = client.upload_invocation_attachment(
            "requirements.md",
            b"# New requirements",
            content_type="text/markdown",
            expires_in_seconds=600,
        )
        self.assertTrue(uploaded["temporary"])
        self.assertIsInstance(uploaded["version"]["id"], str)
        self.assertEqual(
            uploaded["version"]["lifecycle"]["purpose"],
            "invocation_attachment",
        )
