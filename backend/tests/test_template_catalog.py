from __future__ import annotations

import copy
from contextlib import nullcontext
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    ArtifactTemplate,
    ArtifactTemplateVersion,
    AuthorizationGrant,
    BucketFile,
    BusinessScenario,
    DataSource,
    OntologyAction,
    OntologyBranch,
    OntologyEntity,
    OntologyRelease,
    OntologySnapshot,
    Tenant,
    User,
)
from app.routers import data_sources, scenarios, templates
from app.schemas import ActionIn
from app.services import (
    datasource_service,
    object_deletion_service,
    object_storage_service,
    permission_service,
    release_service,
    template_artifact_service,
    template_catalog_service,
    workflow_service,
)
from app.services.auth_service import get_current_user, get_tenant_db
from tests.minio_fakes import FakeMinio


MARKDOWN_V1 = b"# {{ report.title }}\n\nCustomer: {{ project.name }}\n"
MARKDOWN_V2 = b"# {{ report.title }}\n\nCustomer: {{ project.name }}\n\nVersion 2\n"
TEST_MINIO_CONFIGURATION = object_storage_service.MinioConfiguration(
    endpoint="minio.example.test",
    access_key="access",
    secret_key="secret",
    bucket_name="ontology",
    prefix="ontology-business",
)
TEST_MINIO_SOURCE_CONFIG = {
    "storage_backend": "minio",
    "bucket_name": "ontology",
    "prefix": "ontology-business",
}


