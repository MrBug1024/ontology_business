"""Regression tests for permanently deleting retired business scenarios."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    ActionExecutionLog,
    Agent,
    ArtifactTemplate,
    ArtifactTemplateVersion,
    Assertion,
    BucketFile,
    BusinessScenario,
    CapabilityInvocation,
    DataAsset,
    DataAssetVersion,
    DataMapping,
    DataSource,
    DocumentIndexJob,
    DerivationEvidence,
    LogicalDataset,
    LLMInvocationTrace,
    ManagedUploadRun,
    OntologyEntity,
    ReasoningTerm,
    Tenant,
    User,
)
from app.routers import scenarios as scenarios_router
from app.services import (
    object_storage_service,
    permission_service,
    scenario_purge_asset_service,
    scenario_purge_service,
)
from app.services.auth_service import get_current_user


class ScenarioPurgeTests(unittest.TestCase):
    """Scenario purge must remove RESTRICT audit edges before their parents."""

    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        event.listen(
            self.engine,
            "connect",
            lambda connection, _record: connection.execute("PRAGMA foreign_keys=ON"),
        )
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        Base.metadata.create_all(self.engine)

        db = self.Session()
        self.tenant = Tenant(id="tenant-purge", name="永久删除测试租户")
        self.user = User(
            id="user-purge",
            tenant_id=self.tenant.id,
            email="purge-owner@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-purge",
            tenant_id=self.tenant.id,
            name="含推理证据的退役场景",
            status="retired",
        )
        scenario_source = DataSource(
            id="source-purge",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="scenario-owned uploads",
            type="file_bucket",
            resource_scope="modeling",
            config={"storage_backend": "minio"},
        )
        db.add_all([self.tenant, self.user, self.scenario, scenario_source])
        db.flush()
        subject = ReasoningTerm(
            id="term-purge-subject",
            tenant_id=self.tenant.id,
            kind="literal",
            literal_value="subject",
            canonical_hash="a" * 64,
        )
        object_term = ReasoningTerm(
            id="term-purge-object",
            tenant_id=self.tenant.id,
            kind="literal",
            literal_value="object",
            canonical_hash="b" * 64,
        )
        assertion = Assertion(
            id="assertion-purge",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            subject_term_id=subject.id,
            object_term_id=object_term.id,
            predicate_key="purge.evidence",
            assertion_kind="observed",
            canonical_hash="c" * 64,
        )
        action_log = ActionExecutionLog(
            id="action-log-purge",
            scenario_id=self.scenario.id,
            target_id="action-purge",
        )
        evidence = DerivationEvidence(
            id="evidence-purge",
            tenant_id=self.tenant.id,
            assertion_id=assertion.id,
            ordinal=0,
            action_execution_log_id=action_log.id,
            action_scenario_id=self.scenario.id,
            content_hash="d" * 64,
        )
        db.add_all([subject, object_term])
        db.flush()
        db.add_all([assertion, action_log])
        db.flush()
        db.add(evidence)
        permission_service.ensure_organization(
            db, self.tenant.id, owner_user_id=self.user.id
        )
        db.commit()
        db.close()

        self.app = FastAPI()
        self.app.include_router(scenarios_router.router, prefix="/api")

        def override_current_user():
            return SimpleNamespace(id=self.user.id, tenant_id=self.tenant.id)

        def override_db():
            request_db = self.Session()
            request_db.info["user_id"] = self.user.id
            request_db.info["tenant_id"] = self.tenant.id
            try:
                yield request_db
            finally:
                request_db.close()

        self.app.dependency_overrides[get_current_user] = override_current_user
        self.app.dependency_overrides[get_db] = override_db
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()

    def test_purge_deletes_evidence_before_restricted_audit_parents(self) -> None:
        plan = self.client.get(
            f"/api/scenarios/{self.scenario.id}/purge-plan"
        )
        self.assertEqual(plan.status_code, 200, plan.text)
        self.assertEqual(plan.json()["counts"]["derivation_evidence"], 1)
        self.assertTrue(plan.json()["requires_audit_confirmation"])

        response = self.client.post(
            f"/api/scenarios/{self.scenario.id}/purge",
            json={
                "expected_name": self.scenario.name,
                "confirmed": True,
                "delete_audit_history": True,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

        db = self.Session()
        try:
            self.assertIsNone(db.get(BusinessScenario, self.scenario.id))
            self.assertIsNone(db.get(DerivationEvidence, "evidence-purge"))
            self.assertIsNone(db.get(Assertion, "assertion-purge"))
            self.assertIsNone(db.get(ActionExecutionLog, "action-log-purge"))
            self.assertIsNone(
                db.get(DataSource, "source-purge"),
                "purging a retired scenario must remove its owned data sources",
            )
            self.assertEqual(
                {term.id for term in db.scalars(select(ReasoningTerm)).all()},
                {"term-purge-subject", "term-purge-object"},
            )
        finally:
            db.close()

    def test_purge_blocks_active_document_index_job_and_keeps_source(self) -> None:
        db = self.Session()
        bucket_file = BucketFile(
            id="file-purge-index",
            data_source_id="source-purge",
            filename="pending.txt",
            stored_path="minio://tenant-purge/source-purge/pending.txt",
            bucket_name="tenant-purge",
            object_key="source-purge/pending.txt",
        )
        index_job = DocumentIndexJob(
            id="index-purge-active",
            tenant_id=self.tenant.id,
            data_source_id="source-purge",
            bucket_file_id=bucket_file.id,
            active_key=bucket_file.id,
            status="running",
        )
        db.add_all([bucket_file, index_job])
        db.commit()
        db.close()

        plan = self.client.get(f"/api/scenarios/{self.scenario.id}/purge-plan")
        self.assertEqual(plan.status_code, 200, plan.text)
        self.assertFalse(plan.json()["can_purge"])
        self.assertIn("进行中的文档索引任务", "；".join(plan.json()["blockers"]))

        response = self.client.post(
            f"/api/scenarios/{self.scenario.id}/purge",
            json={
                "expected_name": self.scenario.name,
                "confirmed": True,
                "delete_audit_history": True,
            },
        )
        self.assertEqual(response.status_code, 409, response.text)
        db = self.Session()
        try:
            self.assertIsNotNone(db.get(BusinessScenario, self.scenario.id))
            self.assertIsNotNone(db.get(DataSource, "source-purge"))
            self.assertIsNotNone(db.get(BucketFile, bucket_file.id))
            self.assertIsNotNone(db.get(DocumentIndexJob, index_job.id))
        finally:
            db.close()

    def test_purge_blocks_source_referenced_by_another_scenario(self) -> None:
        db = self.Session()
        other_scenario = BusinessScenario(
            id="scenario-purge-other",
            tenant_id=self.tenant.id,
            name="另一场景",
            status="retired",
        )
        other_entity = OntologyEntity(
            id="entity-purge-other",
            scenario_id=other_scenario.id,
            name="另一场景对象",
        )
        cross_mapping = DataMapping(
            id="mapping-purge-cross",
            scenario_id=other_scenario.id,
            entity_id=other_entity.id,
            data_source_id="source-purge",
            table_name="shared_table",
        )
        db.add_all([other_scenario, other_entity, cross_mapping])
        db.commit()
        db.close()

        plan = self.client.get(f"/api/scenarios/{self.scenario.id}/purge-plan")
        self.assertEqual(plan.status_code, 200, plan.text)
        self.assertFalse(plan.json()["can_purge"])
        self.assertIn("其他场景或平台资源引用", "；".join(plan.json()["blockers"]))

        response = self.client.post(
            f"/api/scenarios/{self.scenario.id}/purge",
            json={
                "expected_name": self.scenario.name,
                "confirmed": True,
                "delete_audit_history": True,
            },
        )
        self.assertEqual(response.status_code, 409, response.text)
        db = self.Session()
        try:
            self.assertIsNotNone(db.get(BusinessScenario, self.scenario.id))
            self.assertIsNotNone(db.get(DataSource, "source-purge"))
            self.assertIsNotNone(db.get(DataMapping, cross_mapping.id))
        finally:
            db.close()

    def test_purge_detaches_owned_modeling_catalog_assets(self) -> None:
        db = self.Session()
        source = db.get(DataSource, "source-purge")
        assert source is not None
        source.config = {
            "storage_backend": "minio",
            "bucket_name": "ontology",
            "prefix": "ontology-business",
        }
        object_key = "ontology-business/legacy/scenario-purge/model.csv"
        object_url = object_storage_service.stable_object_url("ontology", object_key)
        bucket_file = BucketFile(
            id="f" * 32,
            data_source_id=source.id,
            filename="model.csv",
            stored_path=object_url,
            storage_provider="minio",
            bucket_name="ontology",
            object_key=object_key,
            object_url=object_url,
            size=8,
            mime="text/csv",
            content_sha256="a" * 64,
            status="parsed",
        )
        asset = DataAsset(
            id="asset-purge-modeling",
            tenant_id=self.tenant.id,
            key="scenario.purge.modeling.asset",
            name="Modeling source",
            kind="file",
            lifecycle_status="active",
            usage_plane="modeling_material",
        )
        asset_version = DataAssetVersion(
            id="asset-version-purge-modeling",
            tenant_id=self.tenant.id,
            asset_id=asset.id,
            version_number=1,
            bucket_file_id=bucket_file.id,
            bucket_data_source_id=source.id,
            provenance_kind="upload",
            status="ready",
            content_sha256=bucket_file.content_sha256,
            byte_size=bucket_file.size,
            source_locator={"bucket_name": "ontology", "object_key": object_key},
            version_document={},
        )
        contract_source = LogicalDataset(
            id="dataset-purge-modeling",
            tenant_id=self.tenant.id,
            key=f"modeling.contract.{source.id}.{bucket_file.content_sha256}",
            name="Modeling contract source",
            lifecycle_status="active",
            usage_plane="modeling_material",
            labels={
                "catalog_purpose": "modeling_contract_source",
                "modeling_source_data_source_id": source.id,
                "modeling_source_bucket_file_id": bucket_file.id,
            },
        )
        db.add_all([bucket_file, asset, asset_version, contract_source])
        db.commit()
        db.close()

        plan = self.client.get(f"/api/scenarios/{self.scenario.id}/purge-plan")
        self.assertEqual(plan.status_code, 200, plan.text)
        self.assertTrue(plan.json()["can_purge"], plan.text)

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
            patch.object(
                scenario_purge_service.object_deletion_service,
                "drain_jobs_best_effort",
                return_value=0,
            ),
        ):
            response = self.client.post(
                f"/api/scenarios/{self.scenario.id}/purge",
                json={
                    "expected_name": self.scenario.name,
                    "confirmed": True,
                    "delete_audit_history": True,
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["deletion_jobs"], 1)

        with self.Session() as db:
            self.assertIsNone(db.get(BusinessScenario, self.scenario.id))
            self.assertIsNone(db.get(DataSource, source.id))
            self.assertIsNone(db.get(BucketFile, bucket_file.id))
            self.assertIsNotNone(db.get(DataAsset, asset.id))
            detached_version = db.get(DataAssetVersion, asset_version.id)
            self.assertIsNotNone(detached_version)
            assert detached_version is not None
            self.assertEqual(detached_version.status, "retired")
            self.assertIsNone(detached_version.bucket_file_id)
            self.assertIsNone(detached_version.bucket_data_source_id)
            self.assertEqual(detached_version.source_locator, {})
            retired_contract_source = db.get(LogicalDataset, contract_source.id)
            self.assertIsNotNone(retired_contract_source)
            assert retired_contract_source is not None
            self.assertEqual(retired_contract_source.lifecycle_status, "retired")

    def test_purge_blocks_file_referenced_by_agent_in_another_scenario(self) -> None:
        """A physical file shared by another Agent must remain untouched."""

        db = self.Session()
        other_scenario = BusinessScenario(
            id="scenario-purge-shared-other",
            tenant_id=self.tenant.id,
            name="共享文件的另一场景",
            status="active",
        )
        other_agent = Agent(
            id="agent-purge-shared-other",
            tenant_id=self.tenant.id,
            scenario_id=other_scenario.id,
            name="共享文件 Agent",
        )
        bucket_file = BucketFile(
            id="file-purge-shared-agent",
            data_source_id="source-purge",
            filename="shared.txt",
            stored_path="minio://tenant-purge/source-purge/shared.txt",
            storage_provider="minio",
            bucket_name="tenant-purge",
            object_key="source-purge/shared.txt",
            object_version_id="v1",
            object_url="minio://tenant-purge/source-purge/shared.txt",
            size=1,
            mime="text/plain",
            content_sha256="a" * 64,
            status="parsed",
        )
        asset = DataAsset(
            id="asset-purge-shared-agent",
            tenant_id=self.tenant.id,
            owner_agent_id=other_agent.id,
            key="agent.shared.scenario.asset",
            name="其他 Agent 的附件",
            kind="file",
            usage_plane="invocation_input",
            lifecycle_status="active",
            labels={"catalog_purpose": "validation_asset"},
        )
        version = DataAssetVersion(
            id="version-purge-shared-agent",
            tenant_id=self.tenant.id,
            asset_id=asset.id,
            version_number=1,
            bucket_file_id=bucket_file.id,
            bucket_data_source_id="source-purge",
            provenance_kind="upload",
            status="ready",
            content_sha256=bucket_file.content_sha256,
            byte_size=1,
            source_locator={},
            version_document={},
        )
        db.add_all([other_scenario, other_agent])
        db.flush()
        db.add_all([bucket_file, asset, version])
        db.commit()
        db.close()

        plan = self.client.get(f"/api/scenarios/{self.scenario.id}/purge-plan")
        self.assertEqual(plan.status_code, 200, plan.text)
        self.assertFalse(plan.json()["can_purge"], plan.text)
        self.assertIn("其他场景或平台资源引用", "；".join(plan.json()["blockers"]))

        response = self.client.post(
            f"/api/scenarios/{self.scenario.id}/purge",
            json={
                "expected_name": self.scenario.name,
                "confirmed": True,
                "delete_audit_history": True,
            },
        )
        self.assertEqual(response.status_code, 409, response.text)

        with self.Session() as db:
            self.assertIsNotNone(db.get(BusinessScenario, self.scenario.id))
            self.assertIsNotNone(db.get(DataSource, "source-purge"))
            retained_file = db.get(BucketFile, bucket_file.id)
            self.assertIsNotNone(retained_file)
            retained_version = db.get(DataAssetVersion, version.id)
            self.assertIsNotNone(retained_version)
            assert retained_version is not None
            self.assertEqual(retained_version.bucket_file_id, bucket_file.id)
            self.assertEqual(retained_version.bucket_data_source_id, "source-purge")
            self.assertIsNotNone(db.get(DataAsset, asset.id))

    def test_purge_detaches_redacted_agent_runtime_source_after_agent_cleanup(self) -> None:
        db = self.Session()
        agent = Agent(
            id="agent-purge-runtime",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="待删除 Agent",
        )
        runtime_source = DataSource(
            id="source-purge-runtime",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            resource_scope="agent_runtime",
            owner_agent_id=agent.id,
            name="Agent runtime uploads",
            type="file_bucket",
            config={},
        )
        upload_run = ManagedUploadRun(
            id="upload-purge-runtime",
            tenant_id=self.tenant.id,
            owner_agent_id=agent.id,
            requested_by_user_id=self.user.id,
            data_source_id=runtime_source.id,
            idempotency_key="upload-purge-runtime",
            request_fingerprint="e" * 64,
            purpose="invocation_attachment",
            filename="private.txt",
            declared_byte_size=1,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add_all([agent, runtime_source, upload_run])
        db.commit()
        db.close()

        response = self.client.post(
            f"/api/scenarios/{self.scenario.id}/purge",
            json={
                "expected_name": self.scenario.name,
                "confirmed": True,
                "delete_audit_history": True,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

        with self.Session() as db:
            self.assertIsNone(db.get(BusinessScenario, self.scenario.id))
            self.assertIsNone(db.get(Agent, agent.id))
            retained_source = db.get(DataSource, runtime_source.id)
            self.assertIsNotNone(retained_source)
            assert retained_source is not None
            self.assertIsNone(retained_source.scenario_id)
            self.assertIsNone(retained_source.owner_agent_id)
            self.assertEqual(retained_source.config, {})
            retained_run = db.get(ManagedUploadRun, upload_run.id)
            self.assertIsNotNone(retained_run)
            assert retained_run is not None
            self.assertEqual(retained_run.error_code, "agent_deleted")

    def test_expired_confirmation_does_not_block_retired_scenario_purge(self) -> None:
        db = self.Session()
        invocation = CapabilityInvocation(
            id="invocation-purge-expired-confirmation",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            capability_kind="workflow",
            capability_key="workflow-expired",
            definition_hash="a" * 64,
            deployment_fingerprint="b" * 64,
            data_context_fingerprint="c" * 64,
            correlation_id="correlation-purge-expired",
            principal_type="user",
            principal_id=self.user.id,
            invocation_source="internal",
            request_id="request-purge-expired",
            input_hash="d" * 64,
            status="awaiting_confirmation",
            request_document={},
            result_document={"confirmation": {"expires_at": "2020-01-01T00:00:00Z"}},
        )
        db.add(invocation)
        db.commit()
        db.close()

        plan = self.client.get(f"/api/scenarios/{self.scenario.id}/purge-plan")
        self.assertEqual(plan.status_code, 200, plan.text)
        self.assertTrue(plan.json()["can_purge"], plan.text)
        self.assertNotIn("仍有进行中的能力调用", "；".join(plan.json()["blockers"]))

    def test_owned_template_does_not_block_scenario_source_cleanup(self) -> None:
        db = self.Session()
        bucket_file = BucketFile(
            id="file-purge-owned-template",
            data_source_id="source-purge",
            filename="owned-template.docx",
            stored_path="minio://ontology/owned-template.docx",
            bucket_name="ontology",
            object_key="owned-template.docx",
            object_url="minio://ontology/owned-template.docx",
            size=1,
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            content_sha256="e" * 64,
            status="parsed",
        )
        template = ArtifactTemplate(
            id="template-purge-owned",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            key="owned_template",
            name="场景自有模板",
        )
        version = ArtifactTemplateVersion(
            id="template-version-purge-owned",
            template_id=template.id,
            version=1,
            bucket_file_id=bucket_file.id,
            filename=bucket_file.filename,
            artifact_format="docx",
            mime=bucket_file.mime,
            size=1,
            content_sha256=bucket_file.content_sha256,
        )
        db.add_all([bucket_file, template])
        db.flush()
        db.add(version)
        db.flush()
        template.current_version_id = version.id
        db.commit()
        state = scenario_purge_asset_service.inspect_scenario_sources(
            db,
            self.scenario,
            check_template_refs=True,
        )
        db.close()

        self.assertEqual(state.blockers, ())
        self.assertEqual({source.id for source in state.sources}, {"source-purge"})

    def test_purge_fails_closed_on_cross_tenant_scenario_history(self) -> None:
        """A malformed scoped audit row must never be silently retained/deleted."""

        db = self.Session()
        other_tenant = Tenant(id="tenant-purge-other", name="脏数据租户")
        dirty_trace = LLMInvocationTrace(
            id="trace-purge-cross-tenant",
            tenant_id=other_tenant.id,
            scenario_id=self.scenario.id,
            provider="test",
            model="test",
            correlation_id="cross-tenant",
        )
        db.add(other_tenant)
        db.flush()
        db.add(dirty_trace)
        db.commit()
        db.close()

        plan = self.client.get(f"/api/scenarios/{self.scenario.id}/purge-plan")
        self.assertEqual(plan.status_code, 200, plan.text)
        self.assertFalse(plan.json()["can_purge"], plan.text)
        self.assertIn("租户归属不一致", "；".join(plan.json()["blockers"]))

        response = self.client.post(
            f"/api/scenarios/{self.scenario.id}/purge",
            json={
                "expected_name": self.scenario.name,
                "confirmed": True,
                "delete_audit_history": True,
            },
        )
        self.assertEqual(response.status_code, 409, response.text)

        with self.Session() as db:
            self.assertIsNotNone(db.get(BusinessScenario, self.scenario.id))
            retained = db.get(LLMInvocationTrace, dirty_trace.id)
            self.assertIsNotNone(retained)
            assert retained is not None
            self.assertEqual(retained.tenant_id, other_tenant.id)


if __name__ == "__main__":
    unittest.main()
