"""Regression coverage for asset-version source ownership boundaries."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.catalog_schemas import DataAssetVersionRegister
from app.database import Base
from app.models import (
    Agent,
    AuthorizationGrant,
    BucketFile,
    BusinessScenario,
    DataAsset,
    DataAssetVersion,
    DataSource,
    DatasetSchema,
    DatasetVersion,
    DatasetVersionAsset,
    LogicalDataset,
    ManagedUploadRun,
    Tenant,
    User,
)
from app.services import catalog_service, object_storage_service, permission_service


class CatalogAssetSourceScopeTests(unittest.TestCase):
    """A bucket-file id must not bridge Agent or source namespaces."""

    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)

        tenant = Tenant(id="tenant-asset-source", name="Asset source scope")
        user = User(
            id="user-asset-source",
            tenant_id=tenant.id,
            email="asset-source@example.test",
            password_hash="test-only",
            status="active",
        )
        scenario = BusinessScenario(
            id="scenario-asset-source",
            tenant_id=tenant.id,
            name="Asset source scope scenario",
            status="active",
        )
        agent_one = Agent(
            id="agent-asset-source-one",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            name="Agent one",
        )
        agent_two = Agent(
            id="agent-asset-source-two",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            name="Agent two",
        )
        self.db.add_all([tenant, user, scenario, agent_one, agent_two])
        self.db.commit()
        permission_service.ensure_organization(
            self.db,
            tenant.id,
            owner_user_id=user.id,
        )
        self.db.commit()
        self.db.info.update(tenant_id=tenant.id, user_id=user.id)

        managed_config = {
            "storage_backend": "minio",
            "bucket_name": "catalog-test",
            "prefix": "platform",
        }
        sources = {
            "owned": DataSource(
                id="source-asset-owned",
                tenant_id=tenant.id,
                scenario_id=scenario.id,
                resource_scope="agent_runtime",
                owner_agent_id=agent_one.id,
                name="Agent one runtime",
                type="file_bucket",
                config=dict(managed_config),
                status="ok",
            ),
            "foreign": DataSource(
                id="source-asset-foreign",
                tenant_id=tenant.id,
                scenario_id=scenario.id,
                resource_scope="agent_runtime",
                owner_agent_id=agent_two.id,
                name="Agent two runtime",
                type="file_bucket",
                config=dict(managed_config),
                status="ok",
            ),
            "shared": DataSource(
                id="source-asset-shared",
                tenant_id=tenant.id,
                scenario_id=None,
                resource_scope="agent_runtime",
                owner_agent_id=None,
                name="Shared external runtime",
                type="file_bucket",
                config=dict(managed_config),
                status="ok",
            ),
            "external": DataSource(
                id=catalog_service.external_upload_source_id(tenant.id),
                tenant_id=tenant.id,
                scenario_id=None,
                resource_scope="agent_runtime",
                owner_agent_id=None,
                name="Managed external runtime",
                type="file_bucket",
                config=dict(managed_config),
                status="ok",
            ),
            "modeling": DataSource(
                id="source-asset-modeling",
                tenant_id=tenant.id,
                scenario_id=None,
                resource_scope="modeling",
                owner_agent_id=None,
                name="Modeling material",
                type="file_bucket",
                config=dict(managed_config),
                status="ok",
            ),
            "unmanaged": DataSource(
                id="source-asset-unmanaged",
                tenant_id=tenant.id,
                scenario_id=scenario.id,
                resource_scope="agent_runtime",
                owner_agent_id=agent_one.id,
                name="Unmanaged Agent source",
                type="file_bucket",
                config={"storage_backend": "local", "path": "ignored"},
                status="ok",
            ),
        }
        self.db.add_all(sources.values())
        self.asset = DataAsset(
            id="asset-source-scope",
            tenant_id=tenant.id,
            owner_agent_id=agent_one.id,
            key="agent.asset.source.scope",
            name="Agent one asset",
            kind="file",
            media_type="text/plain",
            usage_plane="invocation_input",
            lifecycle_status="active",
            labels={"catalog_purpose": "validation_asset"},
            created_by_user_id=user.id,
        )
        self.global_asset = DataAsset(
            id="asset-source-global",
            tenant_id=tenant.id,
            owner_agent_id=None,
            key="global.asset.source.scope",
            name="Global asset",
            kind="file",
            media_type="text/plain",
            usage_plane="invocation_input",
            lifecycle_status="active",
            labels={"catalog_purpose": "invocation_attachment"},
            created_by_user_id=user.id,
        )
        self.agent_two_asset = DataAsset(
            id="asset-source-scope-two",
            tenant_id=tenant.id,
            owner_agent_id=agent_two.id,
            key="agent.asset.source.scope.two",
            name="Agent two asset",
            kind="file",
            media_type="text/plain",
            usage_plane="invocation_input",
            lifecycle_status="active",
            labels={"catalog_purpose": "validation_asset"},
            created_by_user_id=user.id,
        )
        self.db.add_all([self.asset, self.agent_two_asset, self.global_asset])
        self.files: dict[str, BucketFile] = {}
        for name, source in sources.items():
            file = BucketFile(
                id=f"file-asset-{name}",
                data_source_id=source.id,
                filename=f"{name}.txt",
                stored_path=f"minio://catalog-test/platform/file-asset-{name}",
                storage_provider="minio",
                bucket_name="catalog-test",
                object_key=f"platform/file-asset-{name}",
                object_version_id="v1",
                object_url=f"minio://catalog-test/platform/file-asset-{name}",
                size=1,
                mime="text/plain",
                content_sha256=("b" if name == "external" else "a") * 64,
                status="parsed",
            )
            self.files[name] = file
            self.db.add(file)
        self.db.commit()

        # Seed a durable Agent-one lineage for the deterministic shared
        # external bucket.  Registration into another namespace must reject
        # this exact file even when the trusted shared-source flag is set.
        lineage_version = DataAssetVersion(
            id="version-source-lineage-one",
            tenant_id=tenant.id,
            asset_id=self.asset.id,
            version_number=1,
            bucket_file_id=self.files["external"].id,
            bucket_data_source_id=sources["external"].id,
            provenance_kind="upload",
            status="ready",
            content_sha256="b" * 64,
            byte_size=1,
            source_locator={},
            version_document={},
            created_by_user_id=user.id,
        )
        self.db.add(lineage_version)
        self.db.flush()
        self.db.add(
            ManagedUploadRun(
                id="run-source-lineage-one",
                tenant_id=tenant.id,
                owner_agent_id=agent_one.id,
                requested_by_user_id=user.id,
                data_source_id=sources["external"].id,
                bucket_file_id=self.files["external"].id,
                asset_id=self.asset.id,
                asset_version_id=lineage_version.id,
                idempotency_key="source-lineage-one",
                request_fingerprint="c" * 64,
                purpose="validation_asset",
                filename="external.txt",
                client_media_type="text/plain",
                declared_byte_size=1,
                byte_size=1,
                content_sha256="b" * 64,
                status="ready",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                finished_at=datetime.now(timezone.utc),
            )
        )
        self.db.commit()
        self.organization_id = permission_service.ensure_organization(
            self.db,
            tenant.id,
            owner_user_id=user.id,
        ).id
        self.db.commit()

        self._storage_config = patch.object(
            object_storage_service,
            "require_configuration",
            return_value=SimpleNamespace(bucket_name="catalog-test", prefix="platform"),
        )
        self._storage_config.start()

    def tearDown(self) -> None:
        self._storage_config.stop()
        self.db.close()
        self.engine.dispose()

    def _register(
        self,
        source_name: str,
        *,
        asset: DataAsset | None = None,
        allow_shared_runtime_source: bool = False,
    ):
        return catalog_service.register_asset_version(
            self.db,
            asset or self.asset,
            DataAssetVersionRegister(
                bucket_file_id=self.files[source_name].id,
                provenance_kind="upload",
            ),
            allow_shared_runtime_source=allow_shared_runtime_source,
        )

    def test_agent_asset_rejects_foreign_shared_modeling_and_unmanaged_sources(self) -> None:
        for source_name in ("foreign", "shared", "modeling", "unmanaged"):
            with self.subTest(source_name=source_name):
                with self.assertRaises(catalog_service.CatalogError):
                    self._register(source_name)
                self.db.rollback()

    def test_global_asset_rejects_shared_agent_runtime_source_by_default(self) -> None:
        with self.assertRaises(catalog_service.CatalogError):
            self._register("shared", asset=self.global_asset)
        self.db.rollback()

    def test_scenario_modeling_source_requires_write_acl(self) -> None:
        source = self.db.get(DataSource, "source-asset-modeling")
        assert source is not None
        source.scenario_id = "scenario-asset-source"
        self.db.add(
            AuthorizationGrant(
                organization_id=self.organization_id,
                user_id="user-asset-source",
                resource_type="scenario",
                resource_id="scenario-asset-source",
                verb="write",
                effect="deny",
                created_by_user_id="user-asset-source",
            )
        )
        self.db.commit()
        permission_service.refresh_request_authorization(self.db)

        with self.assertRaises(catalog_service.CatalogError):
            self._register("modeling", asset=self.global_asset)
        self.db.rollback()

    def test_agent_lineage_file_cannot_be_rehomed_to_other_agent_or_global_asset(self) -> None:
        for target in (self.agent_two_asset, self.global_asset):
            with self.subTest(target=target.id):
                with self.assertRaises(catalog_service.CatalogError):
                    self._register(
                        "external",
                        asset=target,
                        allow_shared_runtime_source=True,
                    )
                self.db.rollback()

    def test_global_validation_dataset_rejects_private_asset_lineage(self) -> None:
        dataset = LogicalDataset(
            id="dataset-malformed-global-validation",
            tenant_id="tenant-asset-source",
            key="malformed.global.validation",
            name="Malformed global validation dataset",
            usage_plane="invocation_input",
            lifecycle_status="active",
            labels={"catalog_purpose": "validation_dataset", "owner_agent_id": None},
            created_by_user_id="user-asset-source",
        )
        schema = DatasetSchema(
            id="schema-malformed-global-validation",
            tenant_id=dataset.tenant_id,
            dataset_id=dataset.id,
            schema_version=1,
            schema_hash="d" * 64,
            compatibility="none",
            schema_document={},
            created_by_user_id="user-asset-source",
        )
        version = DatasetVersion(
            id="version-malformed-global-validation",
            tenant_id=dataset.tenant_id,
            dataset_id=dataset.id,
            schema_id=schema.id,
            version_number=1,
            status="ready",
            content_hash="e" * 64,
            manifest={},
            created_by_user_id="user-asset-source",
        )
        link = DatasetVersionAsset(
            tenant_id=dataset.tenant_id,
            dataset_id=dataset.id,
            dataset_version_id=version.id,
            asset_version_id="version-source-lineage-one",
            role="source",
            ordinal=0,
            binding_document={},
        )
        self.db.add_all([dataset, schema, version, link])
        self.db.commit()

        self.assertNotIn(
            dataset.id,
            {item.id for item in catalog_service.list_datasets(self.db)},
        )
        with self.assertRaises(catalog_service.CatalogError):
            catalog_service.require_dataset(self.db, dataset.id)
        self.db.rollback()

    def test_global_validation_dataset_without_source_marker_is_hidden(self) -> None:
        """An ownerless package without immutable links must fail closed."""

        dataset = LogicalDataset(
            id="dataset-empty-global-validation",
            tenant_id="tenant-asset-source",
            key="empty.global.validation",
            name="Empty global validation dataset",
            usage_plane="invocation_input",
            lifecycle_status="active",
            labels={"catalog_purpose": "validation_dataset"},
            created_by_user_id="user-asset-source",
        )
        self.db.add(dataset)
        self.db.commit()

        self.assertNotIn(
            dataset.id,
            {item.id for item in catalog_service.list_datasets(self.db)},
        )
        with self.assertRaises(catalog_service.CatalogError):
            catalog_service.require_dataset(self.db, dataset.id)
        self.db.rollback()

    def test_agent_asset_accepts_same_agent_managed_runtime_source(self) -> None:
        version = self._register("owned")
        self.assertEqual(version.asset_id, self.asset.id)
        self.assertEqual(version.bucket_file_id, self.files["owned"].id)
        self.assertEqual(version.status, "ready")


if __name__ == "__main__":
    unittest.main()
