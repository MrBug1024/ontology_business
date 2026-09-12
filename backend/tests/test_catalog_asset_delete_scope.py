"""Regression tests for downward-only catalog asset deletion."""
from __future__ import annotations

import unittest

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

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
    IngestionRun,
    LogicalDataset,
    RunInputBinding,
    Tenant,
    User,
)
from app.routers import catalog
from app.services import catalog_service, permission_service


class CatalogAssetDeleteScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        tenant = Tenant(id="tenant-asset-delete-scope", name="Asset scope")
        user = User(
            id="user-asset-delete-scope",
            tenant_id=tenant.id,
            email="asset-scope@example.test",
            password_hash="test-only",
            status="active",
        )
        scenario = BusinessScenario(
            id="scenario-asset-delete-scope",
            tenant_id=tenant.id,
            name="Asset scope scenario",
        )
        agent = Agent(
            id="agent-asset-delete-scope",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            name="Scoped agent",
        )
        self.db.add_all([tenant, user, scenario, agent])
        self.db.commit()
        permission_service.ensure_organization(
            self.db, tenant.id, owner_user_id=user.id
        )
        self.db.commit()
        self.db.info.update(tenant_id=tenant.id, user_id=user.id)
        self.tenant = tenant
        self.agent = agent

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _asset_version(self, *, file_id: str | None = None) -> tuple[DataAsset, DataAssetVersion]:
        asset = DataAsset(
            id="asset-delete-scope",
            tenant_id=self.tenant.id,
            owner_agent_id=self.agent.id,
            key="agent.asset.delete.scope",
            name="Scoped upload",
            kind="file",
            usage_plane="invocation_input",
            lifecycle_status="active",
            labels={"catalog_purpose": "validation_asset"},
        )
        version = DataAssetVersion(
            id="asset-version-delete-scope",
            tenant_id=self.tenant.id,
            asset_id=asset.id,
            version_number=1,
            bucket_file_id=file_id,
            bucket_data_source_id="source-delete-scope" if file_id else None,
            provenance_kind="upload",
            status="ready",
            content_sha256="a" * 64,
            byte_size=1,
            source_locator={},
            version_document={},
        )
        self.db.add_all([asset, version])
        self.db.flush()
        return asset, version

    def _dataset_version(
        self,
        *,
        dataset_id: str,
        labels: dict,
        usage_plane: str,
        version_id: str,
    ) -> tuple[LogicalDataset, DatasetVersion]:
        dataset = LogicalDataset(
            id=dataset_id,
            tenant_id=self.tenant.id,
            key=f"{dataset_id}.key",
            name=dataset_id,
            usage_plane=usage_plane,
            lifecycle_status="active",
            labels=labels,
        )
        schema = DatasetSchema(
            id=f"schema-{dataset_id}",
            tenant_id=self.tenant.id,
            dataset_id=dataset.id,
            schema_version=1,
            schema_hash="b" * 64,
            compatibility="none",
            schema_document={},
        )
        version = DatasetVersion(
            id=version_id,
            tenant_id=self.tenant.id,
            dataset_id=dataset.id,
            schema_id=schema.id,
            version_number=1,
            status="ready",
            record_count=0,
            fragment_count=0,
            byte_size=0,
            content_hash="c" * 64,
            manifest={},
        )
        self.db.add_all([dataset, schema, version])
        self.db.flush()
        return dataset, version

    def test_independent_dataset_reference_is_blocked_before_mutation(self) -> None:
        asset, version = self._asset_version()
        _dataset, dataset_version = self._dataset_version(
            dataset_id="independent-delete-dataset",
            labels={"catalog_purpose": "ordinary_dataset"},
            usage_plane="modeling_material",
            version_id="independent-delete-version",
        )
        link = DatasetVersionAsset(
            tenant_id=self.tenant.id,
            dataset_id=dataset_version.dataset_id,
            dataset_version_id=dataset_version.id,
            asset_version_id=version.id,
            role="source",
            ordinal=0,
        )
        self.db.add(link)
        self.db.flush()

        with self.assertRaises(catalog_service.CatalogError):
            catalog._asset_deletion_dataset_scope(
                self.db,
                asset,
                [version],
                agent_id=self.agent.id,
            )
        self.assertEqual(self.db.get(DataAsset, asset.id).lifecycle_status, "active")
        self.assertIsNotNone(self.db.scalar(select(DatasetVersionAsset.id)))

    def test_same_agent_validation_dataset_is_the_only_allowed_child(self) -> None:
        asset, version = self._asset_version()
        dataset, dataset_version = self._dataset_version(
            dataset_id="owned-validation-dataset",
            labels={
                "catalog_purpose": "validation_dataset",
                "owner_agent_id": self.agent.id,
            },
            usage_plane="invocation_input",
            version_id="owned-validation-version",
        )
        link = DatasetVersionAsset(
            tenant_id=self.tenant.id,
            dataset_id=dataset.id,
            dataset_version_id=dataset_version.id,
            asset_version_id=version.id,
            role="source",
            ordinal=0,
        )
        self.db.add(link)
        self.db.flush()

        dataset_ids, version_ids = catalog._asset_deletion_dataset_scope(
            self.db,
            asset,
            [version],
            agent_id=self.agent.id,
        )
        self.assertEqual(dataset_ids, {dataset.id})
        self.assertEqual(version_ids, {dataset_version.id})

    def test_scenario_acl_viewer_can_list_and_delete_agent_asset(self) -> None:
        viewer = User(
            id="viewer-asset-delete-scope",
            tenant_id=self.tenant.id,
            email="viewer-asset-scope@example.test",
            password_hash="test-only",
            status="active",
        )
        self.db.add(viewer)
        self.db.flush()
        organization = permission_service.ensure_organization(
            self.db,
            self.tenant.id,
        )
        permission_service.assign_member_role(
            self.db,
            organization,
            user_id=viewer.id,
            role_key="viewer",
        )
        self.db.add(
            AuthorizationGrant(
                organization_id=organization.id,
                user_id=viewer.id,
                resource_type="scenario",
                resource_id=self.agent.scenario_id,
                verb="write",
                effect="allow",
                created_by_user_id="user-asset-delete-scope",
            )
        )
        asset, _version = self._asset_version()
        global_asset = DataAsset(
            id="global-asset-delete-scope",
            tenant_id=self.tenant.id,
            key="global.asset.delete.scope",
            name="Global asset",
            kind="file",
            usage_plane="invocation_input",
            lifecycle_status="active",
            labels={},
        )
        self.db.add(global_asset)
        self.db.commit()
        self.db.info["user_id"] = viewer.id
        permission_service.refresh_request_authorization(self.db)

        scenario = self.db.get(BusinessScenario, self.agent.scenario_id)
        self.assertIsNotNone(scenario)
        assert scenario is not None
        self.assertFalse(
            permission_service.check_tenant_permission(self.db, "write").allowed
        )
        self.assertTrue(
            permission_service.check_scenario(self.db, scenario, "write").allowed
        )

        listed = catalog.list_assets(
            usage_plane=None,
            agent_id=self.agent.id,
            db=self.db,
        )
        self.assertEqual([item.id for item in listed], [asset.id])
        versions = catalog.list_asset_versions(
            asset.id,
            agent_id=self.agent.id,
            db=self.db,
        )
        self.assertEqual([item.id for item in versions], [_version.id])
        deleted = catalog.delete_asset(
            asset.id,
            agent_id=self.agent.id,
            db=self.db,
        )
        self.assertEqual(deleted["asset_id"], asset.id)
        self.assertEqual(self.db.get(DataAsset, asset.id).lifecycle_status, "retired")

        with self.assertRaises(HTTPException) as denied:
            catalog.delete_asset(global_asset.id, agent_id=None, db=self.db)
        self.assertEqual(denied.exception.status_code, 403)

    def test_owned_validation_dataset_is_retired_with_deleted_asset(self) -> None:
        asset, version = self._asset_version()
        dataset, dataset_version = self._dataset_version(
            dataset_id="retire-validation-dataset",
            labels={
                "catalog_purpose": "validation_dataset",
                "owner_agent_id": self.agent.id,
            },
            usage_plane="invocation_input",
            version_id="retire-validation-version",
        )
        self.db.add(
            DatasetVersionAsset(
                tenant_id=self.tenant.id,
                dataset_id=dataset.id,
                dataset_version_id=dataset_version.id,
                asset_version_id=version.id,
                role="source",
                ordinal=0,
            )
        )
        self.db.add(
            IngestionRun(
                id="retire-validation-job",
                tenant_id=self.tenant.id,
                dataset_id=dataset.id,
                pipeline_kind="validation_dataset",
                idempotency_key="retire-validation-idempotency",
                status="pending",
            )
        )
        self.db.flush()

        cleanup = catalog._retire_owned_validation_dataset_children(
            self.db,
            tenant_id=self.tenant.id,
            agent_id=self.agent.id,
            dataset_ids={dataset.id},
        )
        self.assertEqual(cleanup["validation_datasets_retired"], 1)
        self.assertEqual(cleanup["validation_versions_retired"], 1)
        self.assertEqual(cleanup["validation_jobs_cancelled"], 1)
        self.assertEqual(self.db.get(LogicalDataset, dataset.id).lifecycle_status, "retired")
        self.assertEqual(self.db.get(DatasetVersion, dataset_version.id).status, "retired")
        self.assertEqual(self.db.get(IngestionRun, "retire-validation-job").status, "cancelled")

    def test_retirement_rejects_non_owned_validation_dataset(self) -> None:
        _asset, _version = self._asset_version()
        dataset, _dataset_version = self._dataset_version(
            dataset_id="other-agent-validation-dataset",
            labels={
                "catalog_purpose": "validation_dataset",
                "owner_agent_id": "another-agent",
            },
            usage_plane="invocation_input",
            version_id="other-agent-validation-version",
        )
        self.db.flush()

        with self.assertRaises(catalog_service.CatalogError):
            catalog._retire_owned_validation_dataset_children(
                self.db,
                tenant_id=self.tenant.id,
                agent_id=self.agent.id,
                dataset_ids={dataset.id},
            )
        self.assertEqual(self.db.get(LogicalDataset, dataset.id).lifecycle_status, "active")

    def test_file_manifest_from_independent_dataset_is_blocked(self) -> None:
        source = DataSource(
            id="source-delete-scope",
            tenant_id=self.tenant.id,
            name="Upload source",
            type="file_bucket",
            resource_scope="agent_runtime",
            config={},
        )
        file = BucketFile(
            id="file-delete-scope",
            data_source_id=source.id,
            filename="upload.txt",
            stored_path="minio://bucket/upload.txt",
            storage_provider="minio",
            bucket_name="bucket",
            object_key="upload.txt",
            object_version_id="v1",
            size=1,
            mime="text/plain",
        )
        self.db.add_all([source, file])
        self.db.flush()
        asset, version = self._asset_version(file_id=file.id)
        _dataset, independent_version = self._dataset_version(
            dataset_id="manifest-independent-dataset",
            labels={},
            usage_plane="modeling_material",
            version_id="manifest-independent-version",
        )
        independent_version.manifest_bucket_file_id = file.id
        independent_version.manifest_data_source_id = source.id
        self.db.flush()

        with self.assertRaises(catalog_service.CatalogError):
            catalog._assert_scoped_asset_file_references(
                self.db,
                asset,
                [file],
                agent=self.agent,
                allowed_dataset_ids=set(),
                allowed_dataset_version_ids=set(),
                owned_run_ids=set(),
            )
        self.assertEqual(independent_version.manifest_bucket_file_id, file.id)

    def test_file_manifest_from_another_version_is_blocked(self) -> None:
        source = DataSource(
            id="source-delete-scope-version",
            tenant_id=self.tenant.id,
            name="Upload source",
            type="file_bucket",
            resource_scope="agent_runtime",
            config={},
        )
        file = BucketFile(
            id="file-delete-scope-version",
            data_source_id=source.id,
            filename="upload.txt",
            stored_path="minio://bucket/upload-version.txt",
            storage_provider="minio",
            bucket_name="bucket",
            object_key="upload-version.txt",
            object_version_id="v1",
            size=1,
            mime="text/plain",
        )
        self.db.add_all([source, file])
        self.db.flush()
        asset, asset_version = self._asset_version(file_id=file.id)
        dataset, owned_version = self._dataset_version(
            dataset_id="owned-validation-version-dataset",
            labels={
                "catalog_purpose": "validation_dataset",
                "owner_agent_id": self.agent.id,
            },
            usage_plane="invocation_input",
            version_id="owned-validation-version-one",
        )
        sibling_version = DatasetVersion(
            id="owned-validation-version-two",
            tenant_id=self.tenant.id,
            dataset_id=dataset.id,
            schema_id=f"schema-{dataset.id}",
            version_number=2,
            status="ready",
            record_count=0,
            fragment_count=0,
            byte_size=0,
            content_hash="d" * 64,
            manifest={},
        )
        self.db.add(sibling_version)
        self.db.add(
            DatasetVersionAsset(
                tenant_id=self.tenant.id,
                dataset_id=dataset.id,
                dataset_version_id=owned_version.id,
                asset_version_id=asset_version.id,
                role="source",
                ordinal=0,
            )
        )
        sibling_version.manifest_bucket_file_id = file.id
        sibling_version.manifest_data_source_id = source.id
        self.db.flush()

        with self.assertRaises(catalog_service.CatalogError):
            catalog._assert_scoped_asset_file_references(
                self.db,
                asset,
                [file],
                agent=self.agent,
                allowed_dataset_ids={dataset.id},
                allowed_dataset_version_ids={owned_version.id},
                owned_run_ids=set(),
            )
        self.assertEqual(sibling_version.manifest_bucket_file_id, file.id)

    def test_direct_invocation_asset_reference_is_blocked(self) -> None:
        source = DataSource(
            id="source-delete-scope",
            tenant_id=self.tenant.id,
            name="Upload source",
            type="file_bucket",
            resource_scope="agent_runtime",
            config={},
        )
        file = BucketFile(
            id="file-delete-scope-direct",
            data_source_id=source.id,
            filename="upload.txt",
            stored_path="minio://bucket/upload-direct.txt",
            storage_provider="minio",
            bucket_name="bucket",
            object_key="upload-direct.txt",
            object_version_id="v1",
            size=1,
            mime="text/plain",
        )
        self.db.add_all([source, file])
        self.db.flush()
        asset, version = self._asset_version(file_id=file.id)
        self.db.add(
            RunInputBinding(
                id="run-input-delete-scope",
                tenant_id=self.tenant.id,
                scenario_id=self.agent.scenario_id,
                invocation_id="invocation-delete-scope",
                capability_port_id="port-delete-scope",
                ordinal=0,
                source_kind="asset_version",
                asset_version_id=version.id,
                content_hash="e" * 64,
                schema_hash="f" * 64,
                status="provided",
                binding_document={},
            )
        )
        self.db.flush()

        with self.assertRaises(catalog_service.CatalogError):
            catalog._assert_scoped_asset_file_references(
                self.db,
                asset,
                [file],
                agent=self.agent,
                allowed_dataset_ids=set(),
                allowed_dataset_version_ids=set(),
                owned_run_ids=set(),
            )


if __name__ == "__main__":
    unittest.main()
