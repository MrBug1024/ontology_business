"""P2 resource package HTTP boundary tests.

The service layer is deliberately pure; these tests make sure the router does not
accidentally turn a package preview into an unprotected or mutating import path.
"""
from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import (
    BusinessScenario,
    DataMapping,
    DataSource,
    OntologyAction,
    OntologyEntity,
    OntologyProposal,
    OntologySnapshot,
    Tenant,
    User,
)
from app.routers import packages, starter_kits
from app.services import package_service, permission_service, release_service
from app.services.auth_service import get_tenant_db


class PackageRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.Session = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        db = self.Session()
        try:
            self.tenant = Tenant(id="tenant-package-api", name="资源包组织")
            self.other_tenant = Tenant(id="tenant-package-api-other", name="其他组织")
            self.owner = User(
                id="owner-package-api",
                tenant_id=self.tenant.id,
                email="owner-package-api@example.test",
                password_hash="test-only",
                status="active",
            )
            self.viewer = User(
                id="viewer-package-api",
                tenant_id=self.tenant.id,
                email="viewer-package-api@example.test",
                password_hash="test-only",
                status="active",
            )
            self.reviewer = User(
                id="reviewer-package-api",
                tenant_id=self.tenant.id,
                email="reviewer-package-api@example.test",
                password_hash="test-only",
                status="active",
            )
            self.outsider = User(
                id="outsider-package-api",
                tenant_id=self.other_tenant.id,
                email="outsider-package-api@example.test",
                password_hash="test-only",
                status="active",
            )
            self.scenario = BusinessScenario(
                id="scenario-package-api", tenant_id=self.tenant.id, name="资源包场景"
            )
            self.public_scenario = BusinessScenario(
                id="scenario-package-public",
                tenant_id=self.tenant.id,
                name="公共资源包场景",
                is_public=True,
            )
            entity = OntologyEntity(
                id="entity-package-api", scenario_id=self.scenario.id, name="订单"
            )
            source = DataSource(
                id="source-package-api",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                name="安全连接",
                type="postgres",
                config={"password": "must-not-leak-over-http"},
            )
            action = OntologyAction(
                id="action-package-api",
                scenario_id=self.scenario.id,
                entity_id=entity.id,
                name="同步订单",
                executor_type="http",
                executor_config={"headers": {"Authorization": "Bearer must-not-leak-over-http"}},
            )
            db.add_all([
                self.tenant, self.other_tenant, self.owner, self.viewer, self.reviewer, self.outsider,
                self.scenario, self.public_scenario, entity, source, action,
            ])
            db.commit()
            organization = permission_service.ensure_organization(
                db, self.tenant.id, owner_user_id=self.owner.id
            )
            permission_service.assign_member_role(
                db, organization, user_id=self.viewer.id, role_key="viewer"
            )
            permission_service.ensure_organization(
                db, self.other_tenant.id, owner_user_id=self.outsider.id
            )
            db.commit()
        finally:
            db.close()

        self.current_user = self.owner
        self.app = FastAPI()
        self.app.include_router(packages.router, prefix="/api")
        self.app.include_router(starter_kits.router, prefix="/api")

        def override_db():
            request_db = self.Session()
            request_db.info["user_id"] = self.current_user.id
            request_db.info["tenant_id"] = self.current_user.tenant_id
            try:
                yield request_db
            finally:
                request_db.close()

        self.app.dependency_overrides[get_tenant_db] = override_db
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()

    def _as(self, user: User) -> None:
        self.current_user = user

    def _create_branch(self) -> str:
        db = self.Session()
        try:
            db.info["user_id"] = self.owner.id
            db.info["tenant_id"] = self.tenant.id
            branch = release_service.create_branch(
                db, self.scenario.id, name="import/main", description="资源包导入分支"
            )
            return branch.id
        finally:
            db.close()

    def _applicable_package(self) -> dict:
        package = {
            "format": package_service.PACKAGE_FORMAT,
            "version": package_service.PACKAGE_VERSION,
            "manifest": {"name": "客户资源包", "description": "仅声明式资源"},
            "resources": {
                "entities": [{
                    "key": "entity/客户",
                    "name": "客户",
                    "description": "从受治理资源包导入",
                    "icon": "user",
                    "color": "#2563eb",
                    "is_abstract": False,
                }],
                "properties": [{
                    "key": "property/entity/客户/客户编号",
                    "entity_ref": "entity/客户",
                    "name": "客户编号",
                    "data_type": "string",
                    "description": "外部客户主键",
                    "is_key": True,
                    "is_required": True,
                    "is_enum": False,
                    "enum_values": [],
                    "default_value": "",
                    "is_sensitive": False,
                }],
                "relations": [],
                "mappings": [{
                    "key": "mapping/entity/客户/data-source/安全连接/postgres/customers",
                    "entity_ref": "entity/客户",
                    "data_source_ref": {
                        "name": "安全连接", "type": "postgres", "scope": "scenario"
                    },
                    "table_name": "customers",
                    "column_map": {"客户编号": "customer_code"},
                }],
                "actions": [],
                "rules": [],
                "events": [],
                "workflows": [],
            },
        }
        package["manifest"]["fingerprint"] = package_service.package_fingerprint(package)
        return package

    def test_export_and_preview_are_safe_and_read_only(self) -> None:
        exported = self.client.get(f"/api/packages/scenarios/{self.scenario.id}/export")
        self.assertEqual(exported.status_code, 200, exported.text)
        package = exported.json()
        rendered = json.dumps(package, ensure_ascii=False)
        self.assertNotIn("must-not-leak-over-http", rendered)
        self.assertEqual(package["format"], package_service.PACKAGE_FORMAT)

        db = self.Session()
        try:
            before_entities = db.scalar(select(func.count()).select_from(OntologyEntity))
        finally:
            db.close()
        preview = self.client.post(
            f"/api/packages/scenarios/{self.scenario.id}/import-preview",
            json={"package": package},
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        result = preview.json()
        self.assertFalse(result["proposal"]["mutates_target"])
        self.assertEqual(result["proposal"]["mode"], "preview")
        db = self.Session()
        try:
            self.assertEqual(
                before_entities, db.scalar(select(func.count()).select_from(OntologyEntity))
            )
        finally:
            db.close()

    def test_read_only_and_cross_tenant_boundaries(self) -> None:
        self._as(self.viewer)
        export = self.client.get(f"/api/packages/scenarios/{self.scenario.id}/export")
        self.assertEqual(export.status_code, 200, export.text)
        denied_preview = self.client.post(
            f"/api/packages/scenarios/{self.scenario.id}/import-preview",
            json={"package": export.json()},
        )
        self.assertEqual(denied_preview.status_code, 403, denied_preview.text)

        self._as(self.outsider)
        denied_public_export = self.client.get(
            f"/api/packages/scenarios/{self.public_scenario.id}/export"
        )
        self.assertEqual(denied_public_export.status_code, 403, denied_public_export.text)

    def test_starter_kit_catalog_and_preview_never_apply_live_resources(self) -> None:
        catalog = self.client.get("/api/starter-kits")
        self.assertEqual(catalog.status_code, 200, catalog.text)
        self.assertEqual(
            [item["id"] for item in catalog.json()],
            ["retail", "bookkeeping", "supply-chain"],
        )
        self.assertNotIn("package", catalog.json()[0])
        missing = self.client.get("/api/starter-kits/not-a-kit")
        self.assertEqual(missing.status_code, 404, missing.text)

        db = self.Session()
        try:
            before_entities = db.scalar(select(func.count()).select_from(OntologyEntity))
        finally:
            db.close()
        preview = self.client.post(
            f"/api/starter-kits/bookkeeping/scenarios/{self.scenario.id}/import-preview",
            json={"environment": "dev"},
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        preview_result = preview.json()
        self.assertFalse(preview_result["proposal"]["mutates_target"])
        self.assertTrue(preview_result["applicable"], preview.text)
        self.assertEqual(preview_result["starter_kit"]["id"], "bookkeeping")
        self.assertTrue(preview_result["starter_kit"]["fingerprint"].startswith("sha256:"))
        db = self.Session()
        try:
            self.assertEqual(
                before_entities,
                db.scalar(select(func.count()).select_from(OntologyEntity)),
            )
        finally:
            db.close()

    def test_starter_kit_creates_only_a_governed_proposal_with_provenance(self) -> None:
        branch_id = self._create_branch()
        preview = self.client.post(
            f"/api/starter-kits/bookkeeping/scenarios/{self.scenario.id}/import-preview",
            json={"environment": "dev"},
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        kit = preview.json()["starter_kit"]
        created = self.client.post(
            f"/api/starter-kits/bookkeeping/scenarios/{self.scenario.id}/import-proposal",
            json={
                "branch_id": branch_id,
                "expected_fingerprint": kit["fingerprint"],
                "title": "导入财税核算 Starter Kit",
                "description": "以受治理提案引入财税本体基础定义。",
                "submit": True,
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        result = created.json()
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["starter_kit"]["id"], "bookkeeping")
        self.assertEqual(result["package_fingerprint"], kit["fingerprint"])

        db = self.Session()
        try:
            self.assertIsNone(
                db.execute(
                    select(OntologyEntity).where(
                        OntologyEntity.scenario_id == self.scenario.id,
                        OntologyEntity.name == "会计科目",
                    )
                ).scalars().first()
            )
            proposal = db.get(OntologyProposal, result["id"])
            self.assertIsNotNone(proposal)
            self.assertIn("Starter Kit 导入审计：bookkeeping@", proposal.description)
            self.assertIn(kit["fingerprint"], proposal.description)
        finally:
            db.close()

    def test_starter_kit_proposal_requires_the_previewed_fingerprint(self) -> None:
        branch_id = self._create_branch()
        preview = self.client.post(
            f"/api/starter-kits/retail/scenarios/{self.scenario.id}/import-preview",
            json={"environment": "dev"},
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        expected = preview.json()["starter_kit"]["fingerprint"]

        # This simulates a catalog artifact changing after the user completed a
        # preview.  The write endpoint must fail closed before creating a
        # proposal, rather than silently importing the newly loaded content.
        stale_fingerprint = "sha256:" + ("0" * 64)
        self.assertNotEqual(stale_fingerprint, expected)
        created = self.client.post(
            f"/api/starter-kits/retail/scenarios/{self.scenario.id}/import-proposal",
            json={
                "branch_id": branch_id,
                "expected_fingerprint": stale_fingerprint,
                "title": "不应使用已过期预检创建的 Starter Kit 提案",
                "submit": True,
            },
        )
        self.assertEqual(created.status_code, 409, created.text)
        self.assertIn("重新预检", str(created.json().get("detail", "")))

        db = self.Session()
        try:
            self.assertEqual(db.scalar(select(func.count()).select_from(OntologyProposal)), 0)
        finally:
            db.close()

    def test_starter_kit_preview_requires_target_write_permission(self) -> None:
        self._as(self.viewer)
        denied = self.client.post(
            f"/api/starter-kits/retail/scenarios/{self.scenario.id}/import-preview",
            json={"environment": "dev"},
        )
        self.assertEqual(denied.status_code, 403, denied.text)

    def test_applicable_package_creates_only_a_governed_proposal(self) -> None:
        branch_id = self._create_branch()
        package = self._applicable_package()
        preview = self.client.post(
            f"/api/packages/scenarios/{self.scenario.id}/import-preview",
            json={"package": package},
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertTrue(preview.json()["applicable"], preview.text)

        created = self.client.post(
            f"/api/packages/scenarios/{self.scenario.id}/import-proposal",
            json={
                "package": package,
                "branch_id": branch_id,
                "title": "导入客户资源包",
                "description": "路由级治理回归",
                "submit": True,
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        result = created.json()
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["package_fingerprint"], package["manifest"]["fingerprint"])
        self.assertEqual(result["summary"]["create"], 3)

        db = self.Session()
        try:
            # Proposal creation must not write live ontology/data-mapping rows.
            self.assertIsNone(
                db.execute(
                    select(OntologyEntity).where(
                        OntologyEntity.scenario_id == self.scenario.id,
                        OntologyEntity.name == "客户",
                    )
                ).scalars().first()
            )
            self.assertEqual(
                db.scalar(
                    select(func.count()).select_from(DataMapping).where(
                        DataMapping.scenario_id == self.scenario.id
                    )
                ),
                0,
            )
            proposal = db.get(OntologyProposal, result["id"])
            self.assertIsNotNone(proposal)
            self.assertIn(package["manifest"]["fingerprint"], proposal.description)
            snapshot = db.get(OntologySnapshot, result["proposed_snapshot_id"])
            self.assertIsNotNone(snapshot)
            self.assertEqual(
                next(item for item in snapshot.content["entities"] if item["name"] == "客户")["properties"][0]["name"],
                "客户编号",
            )
            self.assertEqual(snapshot.content["mappings"][0]["data_source_id"], "source-package-api")
        finally:
            db.close()

    def test_proposal_rejects_missing_or_mismatched_original_fingerprint(self) -> None:
        branch_id = self._create_branch()
        valid_package = self._applicable_package()
        missing_fingerprint = copy.deepcopy(valid_package)
        missing_fingerprint["manifest"].pop("fingerprint")
        mismatched_fingerprint = copy.deepcopy(valid_package)
        mismatched_fingerprint["manifest"]["description"] = "上传后篡改的描述"

        for label, package, expected_message in (
            ("missing", missing_fingerprint, "缺少完整性指纹"),
            ("mismatched", mismatched_fingerprint, "完整性指纹不匹配"),
        ):
            with self.subTest(label=label):
                created = self.client.post(
                    f"/api/packages/scenarios/{self.scenario.id}/import-proposal",
                    json={
                        "package": package,
                        "branch_id": branch_id,
                        "title": f"不应进入提案的 {label} 资源包",
                        "submit": True,
                    },
                )
                self.assertEqual(created.status_code, 400, created.text)
                self.assertIn(expected_message, str(created.json().get("detail", "")))

        db = self.Session()
        try:
            self.assertEqual(db.scalar(select(func.count()).select_from(OntologyProposal)), 0)
        finally:
            db.close()

    def test_package_proposal_revalidates_a_stale_branch_baseline(self) -> None:
        branch_id = self._create_branch()
        package = self._applicable_package()
        db = self.Session()
        try:
            entity = db.get(OntologyEntity, "entity-package-api")
            entity.description = "在预检后发生的实时变更"
            db.commit()
        finally:
            db.close()

        created = self.client.post(
            f"/api/packages/scenarios/{self.scenario.id}/import-proposal",
            json={
                "package": package,
                "branch_id": branch_id,
                "title": "不应创建的陈旧导入",
                "submit": True,
            },
        )
        self.assertEqual(created.status_code, 409, created.text)
        db = self.Session()
        try:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(OntologyProposal)), 0
            )
        finally:
            db.close()

    def test_imported_mapping_reaches_live_state_only_after_independent_review_and_merge(self) -> None:
        branch_id = self._create_branch()
        package = self._applicable_package()
        created = self.client.post(
            f"/api/packages/scenarios/{self.scenario.id}/import-proposal",
            json={
                "package": package,
                "branch_id": branch_id,
                "title": "受治理客户导入",
                "submit": True,
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        proposal_id = created.json()["id"]

        db = self.Session()
        try:
            db.info["user_id"] = self.reviewer.id
            db.info["tenant_id"] = self.tenant.id
            review = release_service.create_review(
                db, proposal_id, decision="approve", comment="独立评审通过"
            )
            self.assertEqual(review.decision, "approve")

            db.info["user_id"] = self.owner.id
            db.info["tenant_id"] = self.tenant.id
            merged = release_service.merge_proposal(db, proposal_id, confirmed=True)
            self.assertEqual(merged.status, "merged")
        finally:
            db.close()

        db = self.Session()
        try:
            customer = db.execute(
                select(OntologyEntity).where(
                    OntologyEntity.scenario_id == self.scenario.id,
                    OntologyEntity.name == "客户",
                )
            ).scalars().one()
            mapping = db.execute(
                select(DataMapping).where(
                    DataMapping.scenario_id == self.scenario.id,
                    DataMapping.entity_id == customer.id,
                )
            ).scalars().one()
            self.assertEqual(mapping.data_source_id, "source-package-api")
            self.assertEqual(mapping.table_name, "customers")
            self.assertEqual(mapping.column_map, {"客户编号": "customer_code"})
        finally:
            db.close()

if __name__ == "__main__":
    unittest.main()
