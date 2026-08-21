"""P2 发布治理的路由级集成测试。"""
from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    ActionExecutionLog,
    BusinessScenario,
    DataMapping,
    DataSource,
    OntologyAction,
    OntologyBranch,
    OntologyEntity,
    OntologyInstance,
    OntologyProposal,
    OntologyRelease,
    OntologyProperty,
    OntologySnapshot,
    OntologyWorkflow,
    Tenant,
    User,
    WorkflowRun,
)
from app.routers import releases
from app.services import permission_service, release_service
from app.services.auth_service import get_current_user


class ReleaseGovernanceIntegrationTests(unittest.TestCase):
    """覆盖完整治理闭环，以及租户/组织与凭据的硬边界。"""

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
            self.tenant = Tenant(id="tenant-release", name="发布组织")
            self.other_tenant = Tenant(id="tenant-other", name="其他组织")
            self.owner = User(
                id="owner-release",
                tenant_id=self.tenant.id,
                email="owner-release@example.test",
                password_hash="test-only",
                status="active",
            )
            self.reviewer = User(
                id="reviewer-release",
                tenant_id=self.tenant.id,
                email="reviewer-release@example.test",
                password_hash="test-only",
                status="active",
            )
            self.operator = User(
                id="operator-release",
                tenant_id=self.tenant.id,
                email="operator-release@example.test",
                password_hash="test-only",
                status="active",
            )
            self.outsider = User(
                id="outsider-release",
                tenant_id=self.other_tenant.id,
                email="outsider-release@example.test",
                password_hash="test-only",
                status="active",
            )
            self.scenario = BusinessScenario(
                id="scenario-release",
                tenant_id=self.tenant.id,
                name="订单治理场景",
                description="初始定义",
                industry="retail",
                status="draft",
            )
            self.foreign_public_scenario = BusinessScenario(
                id="scenario-release-public-foreign",
                tenant_id=self.other_tenant.id,
                name="外部公开场景",
                is_public=True,
            )
            self.entity = OntologyEntity(
                id="entity-release",
                scenario_id=self.scenario.id,
                name="订单",
                description="初始实体说明",
            )
            self.property = OntologyProperty(
                id="property-release",
                entity_id=self.entity.id,
                name="订单号",
                data_type="string",
                is_key=True,
            )
            self.action = OntologyAction(
                id="action-release",
                scenario_id=self.scenario.id,
                entity_id=self.entity.id,
                name="同步订单",
                executor_type="http",
                executor_config={
                    "url": "https://connector.example.test/orders",
                    "timeout": 10,
                    "headers": {"Authorization": "Bearer REAL-RELEASE-SECRET"},
                    "api_key": "API-KEY-NEVER-LEAK",
                    "nested": {"opaque": "Bearer NESTED-STRING-SECRET"},
                    "connection": "postgresql://release_user:CONNECTION-STRING-SECRET@db.example.test/app",
                    "literal": "api_key=STRING-FORM-SECRET",
                    # ``key`` 本身可能是普通业务配置，不能被凭据脱敏规则误伤。
                    "key": "business-key",
                },
            )
            # 用于验证删除定义的 proposal 会在同一事务内被拒绝，且不影响实时本体。
            self.instance = OntologyInstance(
                id="instance-release",
                scenario_id=self.scenario.id,
                entity_id=self.entity.id,
                name="运行中订单",
                attributes={"订单号": "SO-001"},
            )
            db.add_all(
                [
                    self.tenant,
                    self.other_tenant,
                    self.owner,
                    self.reviewer,
                    self.operator,
                    self.outsider,
                    self.scenario,
                    self.foreign_public_scenario,
                    self.entity,
                    self.property,
                    self.action,
                    self.instance,
                ]
            )
            db.commit()
            organization = permission_service.ensure_organization(
                db, self.tenant.id, owner_user_id=self.owner.id
            )
            permission_service.assign_member_role(
                db, organization, user_id=self.operator.id, role_key="operator"
            )
            permission_service.ensure_organization(
                db, self.other_tenant.id, owner_user_id=self.outsider.id
            )
            db.commit()
        finally:
            db.close()

        self.current_user_id = self.owner.id
        self.current_tenant_id = self.tenant.id
        self.app = FastAPI()
        self.app.include_router(releases.router, prefix="/api")

        def override_current_user():
            return SimpleNamespace(id=self.current_user_id, tenant_id=self.current_tenant_id)

        def override_db():
            request_db = self.Session()
            request_db.info["user_id"] = self.current_user_id
            request_db.info["tenant_id"] = self.current_tenant_id
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

    def _as(self, user: User, tenant: Tenant) -> None:
        self.current_user_id = user.id
        self.current_tenant_id = tenant.id

    def _create_branch_and_baseline(self) -> tuple[dict, dict]:
        response = self.client.post(
            f"/api/releases/scenarios/{self.scenario.id}/branches",
            json={"name": "main", "description": "主发布分支"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        branch = response.json()
        baseline = self.client.get(
            f"/api/releases/snapshots/{branch['head_snapshot_id']}"
        )
        self.assertEqual(baseline.status_code, 200, baseline.text)
        return branch, baseline.json()

    def _create_and_approve(
        self,
        branch_id: str,
        content: dict,
        title: str = "治理变更",
        *,
        assert_author_cannot_review: bool = False,
    ) -> dict:
        self._as(self.owner, self.tenant)
        proposed = self.client.post(
            f"/api/releases/branches/{branch_id}/proposals",
            json={"title": title, "description": "受控提案", "content": content, "submit": True},
        )
        self.assertEqual(proposed.status_code, 200, proposed.text)
        proposal = proposed.json()

        if assert_author_cannot_review:
            self_review = self.client.post(
                f"/api/releases/proposals/{proposal['id']}/reviews",
                json={"decision": "approve"},
            )
            self.assertEqual(self_review.status_code, 403, self_review.text)

        self._as(self.reviewer, self.tenant)
        review = self.client.post(
            f"/api/releases/proposals/{proposal['id']}/reviews",
            json={"decision": "approve", "comment": "评审通过"},
        )
        self.assertEqual(review.status_code, 200, review.text)
        self.assertEqual(review.json()["decision"], "approve")
        return proposal

    def _live_action_config(self) -> dict:
        db = self.Session()
        try:
            return copy.deepcopy(db.get(OntologyAction, self.action.id).executor_config)
        finally:
            db.close()

    def test_governed_merge_publish_and_rollback_preserve_secrets(self) -> None:
        branch, baseline = self._create_branch_and_baseline()
        serialized_baseline = json.dumps(baseline, ensure_ascii=False)
        self.assertNotIn("REAL-RELEASE-SECRET", serialized_baseline)
        self.assertNotIn("API-KEY-NEVER-LEAK", serialized_baseline)
        self.assertNotIn("NESTED-STRING-SECRET", serialized_baseline)
        self.assertNotIn("CONNECTION-STRING-SECRET", serialized_baseline)
        self.assertNotIn("STRING-FORM-SECRET", serialized_baseline)
        config = baseline["content"]["actions"][0]["executor_config"]
        self.assertEqual(config["headers"]["Authorization"], {"__release_secret__": "preserve"})
        self.assertEqual(config["api_key"], {"__release_secret__": "preserve"})
        self.assertEqual(config["nested"]["opaque"], {"__release_secret__": "preserve"})
        self.assertEqual(config["connection"], {"__release_secret__": "preserve"})
        self.assertEqual(config["literal"], {"__release_secret__": "preserve"})
        self.assertEqual(config["key"], "business-key")

        changed = copy.deepcopy(baseline["content"])
        changed["scenario"]["description"] = "已由提案合并"
        changed["actions"][0]["executor_config"]["timeout"] = 20
        # 同时验证两种编辑器行为：占位 marker 和“未回传敏感字段”都不可清空真实凭据。
        changed["actions"][0]["executor_config"].pop("api_key")
        changed["actions"][0]["executor_config"].pop("literal")
        changed["actions"][0]["executor_config"]["nested"].pop("opaque")
        proposal = self._create_and_approve(
            branch["id"], changed, assert_author_cannot_review=True
        )

        # API 层的 confirmed Literal 拒绝 false。
        self._as(self.owner, self.tenant)
        not_confirmed = self.client.post(
            f"/api/releases/proposals/{proposal['id']}/merge",
            json={"confirmed": False},
        )
        self.assertEqual(not_confirmed.status_code, 422)
        numeric_confirmation = self.client.post(
            f"/api/releases/proposals/{proposal['id']}/merge",
            json={"confirmed": 1},
        )
        self.assertEqual(numeric_confirmation.status_code, 422)

        merged = self.client.post(
            f"/api/releases/proposals/{proposal['id']}/merge",
            json={"confirmed": True, "note": "合并到主干"},
        )
        self.assertEqual(merged.status_code, 200, merged.text)
        self.assertEqual(merged.json()["status"], "merged")
        self.assertNotIn("REAL-RELEASE-SECRET", json.dumps(merged.json(), ensure_ascii=False))
        live_config = self._live_action_config()
        self.assertEqual(live_config["timeout"], 20)
        self.assertEqual(live_config["headers"]["Authorization"], "Bearer REAL-RELEASE-SECRET")
        self.assertEqual(live_config["api_key"], "API-KEY-NEVER-LEAK")
        self.assertEqual(live_config["nested"]["opaque"], "Bearer NESTED-STRING-SECRET")
        self.assertEqual(
            live_config["connection"],
            "postgresql://release_user:CONNECTION-STRING-SECRET@db.example.test/app",
        )
        self.assertEqual(live_config["literal"], "api_key=STRING-FORM-SECRET")
        self.assertEqual(live_config["key"], "business-key")

        published = self.client.post(
            f"/api/releases/scenarios/{self.scenario.id}/publish",
            json={"environment": "dev", "branch_id": branch["id"], "confirmed": True},
        )
        self.assertEqual(published.status_code, 200, published.text)
        self.assertEqual(published.json()["environment"], "dev")
        self.assertEqual(published.json()["status"], "released")

        rolled_back = self.client.post(
            f"/api/releases/scenarios/{self.scenario.id}/rollback",
            json={
                "target_snapshot_id": baseline["id"],
                "branch_id": branch["id"],
                "environment": "dev",
                "reason": "恢复稳定定义",
                "confirmed": True,
            },
        )
        self.assertEqual(rolled_back.status_code, 200, rolled_back.text)
        self.assertEqual(rolled_back.json()["target_snapshot_id"], baseline["id"])

        db = self.Session()
        try:
            scenario = db.get(BusinessScenario, self.scenario.id)
            self.assertEqual(scenario.description, "初始定义")
        finally:
            db.close()
        restored_config = self._live_action_config()
        self.assertEqual(restored_config["timeout"], 10)
        self.assertEqual(restored_config["headers"]["Authorization"], "Bearer REAL-RELEASE-SECRET")
        self.assertEqual(restored_config["api_key"], "API-KEY-NEVER-LEAK")
        self.assertEqual(restored_config["nested"]["opaque"], "Bearer NESTED-STRING-SECRET")
        self.assertEqual(
            restored_config["connection"],
            "postgresql://release_user:CONNECTION-STRING-SECRET@db.example.test/app",
        )
        self.assertEqual(restored_config["literal"], "api_key=STRING-FORM-SECRET")
        self.assertEqual(restored_config["key"], "business-key")

        records = self.client.get(
            f"/api/releases/scenarios/{self.scenario.id}/publish?environment=dev"
        )
        self.assertEqual(records.status_code, 200, records.text)
        self.assertIn("rolled_back", {record["status"] for record in records.json()})
        self.assertIn("released", {record["status"] for record in records.json()})

    def test_non_dev_publish_rejects_legacy_direct_connector_action(self) -> None:
        branch, baseline = self._create_branch_and_baseline()
        legacy = copy.deepcopy(baseline["content"])
        legacy["actions"][0]["executor_type"] = "sql"
        legacy["actions"][0]["executor_config"] = {
            "data_source_id": "legacy-data-source",
            "sql": "SELECT 1",
        }
        proposal = self._create_and_approve(branch["id"], legacy, "遗留 SQL 连接器")

        self._as(self.owner, self.tenant)
        merged = self.client.post(
            f"/api/releases/proposals/{proposal['id']}/merge",
            json={"confirmed": True},
        )
        self.assertEqual(merged.status_code, 200, merged.text)

        staging = self.client.post(
            f"/api/releases/scenarios/{self.scenario.id}/publish",
            json={"environment": "staging", "branch_id": branch["id"], "confirmed": True},
        )
        self.assertEqual(staging.status_code, 409, staging.text)
        self.assertIn("data_source_binding_key", staging.json()["detail"])
        records = self.client.get(
            f"/api/releases/scenarios/{self.scenario.id}/publish?environment=staging"
        )
        self.assertEqual(records.status_code, 200, records.text)
        self.assertEqual(records.json(), [])

        dev = self.client.post(
            f"/api/releases/scenarios/{self.scenario.id}/publish",
            json={"environment": "dev", "branch_id": branch["id"], "confirmed": True},
        )
        self.assertEqual(dev.status_code, 200, dev.text)

    def test_non_dev_publish_rejects_llm_default_model_fallback(self) -> None:
        branch, baseline = self._create_branch_and_baseline()
        content = copy.deepcopy(baseline["content"])
        # The fixture's default HTTP action is deliberately dev-only.  Disable
        # it here so this case isolates the LLM binding gate below.
        content["actions"][0]["enabled"] = False
        content["workflows"].append(
            {
                "id": "workflow-llm-default",
                "name": "默认模型流程",
                "description": "",
                "trigger_type": "manual",
                "trigger_config": {},
                "steps": [],
                "nodes": [
                    {"id": "start", "type": "start", "name": "开始", "data": {}},
                    {"id": "llm", "type": "llm", "name": "生成", "data": {"prompt": "x"}},
                    {"id": "end", "type": "end", "name": "结束", "data": {}},
                ],
                "edges": [
                    {"id": "e1", "source": "start", "target": "llm", "label": ""},
                    {"id": "e2", "source": "llm", "target": "end", "label": ""},
                ],
                "status": "active",
                "enabled": True,
                "access_scope": "tenant",
            }
        )
        proposal = self._create_and_approve(branch["id"], content, "默认模型工作流")

        self._as(self.owner, self.tenant)
        merged = self.client.post(
            f"/api/releases/proposals/{proposal['id']}/merge",
            json={"confirmed": True},
        )
        self.assertEqual(merged.status_code, 200, merged.text)

        staging = self.client.post(
            f"/api/releases/scenarios/{self.scenario.id}/publish",
            json={"environment": "staging", "branch_id": branch["id"], "confirmed": True},
        )
        self.assertEqual(staging.status_code, 409, staging.text)
        self.assertIn("llm_binding_key", staging.json()["detail"])
        records = self.client.get(
            f"/api/releases/scenarios/{self.scenario.id}/publish?environment=staging"
        )
        self.assertEqual(records.status_code, 200, records.text)
        self.assertEqual(records.json(), [])

    def test_non_dev_publish_rejects_host_bound_action_executors(self) -> None:
        """staging/prod releases may contain only governed SQL/MCP action paths."""
        for executor_type in ("http", "skill", "script"):
            with self.subTest(executor_type=executor_type):
                db = self.Session()
                try:
                    action = db.get(OntologyAction, self.action.id)
                    self.assertIsNotNone(action)
                    action.executor_type = executor_type
                    db.commit()
                finally:
                    db.close()

                self._as(self.owner, self.tenant)
                branch_response = self.client.post(
                    f"/api/releases/scenarios/{self.scenario.id}/branches",
                    json={"name": f"forbidden-{executor_type}", "description": "非开发门禁"},
                )
                self.assertEqual(branch_response.status_code, 200, branch_response.text)
                staging = self.client.post(
                    f"/api/releases/scenarios/{self.scenario.id}/publish",
                    json={
                        "environment": "staging",
                        "branch_id": branch_response.json()["id"],
                        "confirmed": True,
                    },
                )
                self.assertEqual(staging.status_code, 409, staging.text)
                self.assertIn("禁止", staging.json()["detail"])
                self.assertIn(executor_type, staging.json()["detail"])

    def test_non_dev_rollback_switches_environment_release_without_mutating_live_definition(self) -> None:
        """A staging rollback must never apply a snapshot to shared dev tables."""
        db = self.Session()
        try:
            # The default HTTP action is valid in dev but intentionally cannot
            # be staged; make the baseline non-executable for this isolation
            # test so the target itself passes the staging release gate.
            action = db.get(OntologyAction, self.action.id)
            self.assertIsNotNone(action)
            action.enabled = False
            db.commit()
        finally:
            db.close()

        self._as(self.owner, self.tenant)
        branch, baseline = self._create_branch_and_baseline()
        previous_content = copy.deepcopy(baseline["content"])
        previous_content["scenario"]["description"] = "旧预发定义"

        db = self.Session()
        try:
            previous = OntologySnapshot(
                id="snapshot-staging-before",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                branch_id=branch["id"],
                parent_snapshot_id=baseline["id"],
                kind="merge",
                content=previous_content,
                content_hash=release_service.snapshot_hash(previous_content),
                created_by_user_id=self.owner.id,
            )
            old_release = OntologyRelease(
                id="release-staging-before",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                branch_id=branch["id"],
                snapshot_id=previous.id,
                environment="staging",
                status="released",
                created_by_user_id=self.owner.id,
            )
            db.add_all([previous, old_release])
            db.commit()

            # Simulate continued dev authoring after staging was deployed.
            # Old rollback logic both rejected this divergence and would have
            # applied the target globally; neither is allowed for staging.
            scenario = db.get(BusinessScenario, self.scenario.id)
            action = db.get(OntologyAction, self.action.id)
            self.assertIsNotNone(scenario)
            self.assertIsNotNone(action)
            scenario.description = "开发环境继续演进"
            action.description = "开发 Action 保持不变"
            db.commit()
            snapshot_count = db.execute(
                select(OntologySnapshot).where(OntologySnapshot.branch_id == branch["id"])
            ).scalars().all()
            self.assertEqual(len(snapshot_count), 2)
        finally:
            db.close()

        rolled_back = self.client.post(
            f"/api/releases/scenarios/{self.scenario.id}/rollback",
            json={
                "target_snapshot_id": baseline["id"],
                "branch_id": branch["id"],
                "environment": "staging",
                "reason": "恢复预发稳定版本",
                "confirmed": True,
            },
        )
        self.assertEqual(rolled_back.status_code, 200, rolled_back.text)
        self.assertEqual(rolled_back.json()["target_snapshot_id"], baseline["id"])
        self.assertEqual(rolled_back.json()["result_snapshot_id"], baseline["id"])

        db = self.Session()
        try:
            scenario = db.get(BusinessScenario, self.scenario.id)
            action = db.get(OntologyAction, self.action.id)
            saved_branch = db.get(OntologyBranch, branch["id"])
            self.assertEqual(scenario.description, "开发环境继续演进")
            self.assertEqual(action.description, "开发 Action 保持不变")
            self.assertEqual(saved_branch.head_snapshot_id, baseline["id"])
            snapshots = db.execute(
                select(OntologySnapshot).where(OntologySnapshot.branch_id == branch["id"])
            ).scalars().all()
            self.assertEqual(len(snapshots), 2)

            old_release = db.get(OntologyRelease, "release-staging-before")
            self.assertEqual(old_release.status, "rolled_back")
            active = db.execute(
                select(OntologyRelease).where(
                    OntologyRelease.scenario_id == self.scenario.id,
                    OntologyRelease.environment == "staging",
                    OntologyRelease.status == "released",
                )
            ).scalars().all()
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0].snapshot_id, baseline["id"])
        finally:
            db.close()

    def test_draft_submission_requires_its_creator_and_current_branch_base(self) -> None:
        branch, baseline = self._create_branch_and_baseline()
        draft = self.client.post(
            f"/api/releases/branches/{branch['id']}/proposals",
            json={
                "title": "待提交草稿",
                "description": "先保存后评审",
                "content": baseline["content"],
                "submit": False,
            },
        )
        self.assertEqual(draft.status_code, 200, draft.text)
        self.assertEqual(draft.json()["status"], "draft")

        self._as(self.reviewer, self.tenant)
        denied = self.client.post(f"/api/releases/proposals/{draft.json()['id']}/submit")
        self.assertEqual(denied.status_code, 403, denied.text)

        self._as(self.owner, self.tenant)
        submitted = self.client.post(f"/api/releases/proposals/{draft.json()['id']}/submit")
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(submitted.json()["status"], "submitted")
        self.assertIsNotNone(submitted.json()["submitted_at"])

    def test_unsafe_merge_rolls_back_everything_and_keeps_live_definition(self) -> None:
        branch, baseline = self._create_branch_and_baseline()
        unsafe = copy.deepcopy(baseline["content"])
        unsafe["entities"] = []
        unsafe["actions"] = []
        proposal = self._create_and_approve(branch["id"], unsafe, "错误删除运行中实体")

        self._as(self.owner, self.tenant)
        rejected = self.client.post(
            f"/api/releases/proposals/{proposal['id']}/merge",
            json={"confirmed": True},
        )
        self.assertEqual(rejected.status_code, 400, rejected.text)
        self.assertIn("运行时对象", rejected.json()["detail"])

        db = self.Session()
        try:
            self.assertIsNotNone(db.get(OntologyEntity, self.entity.id))
            self.assertIsNotNone(db.get(OntologyAction, self.action.id))
            saved_proposal = db.get(OntologyProposal, proposal["id"])
            self.assertEqual(saved_proposal.status, "approved")
            snapshot_kinds = db.execute(
                select(OntologySnapshot.kind).where(OntologySnapshot.branch_id == branch["id"])
            ).scalars().all()
            self.assertNotIn("pre_merge", snapshot_kinds)
            self.assertNotIn("merge", snapshot_kinds)
            saved_branch = db.get(OntologyBranch, branch["id"])
            self.assertEqual(saved_branch.head_snapshot_id, baseline["id"])
        finally:
            db.close()

    def test_org_role_cross_tenant_and_missing_context_fail_closed(self) -> None:
        branch, _ = self._create_branch_and_baseline()

        # Generic scenario discovery can include a public foreign scenario, but
        # the release workspace must list only current-tenant governance targets.
        owned_catalog = self.client.get("/api/releases/scenarios")
        self.assertEqual(owned_catalog.status_code, 200, owned_catalog.text)
        self.assertEqual({item["id"] for item in owned_catalog.json()}, {self.scenario.id})

        self._as(self.operator, self.tenant)
        read = self.client.get(f"/api/releases/scenarios/{self.scenario.id}/branches")
        self.assertEqual(read.status_code, 200, read.text)
        denied_manage = self.client.post(
            f"/api/releases/scenarios/{self.scenario.id}/branches",
            json={"name": "operator-branch"},
        )
        self.assertEqual(denied_manage.status_code, 403)

        self._as(self.outsider, self.other_tenant)
        outsider_catalog = self.client.get("/api/releases/scenarios")
        self.assertEqual(outsider_catalog.status_code, 200, outsider_catalog.text)
        self.assertEqual(
            {item["id"] for item in outsider_catalog.json()}, {self.foreign_public_scenario.id}
        )
        cross_tenant = self.client.get(f"/api/releases/branches/{branch['id']}")
        self.assertEqual(cross_tenant.status_code, 404)
        hidden_snapshot = self.client.get(f"/api/releases/snapshots/{branch['head_snapshot_id']}")
        self.assertEqual(hidden_snapshot.status_code, 404)

        db = self.Session()
        try:
            with self.assertRaises(Exception) as error:
                release_service.create_branch(
                    db,
                    self.scenario.id,
                    name="no-context",
                )
            self.assertEqual(getattr(error.exception, "status_code", None), 401)
        finally:
            db.close()

    def test_snapshot_response_redacts_legacy_nested_string_credentials(self) -> None:
        branch, _ = self._create_branch_and_baseline()
        db = self.Session()
        try:
            # 模拟早期预览库或人工插入的历史 JSON：路由输出仍必须二次去敏。
            db.add(
                OntologySnapshot(
                    id="legacy-secret-snapshot",
                    tenant_id=self.tenant.id,
                    scenario_id=self.scenario.id,
                    branch_id=branch["id"],
                    kind="proposal",
                    content={
                        "nested": {"opaque": "Bearer LEGACY-NESTED-SECRET"},
                        "connection": "postgresql://legacy:LEGACY-CONNECTION-SECRET@db.test/app",
                        "literal": "token=LEGACY-TOKEN-SECRET",
                    },
                    content_hash="legacy-unsafe-content",
                )
            )
            db.commit()
        finally:
            db.close()

        response = self.client.get("/api/releases/snapshots/legacy-secret-snapshot")
        self.assertEqual(response.status_code, 200, response.text)
        serialized = json.dumps(response.json(), ensure_ascii=False)
        self.assertNotIn("LEGACY-NESTED-SECRET", serialized)
        self.assertNotIn("LEGACY-CONNECTION-SECRET", serialized)
        self.assertNotIn("LEGACY-TOKEN-SECRET", serialized)
        self.assertEqual(
            response.json()["content"]["nested"]["opaque"],
            {"__release_secret__": "preserve"},
        )

    def test_nonterminal_workflow_run_blocks_workflow_and_dependency_changes(self) -> None:
        db = self.Session()
        try:
            workflow = OntologyWorkflow(
                id="workflow-pending",
                scenario_id=self.scenario.id,
                name="待执行同步流程",
                status="active",
                enabled=True,
                steps=[{"step": 1, "type": "action", "action_id": self.action.id}],
            )
            run = WorkflowRun(
                id="run-pending",
                scenario_id=self.scenario.id,
                workflow_id=workflow.id,
                status="queued",
            )
            db.add_all([workflow, run])
            db.commit()
        finally:
            db.close()

        branch, baseline = self._create_branch_and_baseline()
        workflow_change = copy.deepcopy(baseline["content"])
        workflow_change["workflows"][0]["description"] = "不应影响已经排队的运行"
        workflow_proposal = self._create_and_approve(
            branch["id"], workflow_change, "错误修改运行中工作流"
        )
        self._as(self.owner, self.tenant)
        workflow_result = self.client.post(
            f"/api/releases/proposals/{workflow_proposal['id']}/merge",
            json={"confirmed": True},
        )
        self.assertEqual(workflow_result.status_code, 400, workflow_result.text)
        self.assertIn("运行中的工作流", workflow_result.json()["detail"])

        action_change = copy.deepcopy(baseline["content"])
        action_change["actions"][0]["description"] = "不应影响排队流程的 Action"
        action_proposal = self._create_and_approve(
            branch["id"], action_change, "错误修改运行中 Action"
        )
        self._as(self.owner, self.tenant)
        action_result = self.client.post(
            f"/api/releases/proposals/{action_proposal['id']}/merge",
            json={"confirmed": True},
        )
        self.assertEqual(action_result.status_code, 400, action_result.text)
        self.assertIn("运行中工作流引用的Action", action_result.json()["detail"])

        # 运行终态后才允许同一变更，确认阻塞条件不会永久锁死治理流程。
        db = self.Session()
        try:
            db.get(WorkflowRun, "run-pending").status = "succeeded"
            db.commit()
        finally:
            db.close()
        terminal_proposal = self._create_and_approve(
            branch["id"], action_change, "终态后允许修改 Action"
        )
        self._as(self.owner, self.tenant)
        terminal_result = self.client.post(
            f"/api/releases/proposals/{terminal_proposal['id']}/merge",
            json={"confirmed": True},
        )
        self.assertEqual(terminal_result.status_code, 200, terminal_result.text)

    def test_rollback_cannot_apply_a_snapshot_from_another_branch(self) -> None:
        main_branch, _ = self._create_branch_and_baseline()
        second = self.client.post(
            f"/api/releases/scenarios/{self.scenario.id}/branches",
            json={"name": "feature", "description": "隔离特性分支"},
        )
        self.assertEqual(second.status_code, 200, second.text)
        cross_branch = self.client.post(
            f"/api/releases/scenarios/{self.scenario.id}/rollback",
            json={
                "target_snapshot_id": second.json()["head_snapshot_id"],
                "branch_id": main_branch["id"],
                "confirmed": True,
            },
        )
        self.assertEqual(cross_branch.status_code, 400, cross_branch.text)
        self.assertIn("当前分支", cross_branch.json()["detail"])

    def test_mapping_and_execution_audit_references_block_definition_deletion(self) -> None:
        db = self.Session()
        try:
            mapping_entity = OntologyEntity(
                id="entity-mapping", scenario_id=self.scenario.id, name="映射实体"
            )
            mapping_property = OntologyProperty(
                id="property-mapping", entity_id=mapping_entity.id, name="映射字段"
            )
            source = DataSource(
                id="source-release",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                name="带凭据数据源",
                type="postgres",
                config={"password": "DATASOURCE-SECRET-NOT-IN-SNAPSHOT"},
            )
            mapping = DataMapping(
                id="mapping-release",
                scenario_id=self.scenario.id,
                entity_id=mapping_entity.id,
                data_source_id=source.id,
                table_name="orders",
                column_map={"映射字段": "source_column"},
            )
            db.add_all([mapping_entity, mapping_property, source, mapping])
            db.commit()
        finally:
            db.close()

        branch, baseline = self._create_branch_and_baseline()
        self.assertNotIn(
            "DATASOURCE-SECRET-NOT-IN-SNAPSHOT", json.dumps(baseline, ensure_ascii=False)
        )

        mapping_unsafe = copy.deepcopy(baseline["content"])
        mapping_unsafe["entities"] = [
            entity for entity in mapping_unsafe["entities"] if entity["id"] != "entity-mapping"
        ]
        # Mappings are now part of release snapshots.  Model the intended
        # deletion request rather than leaving a syntactically dangling mapping;
        # the merge guard should reject it because the live entity is still bound.
        mapping_unsafe["mappings"] = [
            mapping for mapping in mapping_unsafe["mappings"]
            if mapping["entity_id"] != "entity-mapping"
        ]
        mapping_proposal = self._create_and_approve(
            branch["id"], mapping_unsafe, "错误删除有映射的实体"
        )
        self._as(self.owner, self.tenant)
        mapping_result = self.client.post(
            f"/api/releases/proposals/{mapping_proposal['id']}/merge",
            json={"confirmed": True},
        )
        self.assertEqual(mapping_result.status_code, 400, mapping_result.text)
        self.assertIn("数据映射", mapping_result.json()["detail"])

        property_unsafe = copy.deepcopy(baseline["content"])
        next(
            entity for entity in property_unsafe["entities"] if entity["id"] == "entity-mapping"
        )["properties"] = []
        property_proposal = self._create_and_approve(
            branch["id"], property_unsafe, "错误删除有映射的属性"
        )
        self._as(self.owner, self.tenant)
        property_result = self.client.post(
            f"/api/releases/proposals/{property_proposal['id']}/merge",
            json={"confirmed": True},
        )
        self.assertEqual(property_result.status_code, 400, property_result.text)
        self.assertIn("数据映射", property_result.json()["detail"])

        db = self.Session()
        try:
            db.add(
                ActionExecutionLog(
                    id="execution-release",
                    scenario_id=self.scenario.id,
                    target_type="action",
                    target_id=self.action.id,
                    target_name=self.action.name,
                    status="success",
                )
            )
            db.commit()
        finally:
            db.close()

        action_unsafe = copy.deepcopy(baseline["content"])
        action_unsafe["actions"] = []
        execution_proposal = self._create_and_approve(
            branch["id"], action_unsafe, "错误删除有执行审计的 Action"
        )
        self._as(self.owner, self.tenant)
        execution_result = self.client.post(
            f"/api/releases/proposals/{execution_proposal['id']}/merge",
            json={"confirmed": True},
        )
        self.assertEqual(execution_result.status_code, 400, execution_result.text)
        self.assertIn("执行审计", execution_result.json()["detail"])


if __name__ == "__main__":
    unittest.main()
