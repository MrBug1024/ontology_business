"""Regression coverage for dataset-version asset ownership boundaries."""
from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.catalog_schemas import DatasetVersionCreate
from app.database import Base
from app.models import (
    Agent,
    BucketFile,
    BusinessScenario,
    DataAsset,
    DataAssetVersion,
    DataSource,
    DatasetSchema,
    LogicalDataset,
    Tenant,
    User,
)
from app.services import catalog_service, object_storage_service, permission_service


class CatalogDatasetVersionScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        tenant = Tenant(id="tenant-dataset-version", name="Dataset version scope")
        user = User(
            id="user-dataset-version",
            tenant_id=tenant.id,
            email="dataset-version@example.test",
            password_hash="test-only",
            status="active",
        )
        scenario = BusinessScenario(
            id="scenario-dataset-version",
            tenant_id=tenant.id,
            name="Dataset version scenario",
            status="active",
        )
        agent = Agent(
            id="agent-dataset-version",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            name="Private Agent",
        )
        source = DataSource(
            id="source-dataset-version",
            tenant_id=tenant.id,
            resource_scope="modeling",
            owner_agent_id=None,
            name="Modeling source",
            type="file_bucket",
            config={
                "storage_backend": "minio",
                "bucket_name": "dataset-version-test",
                "prefix": "platform",
            },
            status="ok",
        )
        bucket_file = BucketFile(
            id="file-dataset-version",
            data_source_id=source.id,
            filename="private.csv",
            stored_path="minio://dataset-version-test/platform/private.csv",
            storage_provider="minio",
            bucket_name="dataset-version-test",
            object_key="platform/private.csv",
            object_version_id="v1",
            object_url="minio://dataset-version-test/platform/private.csv",
            size=1,
            mime="text/csv",
            content_sha256="a" * 64,
            status="parsed",
        )
        asset = DataAsset(
            id="asset-dataset-version",
            tenant_id=tenant.id,
            owner_agent_id=agent.id,
            key="private.asset.version",
            name="Private Agent asset",
            kind="file",
            media_type="text/csv",
            usage_plane="invocation_input",
            lifecycle_status="active",
            labels={"catalog_purpose": "validation_asset"},
            created_by_user_id=user.id,
        )
        asset_version = DataAssetVersion(
            id="asset-version-dataset",
            tenant_id=tenant.id,
            asset_id=asset.id,
            version_number=1,
            bucket_file_id=bucket_file.id,
            bucket_data_source_id=source.id,
            provenance_kind="upload",
            status="ready",
            content_sha256="a" * 64,
            byte_size=1,
            source_locator={},
            version_document={},
            created_by_user_id=user.id,
        )
        dataset = LogicalDataset(
            id="dataset-version-target",
            tenant_id=tenant.id,
            key="global.dataset.target",
            name="Global target",
            usage_plane="modeling_material",
            lifecycle_status="active",
            labels={},
            created_by_user_id=user.id,
        )
        schema = DatasetSchema(
            id="schema-version-target",
            tenant_id=tenant.id,
            dataset_id=dataset.id,
            schema_version=1,
            schema_hash="b" * 64,
            compatibility="none",
            schema_document={},
            created_by_user_id=user.id,
        )
        self.db.add_all(
            [
                tenant,
                user,
                scenario,
                agent,
                source,
                bucket_file,
                asset,
                asset_version,
                dataset,
                schema,
            ]
        )
        self.db.commit()
        permission_service.ensure_organization(
            self.db,
            tenant.id,
            owner_user_id=user.id,
        )
        self.db.commit()
        self.db.info.update(tenant_id=tenant.id, user_id=user.id)
        self.dataset = dataset
        self.schema = schema
        self.asset_version = asset_version
        self._storage_config = patch.object(
            object_storage_service,
            "require_configuration",
            return_value=SimpleNamespace(
                bucket_name="dataset-version-test", prefix="platform"
            ),
        )
        self._storage_config.start()

    def tearDown(self) -> None:
        self._storage_config.stop()
        self.db.close()
        self.engine.dispose()

    def test_private_agent_asset_version_cannot_be_embedded_in_global_dataset(self) -> None:
        with self.assertRaises(catalog_service.CatalogError):
            catalog_service.create_dataset_version(
                self.db,
                self.dataset,
                DatasetVersionCreate(
                    schema_id=self.schema.id,
                    asset_version_ids=[self.asset_version.id],
                    manifest={"record_count": 1},
                ),
            )


if __name__ == "__main__":
    unittest.main()