class TemplateCatalogTests(unittest.TestCase):
    @staticmethod
    def _fake_upload_claim(**kwargs):
        return SimpleNamespace(object_key=kwargs["object_key"])

    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.Session = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )
        Base.metadata.create_all(self.engine)
        self.minio = FakeMinio()
        self.configuration_patch = patch.object(
            object_storage_service,
            "configuration",
            return_value=TEST_MINIO_CONFIGURATION,
        )
        self.client_patch = patch.object(
            object_storage_service,
            "get_client",
            return_value=self.minio,
        )
        # Template catalog behavior is covered here; the transactional outbox
        # has dedicated tests. Avoid a second SQLite session committing the
        # shared StaticPool connection used by this HTTP-focused test class.
        self.upload_intent_patch = patch.object(
            object_deletion_service,
            "_prepare_upload_intent",
            side_effect=self._fake_upload_claim,
        )
        self.heartbeat_patch = patch.object(
            object_deletion_service,
            "heartbeat_upload_intent",
            side_effect=lambda _claim: nullcontext(),
        )
        self.begin_upload_patch = patch.object(
            object_deletion_service,
            "begin_upload_put",
        )
        self.assert_upload_patch = patch.object(
            object_deletion_service,
            "assert_upload_active",
        )
        self.retain_upload_patch = patch.object(
            object_deletion_service,
            "retain_bucket_file_upload",
        )
        self.configuration_patch.start()
        self.client_patch.start()
        self.upload_intent_patch.start()
        self.heartbeat_patch.start()
        self.begin_upload_patch.start()
        self.assert_upload_patch.start()
        self.retain_upload_patch.start()
        db = self.Session()
        self.tenant = Tenant(id="tenant-templates", name="模板租户")
        self.user = User(
            id="user-templates",
            tenant_id=self.tenant.id,
            email="templates@example.test",
            password_hash="test-only",
            status="active",
        )
        self.foreign_tenant = Tenant(id="tenant-foreign", name="其他租户")
        self.foreign_user = User(
            id="user-foreign",
            tenant_id=self.foreign_tenant.id,
            email="foreign-templates@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario_a = BusinessScenario(
            id="scenario-template-a", tenant_id=self.tenant.id, name="审计场景 A"
        )
        self.scenario_b = BusinessScenario(
            id="scenario-template-b", tenant_id=self.tenant.id, name="审计场景 B"
        )
        self.entity_a = OntologyEntity(
            id="entity-template-a", scenario_id=self.scenario_a.id, name="审计项目"
        )
        self.entity_b = OntologyEntity(
            id="entity-template-b", scenario_id=self.scenario_b.id, name="审计项目"
        )
        self.global_bucket = DataSource(
            id="bucket-template-global",
            tenant_id=self.tenant.id,
            scenario_id=None,
            name="共享模板库",
            type="file_bucket",
            config=dict(TEST_MINIO_SOURCE_CONFIG),
        )
        self.scenario_bucket = DataSource(
            id="bucket-template-a",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario_a.id,
            name="场景模板库",
            type="file_bucket",
            config=dict(TEST_MINIO_SOURCE_CONFIG),
        )
        self.target_bucket = DataSource(
            id="bucket-template-output",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario_a.id,
            name="场景附件库",
            type="file_bucket",
            config=dict(TEST_MINIO_SOURCE_CONFIG),
        )
        db.add_all([
            self.tenant,
            self.user,
            self.foreign_tenant,
            self.foreign_user,
            self.scenario_a,
            self.scenario_b,
            self.entity_a,
            self.entity_b,
            self.global_bucket,
            self.scenario_bucket,
            self.target_bucket,
        ])
        db.commit()
        self.organization = permission_service.ensure_organization(
            db, self.tenant.id, owner_user_id=self.user.id
        )
        permission_service.ensure_organization(
            db, self.foreign_tenant.id, owner_user_id=self.foreign_user.id
        )
        db.commit()
        db.close()

        self.current_tenant_id = self.tenant.id
        self.current_user_id = self.user.id
        self.app = FastAPI()
        self.app.include_router(templates.router, prefix="/api")
        self.app.include_router(data_sources.router, prefix="/api")
        self.app.include_router(scenarios.router, prefix="/api")

        def override_user():
            return SimpleNamespace(
                id=self.current_user_id, tenant_id=self.current_tenant_id
            )

        def override_db():
            session = self.Session()
            session.info["tenant_id"] = self.current_tenant_id
            session.info["user_id"] = self.current_user_id
            try:
                yield session
            finally:
                session.close()

        self.app.dependency_overrides[get_current_user] = override_user
        self.app.dependency_overrides[get_db] = override_db
        self.app.dependency_overrides[get_tenant_db] = override_db
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()
        self.retain_upload_patch.stop()
        self.assert_upload_patch.stop()
        self.begin_upload_patch.stop()
        self.heartbeat_patch.stop()
        self.upload_intent_patch.stop()
        self.client_patch.stop()
        self.configuration_patch.stop()

    def _db(self) -> Session:
        db = self.Session()
        db.info["tenant_id"] = self.tenant.id
        db.info["user_id"] = self.user.id
        return db

    def _upload(
        self,
        *,
        data_source_id: str | None = None,
        scenario_id: str | None = None,
        key: str = "annual_audit_report",
        filename: str = "audit.markdown",
        content: bytes = MARKDOWN_V1,
    ) -> dict:
        data = {
            "data_source_id": data_source_id or self.global_bucket.id,
            "name": "年度审计报告",
            "purpose": "年度财务报表审计交付",
            "description": "统一审计报告模板",
            "key": key,
            "version_note": "初始版本",
        }
        if scenario_id:
            data["scenario_id"] = scenario_id
        response = self.client.post(
            "/api/templates/upload",
            data=data,
            files={"file": (filename, content, "text/markdown")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _new_action_payload(self, template: dict, *, version: int | None = None) -> ActionIn:
        config = {
            "template_id": template["id"],
            "target_data_source_id": self.target_bucket.id,
            "output_filename": "{{project.name}}审计报告.markdown",
        }
        if version is not None:
            config["template_version"] = version
        return ActionIn(
            entity_id=self.entity_a.id,
            name="生成年度审计报告",
            description="生成审计附件",
            input_schema={},
            executor_type="template",
            executor_config=config,
            requires_confirmation=True,
            idempotency_required=True,
        )

    def test_upload_inspects_markdown_and_lists_global_plus_scenario_templates(self) -> None:
        shared = self._upload()
        self.assertEqual(shared["current_version"]["artifact_format"], "markdown")
        self.assertEqual(
            shared["current_version"]["placeholder_paths"],
            ["project.name", "report.title"],
        )
        self.assertEqual(shared["purpose"], "年度财务报表审计交付")
        self.assertEqual(len(shared["current_version"]["sha256"]), 64)

        scoped = self._upload(
            data_source_id=self.scenario_bucket.id,
            scenario_id=self.scenario_a.id,
            key="scenario_a_notes",
            filename="notes.md",
        )
        listed = self.client.get(
            f"/api/templates?scenario_id={self.scenario_a.id}&status=active&artifact_format=markdown"
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual({item["id"] for item in listed.json()}, {shared["id"], scoped["id"]})

        updated = self.client.put(
            f"/api/templates/{shared['id']}",
            json={"purpose": "集团年度审计", "key": "group_annual_audit"},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["purpose"], "集团年度审计")

        self.current_tenant_id = self.foreign_tenant.id
        self.current_user_id = self.foreign_user.id
        hidden = self.client.get(f"/api/templates/{shared['id']}")
        self.assertEqual(hidden.status_code, 404)

    def test_upload_uses_staged_inspection_without_reading_template_back_from_minio(self) -> None:
        large_markdown = b"# {{ report.title }}\n" + b"x" * (3 * 1024 * 1024)
        with patch.object(
            template_catalog_service,
            "inspect_bucket_file",
            side_effect=AssertionError("uploaded template must not be read back"),
        ):
            uploaded = self._upload(
                key="staged_large_template",
                filename="staged.md",
                content=large_markdown,
            )
        self.assertEqual(uploaded["current_version"]["size"], len(large_markdown))

        with patch.object(
            template_catalog_service,
            "inspect_bucket_file",
            side_effect=AssertionError("uploaded version must not be read back"),
        ):
            response = self.client.post(
                f"/api/templates/{uploaded['id']}/versions/upload",
                data={
                    "data_source_id": self.global_bucket.id,
                    "version_note": "流式修订版",
                    "set_current": "true",
                },
                files={
                    "file": (
                        "staged-v2.md",
                        b"# {{ report.title }}\nsecond version\n",
                        "text/markdown",
                    )
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["version_count"], 2)

    def test_existing_bucket_files_can_be_registered_and_versioned(self) -> None:
        db = self._db()
        first = datasource_service.save_bucket_file(
            self.scenario_bucket, "existing.md", MARKDOWN_V1
        )
        second = datasource_service.save_bucket_file(
            self.scenario_bucket, "existing-v2.md", MARKDOWN_V2
        )
        db.add_all([first, second])
        db.commit()
        first_id = first.id
        second_id = second.id

        registered = self.client.post(
            "/api/templates/register",
            json={
                "file_id": first_id,
                "scenario_id": self.scenario_a.id,
                "name": "既有文件模板",
                "purpose": "验证从文件桶登记",
                "key": "existing_bucket_template",
                "version_note": "登记版本",
            },
        )
        self.assertEqual(registered.status_code, 200, registered.text)
        catalog = registered.json()
        self.assertEqual(catalog["current_version"]["bucket_file_id"], first_id)

        versioned = self.client.post(
            f"/api/templates/{catalog['id']}/versions/register",
            json={
                "file_id": second_id,
                "version_note": "第二版",
                "set_current": True,
            },
        )
        self.assertEqual(versioned.status_code, 200, versioned.text)
        self.assertEqual(versioned.json()["current_version"]["version"], 2)
        self.assertEqual(len(versioned.json()["versions"]), 2)
        deleted_scenario = self.client.delete(
            f"/api/scenarios/{self.scenario_a.id}"
        )
        self.assertEqual(deleted_scenario.status_code, 200, deleted_scenario.text)
        db.expire_all()
        self.assertIsNone(db.get(ArtifactTemplate, catalog["id"]))
        # Scenario deletion detaches the source (its FK is SET NULL) and
        # removes bytes through the durable-object deletion outbox. Metadata
        # remains for audit/recovery instead of treating a MinIO URI as a
        # cascade-owned local file.
        remaining_file = db.get(BucketFile, first_id)
        self.assertIsNotNone(remaining_file)
        with self.assertRaises(FileNotFoundError):
            datasource_service.read_bucket_file(remaining_file, self.scenario_bucket)
        db.close()

    def test_delete_file_bucket_cascades_legacy_files_and_minio_objects(self) -> None:
        db = self._db()
        current = datasource_service.save_bucket_file(
            self.scenario_bucket,
            "current.md",
            b"current file",
        )
        legacy_key = "ontology-business/migrations/file-buckets/legacy.md"
        legacy_body = b"legacy file"
        legacy_upload = object_storage_service.put_object(
            "ontology",
            legacy_key,
            legacy_body,
            content_type="text/markdown",
        )
        legacy_url = object_storage_service.stable_object_url("ontology", legacy_key)
        legacy = BucketFile(
            id="d" * 32,
            data_source_id=self.scenario_bucket.id,
            filename="legacy.md",
            stored_path=legacy_url,
            storage_provider="minio",
            bucket_name="ontology",
            object_key=legacy_key,
            object_version_id=legacy_upload.version_id,
            etag=legacy_upload.etag,
            object_url=legacy_url,
            size=len(legacy_body),
            mime="text/markdown",
        )
        db.add_all([current, legacy])
        db.commit()
        source_id = self.scenario_bucket.id
        current_id = current.id
        legacy_id = legacy.id
        current_key = current.object_key
        # Simulate the deployment and the data-source policy moving to a new
        # bucket/prefix after these records were persisted.  Deletion must use
        # each BucketFile's controlled historical identity, not today's config.
        rotated_configuration = object_storage_service.MinioConfiguration(
            endpoint="minio.example.test",
            access_key="access",
            secret_key="secret",
            bucket_name="ontology-current",
            prefix="ontology-current-files",
        )
        source = db.get(DataSource, source_id)
        self.assertIsNotNone(source)
        source.config = {
            "storage_backend": "minio",
            "bucket_name": rotated_configuration.bucket_name,
            "prefix": rotated_configuration.prefix,
        }
        db.commit()
        db.close()

        with patch.object(
            object_storage_service,
            "configuration",
            return_value=rotated_configuration,
        ):
            deleted = self.client.delete(f"/api/data-sources/{source_id}")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["data"], {
            "data_source_id": source_id,
            "files_deleted": 2,
            "cleanup_jobs": 2,
        })

        verify = self._db()
        self.assertIsNone(verify.get(DataSource, source_id))
        self.assertIsNone(verify.get(BucketFile, current_id))
        self.assertIsNone(verify.get(BucketFile, legacy_id))
        verify.close()
        self.assertNotIn(("ontology", current_key), self.minio.objects)
        self.assertNotIn(("ontology", legacy_key), self.minio.objects)

    def test_action_pins_version_deprecation_blocks_rebinding_but_not_existing_run(self) -> None:
        catalog = self._upload(filename="audit.md")
        db = self._db()
        payload = self._new_action_payload(catalog)
        scenarios._validate_action_executor(db, self.scenario_a.id, payload)
        self.assertEqual(payload.executor_config["template_version"], 1)
        self.assertNotIn("template_file_id", payload.executor_config)
        pinned_hash = payload.executor_config["template_sha256"]
        action = OntologyAction(
            id="action-pinned-template",
            scenario_id=self.scenario_a.id,
            **payload.model_dump(),
        )
        db.add(action)
        db.commit()
        target_delete = self.client.delete(
            f"/api/data-sources/{self.target_bucket.id}"
        )
        self.assertEqual(target_delete.status_code, 409, target_delete.text)
        self.assertIn("附件目标", target_delete.json()["detail"])

        version_response = self.client.post(
            f"/api/templates/{catalog['id']}/versions/upload",
            data={
                "data_source_id": self.global_bucket.id,
                "version_note": "修订版",
                "set_current": "true",
            },
            files={"file": ("audit-v2.md", MARKDOWN_V2, "text/markdown")},
        )
        self.assertEqual(version_response.status_code, 200, version_response.text)
        self.assertEqual(version_response.json()["current_version"]["version"], 2)
        db.expire_all()
        self.assertEqual(action.executor_config["template_version"], 1)
        self.assertEqual(action.executor_config["template_sha256"], pinned_hash)

        deprecated = self.client.post(f"/api/templates/{catalog['id']}/deprecate")
        self.assertEqual(deprecated.status_code, 200, deprecated.text)
        blocked_new = self._new_action_payload(catalog, version=2)
        with self.assertRaises(Exception) as blocked:
            scenarios._validate_action_executor(db, self.scenario_a.id, blocked_new)
        self.assertIn("已停用", str(blocked.exception))

        unchanged = ActionIn(**{
            **payload.model_dump(),
            "description": "仅修改操作说明",
            "executor_config": dict(action.executor_config),
        })
        scenarios._validate_action_executor(
            db,
            self.scenario_a.id,
            unchanged,
            existing_action=action,
        )
        changed_output = ActionIn(**{
            **payload.model_dump(),
            "executor_config": {
                **dict(action.executor_config),
                "output_filename": "改绑后的审计报告.md",
            },
        })
        with self.assertRaises(Exception) as changed_blocked:
            scenarios._validate_action_executor(
                db,
                self.scenario_a.id,
                changed_output,
                existing_action=action,
            )
        self.assertIn("已停用", str(changed_blocked.exception))
        preview = workflow_service.execute_action(
            db,
            action,
            {"report": {"title": "审计报告"}, "project": {"name": "华信"}},
            dry_run=True,
        )
        self.assertEqual(preview["status"], "dry_run")
        self.assertEqual(preview["result"]["plan"]["artifact"]["template_version"], 1)
        db.info["action_lineage_context"] = {
            "parent_action_log_id": preview["log_id"],
            "correlation_id": preview["correlation_id"],
        }
        executed = workflow_service.execute_action(
            db,
            action,
            {"report": {"title": "审计报告"}, "project": {"name": "华信"}},
            confirm=True,
            idempotency_key="catalog-template-generation",
        )
        self.assertEqual(executed["status"], "success")
        artifact = executed["result"]["artifact"]
        self.assertEqual(artifact["template_id"], catalog["id"])
        self.assertEqual(artifact["template_version"], 1)
        generated = db.get(BucketFile, artifact["id"])
        self.assertEqual(generated.origin_template_id, catalog["id"])
        self.assertEqual(
            generated.origin_template_version_id,
            artifact["template_version_id"],
        )
        rendered_text = datasource_service.read_bucket_file(
            generated,
            self.target_bucket,
        )[0].decode("utf-8")
        self.assertIn("# 审计报告", rendered_text)
        self.assertIn("Customer: 华信", rendered_text)
        db.close()

    def test_delete_and_scope_changes_are_blocked_by_live_release_and_governance_references(self) -> None:
        catalog = self._upload()
        db = self._db()
        payload = self._new_action_payload(catalog)
        scenarios._validate_action_executor(db, self.scenario_a.id, payload)
        action = OntologyAction(
            id="action-template-reference",
            scenario_id=self.scenario_a.id,
            **payload.model_dump(),
        )
        db.add(action)
        db.commit()

        blocked_delete = self.client.delete(f"/api/templates/{catalog['id']}")
        self.assertEqual(blocked_delete.status_code, 409, blocked_delete.text)
        self.assertIn("Action", blocked_delete.json()["detail"])

        bucket_file_id = catalog["current_version"]["bucket_file_id"]
        bucket_file = db.get(BucketFile, bucket_file_id)
        blocked_file = self.client.delete(f"/api/data-sources/files/{bucket_file_id}")
        self.assertEqual(blocked_file.status_code, 409, blocked_file.text)
        self.assertTrue(
            datasource_service.read_bucket_file(bucket_file, self.global_bucket)[0]
        )
        blocked_bucket = self.client.delete(f"/api/data-sources/{self.global_bucket.id}")
        self.assertEqual(blocked_bucket.status_code, 409, blocked_bucket.text)
        self.assertTrue(
            datasource_service.read_bucket_file(bucket_file, self.global_bucket)[0]
        )

        branch = OntologyBranch(
            id="branch-template-release",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario_a.id,
            name="main",
        )
        snapshot = OntologySnapshot(
            id="snapshot-template-release",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario_a.id,
            branch_id=branch.id,
            kind="merge",
            content={
                "actions": [{
                    "id": action.id,
                    "name": action.name,
                    "executor_config": dict(action.executor_config),
                }]
            },
        )
        release = OntologyRelease(
            id="release-template",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario_a.id,
            branch_id=branch.id,
            snapshot_id=snapshot.id,
            environment="dev",
            status="released",
        )
        db.add_all([branch, snapshot, release])
        db.delete(action)
        db.commit()
        release_block = self.client.delete(f"/api/templates/{catalog['id']}")
        self.assertEqual(release_block.status_code, 409, release_block.text)
        self.assertIn("发布快照", release_block.json()["detail"])

        release.status = "superseded"
        db.commit()
        governance_block = self.client.delete(f"/api/templates/{catalog['id']}")
        self.assertEqual(governance_block.status_code, 409, governance_block.text)
        self.assertIn("回滚", governance_block.json()["detail"])

        # A superseded release can still be restored from its merge snapshot.
        # Only after that governance source no longer references the template is
        # deleting the catalog metadata safe.
        snapshot.content = {"actions": []}
        db.commit()
        deleted = self.client.delete(f"/api/templates/{catalog['id']}")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertTrue(
            datasource_service.read_bucket_file(bucket_file, self.global_bucket)[0],
            "catalog deletion must not delete bucket bytes",
        )
        db.close()

    def test_hidden_scenario_references_do_not_leak_counts_but_still_protect_template(self) -> None:
        catalog = self._upload()
        db = self._db()
        config = {
            "template_id": catalog["id"],
            "template_version": 1,
            "template_sha256": catalog["current_version"]["sha256"],
            "target_data_source_id": self.target_bucket.id,
        }
        hidden_action = OntologyAction(
            id="action-hidden-template",
            scenario_id=self.scenario_b.id,
            entity_id=self.entity_b.id,
            name="隐藏场景动作",
            executor_type="template",
            executor_config=config,
        )
        deny = AuthorizationGrant(
            organization_id=self.organization.id,
            user_id=self.user.id,
            resource_type="scenario",
            resource_id=self.scenario_b.id,
            verb="read",
            effect="deny",
        )
        db.add_all([hidden_action, deny])
        db.commit()

        detail = self.client.get(f"/api/templates/{catalog['id']}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["reference_count"], 0)
        self.assertEqual(detail.json()["references"], [])
        self.assertFalse(detail.json()["deletable"])
        blocked = self.client.delete(f"/api/templates/{catalog['id']}")
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertNotIn("1", blocked.json()["detail"])

        narrowed = self.client.put(
            f"/api/templates/{catalog['id']}",
            json={"scenario_id": self.scenario_a.id},
        )
        self.assertEqual(narrowed.status_code, 409, narrowed.text)
        db.close()

    def test_legacy_actions_are_cataloged_once_and_complete_pins_skip_startup_io(self) -> None:
        db = self._db()
        file = datasource_service.save_bucket_file(
            self.scenario_bucket, "legacy.md", MARKDOWN_V1
        )
        db.add(file)
        db.flush()
        action = OntologyAction(
            id="action-legacy-template",
            scenario_id=self.scenario_a.id,
            entity_id=self.entity_a.id,
            name="旧模板动作",
            executor_type="template",
            executor_config={
                "template_file_id": file.id,
                "template_data_source_id": self.scenario_bucket.id,
                "target_data_source_id": self.target_bucket.id,
                "output_filename": "{{project.name}}报告.md",
            },
        )
        db.add(action)
        db.commit()

        startup_order: list[str] = []
        original_s_lock = template_catalog_service.lock_scenarios_for_template_write
        original_create = template_catalog_service.create_from_bucket_file

        def record_startup_s(*args, **kwargs):
            startup_order.append("S")
            return original_s_lock(*args, **kwargs)

        def record_startup_catalog(*args, **kwargs):
            startup_order.append("catalog")
            return original_create(*args, **kwargs)

        with (
            patch.object(
                template_catalog_service,
                "lock_scenarios_for_template_write",
                side_effect=record_startup_s,
            ),
            patch.object(
                template_catalog_service,
                "create_from_bucket_file",
                side_effect=record_startup_catalog,
            ),
        ):
            self.assertEqual(template_catalog_service.migrate_legacy_template_actions(db), 1)
        self.assertLess(
            startup_order.index("S"), startup_order.index("catalog"), startup_order
        )
        db.commit()
        self.assertIn("template_id", action.executor_config)
        self.assertNotIn("template_file_id", action.executor_config)
        self.assertEqual(action.executor_config["template_version"], 1)
        self.assertEqual(db.query(ArtifactTemplate).count(), 1)
        self.assertEqual(db.query(ArtifactTemplateVersion).count(), 1)

        with patch.object(
            template_catalog_service,
            "resolve_version",
            side_effect=AssertionError("complete pins must not reopen files at startup"),
        ):
            self.assertEqual(template_catalog_service.migrate_legacy_template_actions(db), 0)
        self.assertEqual(db.query(ArtifactTemplate).count(), 1)
        db.close()

    def test_startup_migration_refreshes_action_after_s_lock_before_writing(self) -> None:
        db = self._db()
        legacy_file = datasource_service.save_bucket_file(
            self.scenario_bucket, "startup-stale.md", MARKDOWN_V1
        )
        db.add(legacy_file)
        db.flush()
        action = OntologyAction(
            id="action-startup-stale",
            scenario_id=self.scenario_a.id,
            entity_id=self.entity_a.id,
            name="启动并发模板动作",
            executor_type="template",
            executor_config={
                "template_file_id": legacy_file.id,
                "target_data_source_id": self.target_bucket.id,
            },
        )
        db.add(action)
        db.commit()
        original_s_lock = template_catalog_service.lock_scenarios_for_template_write
        fresh_config = {
            "template_id": "online-fresh-template",
            "template_version": 7,
            "template_sha256": "a" * 64,
            "target_data_source_id": self.target_bucket.id,
        }

        def concurrent_online_save(*args, **kwargs):
            locked = original_s_lock(*args, **kwargs)
            action.executor_config = dict(fresh_config)
            db.flush()
            return locked

        with patch.object(
            template_catalog_service,
            "lock_scenarios_for_template_write",
            side_effect=concurrent_online_save,
        ):
            self.assertEqual(
                template_catalog_service.migrate_legacy_template_actions(db), 0
            )
        self.assertEqual(action.executor_config, fresh_config)
        self.assertEqual(db.query(ArtifactTemplate).count(), 0)
        db.rollback()
        db.close()

    def test_new_template_scope_acl_is_checked_before_s_lock(self) -> None:
        catalog = self._upload(key="acl_before_lock")
        db = self._db()
        existing = datasource_service.save_bucket_file(
            self.global_bucket, "acl-register.md", MARKDOWN_V1
        )
        db.add(existing)
        db.commit()
        denied = HTTPException(403, "没有目标场景权限")
        with (
            patch.object(templates, "_scenario_access", side_effect=denied),  # noqa: SLF001
            patch.object(
                template_catalog_service,
                "lock_scenarios_for_template_write",
                side_effect=AssertionError("ACL must run before S lock"),
            ),
        ):
            register = self.client.post(
                "/api/templates/register",
                json={
                    "file_id": existing.id,
                    "scenario_id": self.scenario_b.id,
                    "name": "无权限登记",
                },
            )
            upload = self.client.post(
                "/api/templates/upload",
                data={
                    "data_source_id": self.global_bucket.id,
                    "scenario_id": self.scenario_b.id,
                    "name": "无权限上传",
                },
                files={"file": ("denied.md", MARKDOWN_V1, "text/markdown")},
            )
            update = self.client.put(
                f"/api/templates/{catalog['id']}",
                json={"scenario_id": self.scenario_b.id},
            )
        self.assertEqual(register.status_code, 403, register.text)
        self.assertEqual(upload.status_code, 403, upload.text)
        self.assertEqual(update.status_code, 403, update.text)
        db.close()

    def test_snapshot_legacy_ids_are_tenant_filtered_before_resource_locks(self) -> None:
        db = self._db()
        foreign_source = DataSource(
            id="bucket-foreign-template-source",
            tenant_id=self.foreign_tenant.id,
            scenario_id=None,
            name="其他租户模板桶",
            type="file_bucket",
            config=dict(TEST_MINIO_SOURCE_CONFIG),
        )
        db.add(foreign_source)
        db.flush()
        foreign_file = datasource_service.save_bucket_file(
            foreign_source, "foreign.md", MARKDOWN_V1
        )
        db.add(foreign_file)
        db.commit()

        def legacy_action(file_id: str) -> dict:
            return {
                "id": "snapshot-legacy-foreign",
                "name": "跨租户旧模板",
                "executor_type": "template",
                "requires_confirmation": True,
                "idempotency_required": True,
                "input_schema": {},
                "executor_config": {
                    "template_file_id": file_id,
                    "template_data_source_id": foreign_source.id,
                    "target_data_source_id": foreign_source.id,
                },
            }

        errors: list[str] = []
        original_scalars = db.scalars

        def no_resource_lock(*args, **kwargs):
            raise AssertionError("foreign legacy IDs must fail before D/F lock queries")

        with patch.object(db, "scalars", side_effect=no_resource_lock):
            for file_id in (foreign_file.id, "missing-legacy-file"):
                with self.assertRaises(
                    template_catalog_service.TemplateCatalogError
                ) as raised:
                    template_catalog_service.validate_snapshot_template_actions(
                        db,
                        tenant_id=self.tenant.id,
                        scenario_id=self.scenario_a.id,
                        actions=[legacy_action(file_id)],
                    )
                errors.append(str(raised.exception))
        self.assertEqual(errors[0], errors[1])
        self.assertIn("不存在或不在当前租户", errors[0])
        self.assertTrue(callable(original_scalars))
        db.close()

    def test_snapshot_catalog_and_bucket_scope_are_filtered_before_locks(self) -> None:
        db = self._db()
        scenario_b_bucket = DataSource(
            id="bucket-template-b-private",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario_b.id,
            name="场景 B 私有模板桶",
            type="file_bucket",
            config=dict(TEST_MINIO_SOURCE_CONFIG),
        )
        db.add(scenario_b_bucket)
        db.flush()
        scenario_b_file = datasource_service.save_bucket_file(
            scenario_b_bucket, "scenario-b.md", MARKDOWN_V1
        )
        db.add(scenario_b_file)
        db.flush()
        scenario_b_template = template_catalog_service.create_from_bucket_file(
            db,
            tenant_id=self.tenant.id,
            template_file=scenario_b_file,
            template_source=scenario_b_bucket,
            scenario_id=self.scenario_b.id,
            name="场景 B 私有模板",
            key="scenario_b_private_template",
        )
        db.commit()
        scenario_b_version = db.get(
            ArtifactTemplateVersion, scenario_b_template.current_version_id
        )

        def catalog_action(template_id: str) -> dict:
            paths = list(scenario_b_version.placeholder_paths or [])
            return {
                "id": "snapshot-catalog-scope",
                "name": "越场景目录模板",
                "executor_type": "template",
                "requires_confirmation": True,
                "idempotency_required": True,
                "input_schema": template_artifact_service.merge_template_input_schema(
                    {}, paths
                ),
                "executor_config": {
                    "template_id": template_id,
                    "template_version": scenario_b_version.version,
                    "template_sha256": scenario_b_version.content_sha256,
                    "target_data_source_id": self.target_bucket.id,
                    "template_variable_paths": paths,
                },
            }

        catalog_errors: list[str] = []
        for template_id in (scenario_b_template.id, "missing-catalog-template"):
            with self.assertRaises(
                template_catalog_service.TemplateCatalogError
            ) as raised:
                template_catalog_service.validate_snapshot_template_actions(
                    db,
                    tenant_id=self.tenant.id,
                    scenario_id=self.scenario_a.id,
                    actions=[catalog_action(template_id)],
                )
            catalog_errors.append(str(raised.exception))
        self.assertEqual(catalog_errors[0], catalog_errors[1])

        local_legacy_file = datasource_service.save_bucket_file(
            self.scenario_bucket, "local-legacy.md", MARKDOWN_V1
        )
        db.add(local_legacy_file)
        db.commit()

        def legacy_with_target(target_id: str) -> dict:
            return {
                "id": "snapshot-target-scope",
                "name": "越场景目标桶",
                "executor_type": "template",
                "requires_confirmation": True,
                "idempotency_required": True,
                "input_schema": {},
                "executor_config": {
                    "template_file_id": local_legacy_file.id,
                    "template_data_source_id": self.scenario_bucket.id,
                    "target_data_source_id": target_id,
                },
            }

        target_errors: list[str] = []
        for target_id in (scenario_b_bucket.id, "missing-target-bucket"):
            with self.assertRaises(
                template_catalog_service.TemplateCatalogError
            ) as raised:
                template_catalog_service.validate_snapshot_template_actions(
                    db,
                    tenant_id=self.tenant.id,
                    scenario_id=self.scenario_a.id,
                    actions=[legacy_with_target(target_id)],
                )
            target_errors.append(str(raised.exception))
        self.assertEqual(target_errors[0], target_errors[1])
        db.close()

    def test_legacy_governance_snapshot_blocks_source_file_and_bucket_deletion(self) -> None:
        db = self._db()
        legacy_file = datasource_service.save_bucket_file(
            self.scenario_bucket, "rollback-legacy.md", MARKDOWN_V1
        )
        db.add(legacy_file)
        branch = OntologyBranch(
            id="branch-legacy-source-guard",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario_a.id,
            name="legacy-source-guard",
        )
        snapshot = OntologySnapshot(
            id="snapshot-legacy-source-guard",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario_a.id,
            branch_id=branch.id,
            kind="merge",
            content={
                "actions": [
                    {
                        "id": "historical-legacy-action",
                        "executor_type": "template",
                        "executor_config": {
                            "template_file_id": legacy_file.id,
                            "template_data_source_id": self.scenario_bucket.id,
                            "target_data_source_id": self.target_bucket.id,
                        },
                    }
                ]
            },
        )
        db.add_all([branch, snapshot])
        db.commit()

        blocked_file = self.client.delete(
            f"/api/data-sources/files/{legacy_file.id}"
        )
        self.assertEqual(blocked_file.status_code, 409, blocked_file.text)
        self.assertIn("旧式模板", blocked_file.json()["detail"])
        blocked_bucket = self.client.delete(
            f"/api/data-sources/{self.scenario_bucket.id}"
        )
        self.assertEqual(blocked_bucket.status_code, 409, blocked_bucket.text)
        self.assertIn("模板来源", blocked_bucket.json()["detail"])
        self.assertIsNotNone(db.get(BucketFile, legacy_file.id))
        db.close()

    def test_unsafe_placeholder_and_unauthorized_shared_target_are_rejected(self) -> None:
        unsafe = self.client.post(
            "/api/templates/upload",
            data={
                "data_source_id": self.global_bucket.id,
                "name": "危险模板",
                "key": "unsafe_placeholder",
            },
            files={
                "file": (
                    "unsafe.md",
                    b"{{ __proto__.polluted }}",
                    "text/markdown",
                )
            },
        )
        self.assertEqual(unsafe.status_code, 400, unsafe.text)
        self.assertIn("不安全", unsafe.json()["detail"])

        catalog = self._upload(key="shared_target_acl", filename="shared.md")
        payload = ActionIn(
            entity_id=self.entity_a.id,
            name="写入共享附件桶",
            input_schema={},
            executor_type="template",
            executor_config={
                "template_id": catalog["id"],
                "target_data_source_id": self.global_bucket.id,
            },
            requires_confirmation=True,
            idempotency_required=True,
        )
        db = self._db()
        with patch.object(
            permission_service,
            "require_tenant_permission",
            side_effect=HTTPException(403, "没有组织管理权限"),
        ):
            with self.assertRaises(HTTPException) as denied:
                scenarios._validate_action_executor(db, self.scenario_a.id, payload)
        self.assertEqual(denied.exception.status_code, 403)

        scenarios._validate_action_executor(db, self.scenario_a.id, payload)
        existing = OntologyAction(
            id="action-shared-target-preauthorized",
            scenario_id=self.scenario_a.id,
            **payload.model_dump(),
        )
        db.add(existing)
        db.commit()
        changed = ActionIn(**{
            **payload.model_dump(),
            "executor_config": {
                **existing.executor_config,
                "output_filename": "changed-output.md",
            },
        })
        with patch.object(
            permission_service,
            "require_tenant_permission",
            side_effect=HTTPException(403, "没有组织管理权限"),
        ):
            with self.assertRaises(HTTPException):
                scenarios._validate_action_executor(
                    db,
                    self.scenario_a.id,
                    changed,
                    existing_action=existing,
                )
        unchanged = ActionIn(**payload.model_dump())
        with patch.object(
            permission_service,
            "require_tenant_permission",
            side_effect=AssertionError("unchanged governed target must not re-authorize"),
        ):
            scenarios._validate_action_executor(
                db,
                self.scenario_a.id,
                unchanged,
                existing_action=existing,
            )
        db.close()

    def test_response_failure_after_commit_never_deletes_registered_template_bytes(self) -> None:
        with patch.object(templates, "_out", side_effect=RuntimeError("response failed")):
            with self.assertRaisesRegex(RuntimeError, "response failed"):
                self.client.post(
                    "/api/templates/upload",
                    data={
                        "data_source_id": self.global_bucket.id,
                        "name": "提交后响应失败模板",
                        "key": "committed_response_failure",
                    },
                    files={"file": ("committed.md", MARKDOWN_V1, "text/markdown")},
                )
        db = self._db()
        template = db.scalar(select(ArtifactTemplate).where(
            ArtifactTemplate.key == "committed_response_failure"
        ))
        self.assertIsNotNone(template)
        version = db.get(ArtifactTemplateVersion, template.current_version_id)
        bucket_file = db.get(BucketFile, version.bucket_file_id)
        self.assertTrue(
            datasource_service.read_bucket_file(bucket_file, self.global_bucket)[0]
        )
        db.close()

    def test_same_hash_version_dedup_fails_closed_when_existing_bytes_are_corrupt(self) -> None:
        catalog = self._upload(filename="dedup.md", key="dedup_integrity")
        db = self._db()
        template = db.get(ArtifactTemplate, catalog["id"])
        version = db.get(ArtifactTemplateVersion, template.current_version_id)
        bucket_file = db.get(BucketFile, version.bucket_file_id)
        self.minio.overwrite_object(
            bucket_file.bucket_name,
            bucket_file.object_key,
            b"corrupted-template-bytes",
            version_id=bucket_file.object_version_id,
        )
        db.close()

        response = self.client.post(
            f"/api/templates/{catalog['id']}/versions/upload",
            data={
                "data_source_id": self.global_bucket.id,
                "version_note": "重复内容",
                "set_current": "true",
            },
            files={"file": ("dedup-copy.md", MARKDOWN_V1, "text/markdown")},
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("完整性", response.json()["detail"])
        db = self._db()
        self.assertEqual(
            db.scalar(select(func.count()).select_from(ArtifactTemplateVersion)),
            1,
        )
        db.close()

    def test_governance_rejects_dangling_or_forged_template_pins_before_state_change(self) -> None:
        catalog = self._upload(filename="governed.md", key="governed_template")
        db = self._db()
        payload = self._new_action_payload(catalog, version=1)
        scenarios._validate_action_executor(db, self.scenario_a.id, payload)
        action = OntologyAction(
            id="action-governed-template",
            scenario_id=self.scenario_a.id,
            **payload.model_dump(),
        )
        db.get(OntologyEntity, self.entity_a.id).is_abstract = True
        db.add(action)
        db.commit()
        content = release_service.capture_snapshot_content(db, self.scenario_a)
        branch = OntologyBranch(
            id="branch-governed-template",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario_a.id,
            name="governed-template-main",
        )
        db.add(branch)
        db.flush()
        baseline = OntologySnapshot(
            id="snapshot-governed-template",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario_a.id,
            branch_id=branch.id,
            kind="baseline",
            content=content,
            content_hash=release_service.snapshot_hash(content),
        )
        db.add(baseline)
        db.flush()
        branch.base_snapshot_id = baseline.id
        branch.head_snapshot_id = baseline.id
        db.commit()

        forged = copy.deepcopy(content)
        forged["scenario"]["name"] = "不得落地的场景名称"
        forged["actions"][0]["executor_config"]["template_sha256"] = "0" * 64
        before_snapshot_count = db.scalar(
            select(func.count()).select_from(OntologySnapshot)
        )
        with self.assertRaises(release_service.ReleaseValidationError):
            release_service.create_proposal(
                db,
                branch.id,
                title="伪造模板固定哈希",
                description="必须在快照持久化前拒绝",
                content=forged,
            )
        db.rollback()
        self.assertEqual(
            db.scalar(select(func.count()).select_from(OntologySnapshot)),
            before_snapshot_count,
        )

        incomplete_contract = copy.deepcopy(content)
        incomplete_contract["actions"][0]["input_schema"] = {}
        with self.assertRaises(release_service.ReleaseValidationError) as contract_error:
            release_service.create_proposal(
                db,
                branch.id,
                title="缺少模板输入契约",
                description="必须在快照持久化前拒绝",
                content=incomplete_contract,
            )
        db.rollback()
        self.assertIn("输入参数", str(contract_error.exception))
        self.assertEqual(
            db.scalar(select(func.count()).select_from(OntologySnapshot)),
            before_snapshot_count,
        )

        with self.assertRaises(release_service.ReleaseValidationError):
            release_service._apply_snapshot_content(db, self.scenario_a, forged)  # noqa: SLF001
        db.rollback()
        persisted_scenario = db.get(BusinessScenario, self.scenario_a.id)
        self.assertNotEqual(persisted_scenario.name, "不得落地的场景名称")

        baseline.kind = "merge"
        baseline.content = forged
        baseline.content_hash = release_service.snapshot_hash(forged)
        db.commit()
        with self.assertRaises(release_service.ReleaseValidationError):
            release_service.publish_snapshot(
                db,
                self.scenario_a.id,
                environment="dev",
                confirmed=True,
                branch_id=branch.id,
            )
        db.rollback()
        self.assertEqual(
            db.scalar(select(func.count()).select_from(OntologyRelease)),
            0,
        )
        db.close()

    def test_template_writes_observe_scenario_before_catalog_and_bucket_locks(self) -> None:
        catalog = self._upload(
            data_source_id=self.scenario_bucket.id,
            scenario_id=self.scenario_a.id,
            filename="lock-order.md",
            key="lock_order_template",
        )
        db = self._db()
        next_file = datasource_service.save_bucket_file(
            self.scenario_bucket, "lock-order-v2.md", MARKDOWN_V2
        )
        db.add(next_file)
        db.commit()

        order: list[str] = []
        original_s_lock = template_catalog_service.lock_scenarios_for_template_write
        original_get_owned = template_catalog_service.get_owned
        original_file = templates._file  # noqa: SLF001

        def record_s(*args, **kwargs):
            order.append("S")
            return original_s_lock(*args, **kwargs)

        def record_template(*args, **kwargs):
            if kwargs.get("for_update"):
                order.append("T")
            return original_get_owned(*args, **kwargs)

        def record_file(*args, **kwargs):
            order.append("D/F")
            return original_file(*args, **kwargs)

        with (
            patch.object(
                template_catalog_service,
                "lock_scenarios_for_template_write",
                side_effect=record_s,
            ),
            patch.object(
                template_catalog_service,
                "get_owned",
                side_effect=record_template,
            ),
            patch.object(templates, "_file", side_effect=record_file),  # noqa: SLF001
        ):
            response = self.client.post(
                f"/api/templates/{catalog['id']}/versions/register",
                json={"file_id": next_file.id, "set_current": True},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertLess(order.index("S"), order.index("T"), order)
        self.assertLess(order.index("T"), order.index("D/F"), order)

        action_order: list[str] = []
        payload = self._new_action_payload(catalog, version=1)
        original_resolve = template_catalog_service.resolve_version
        original_lock_sources = scenarios._lock_template_data_sources  # noqa: SLF001

        def record_action_s(*args, **kwargs):
            action_order.append("S")
            return original_s_lock(*args, **kwargs)

        def record_action_t(*args, **kwargs):
            action_order.append("T")
            return original_resolve(*args, **kwargs)

        def record_action_d(*args, **kwargs):
            action_order.append("D")
            return original_lock_sources(*args, **kwargs)

        with (
            patch.object(
                template_catalog_service,
                "lock_scenarios_for_template_write",
                side_effect=record_action_s,
            ),
            patch.object(
                template_catalog_service,
                "resolve_version",
                side_effect=record_action_t,
            ),
            patch.object(
                scenarios,
                "_lock_template_data_sources",
                side_effect=record_action_d,
            ),
        ):
            scenarios._validate_action_executor(db, self.scenario_a.id, payload)
        self.assertLess(action_order.index("S"), action_order.index("T"), action_order)
        self.assertLess(action_order.index("T"), action_order.index("D"), action_order)

        governance_order: list[str] = []
        with (
            patch.object(
                template_catalog_service,
                "lock_scenarios_for_template_write",
                side_effect=lambda *args, **kwargs: (
                    governance_order.append("S")
                    or original_s_lock(*args, **kwargs)
                ),
            ),
            patch.object(
                template_catalog_service,
                "validate_snapshot_template_actions",
                side_effect=lambda *args, **kwargs: governance_order.append("T/D/F"),
            ),
        ):
            release_service._validate_snapshot_template_actions(  # noqa: SLF001
                db,
                self.scenario_a,
                {"actions": []},
            )
        self.assertEqual(governance_order, ["S", "T/D/F"])
        db.close()

    def test_action_update_refreshes_a_after_s_before_unchanged_binding_check(self) -> None:
        catalog = self._upload(
            data_source_id=self.scenario_bucket.id,
            scenario_id=self.scenario_a.id,
            filename="stale-action.md",
            key="stale_action_binding",
        )
        db = self._db()
        payload = self._new_action_payload(catalog, version=1)
        scenarios._validate_action_executor(db, self.scenario_a.id, payload)
        original_config = dict(payload.executor_config)
        action = OntologyAction(
            id="action-stale-binding",
            scenario_id=self.scenario_a.id,
            **payload.model_dump(),
        )
        db.add(action)
        db.commit()
        deprecated = self.client.post(f"/api/templates/{catalog['id']}/deprecate")
        self.assertEqual(deprecated.status_code, 200, deprecated.text)
        db.expire_all()
        incoming = ActionIn(**{
            **payload.model_dump(),
            "description": "等待 S 前观察到的旧请求",
            "executor_config": original_config,
        })
        original_s_lock = template_catalog_service.lock_scenarios_for_template_write
        injected = False

        def concurrent_rebind(*args, **kwargs):
            nonlocal injected
            locked = original_s_lock(*args, **kwargs)
            if not injected:
                injected = True
                live = db.get(OntologyAction, action.id)
                live.executor_config = {
                    **original_config,
                    "output_filename": "并发改绑后的报告.md",
                }
                db.flush()
            return locked

        with patch.object(
            template_catalog_service,
            "lock_scenarios_for_template_write",
            side_effect=concurrent_rebind,
        ):
            with self.assertRaises(HTTPException) as blocked:
                scenarios.update_action(action.id, incoming, db)
        self.assertIn("已停用", str(blocked.exception.detail))
        db.rollback()
        db.close()


if __name__ == "__main__":
    unittest.main()
