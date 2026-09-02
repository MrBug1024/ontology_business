from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    AssistantCompilationJob,
    AssistantMessage,
    AssistantThread,
    BusinessScenario,
    DataMapping,
    DataSource,
    OntologyEntity,
    OntologyInstance,
    OntologyRelease,
    OntologyRelation,
    OntologyRule,
    OntologySnapshot,
    OntologyWorkflow,
    ScenarioModelDraftResource,
    Tenant,
    User,
)
from app.routers import assistant, scenarios
from app.services import (
    assistant_orchestrator,
    permission_service,
    scenario_model_compiler,
    scenario_model_draft_service,
)
from app.services.auth_service import get_current_user, get_tenant_db
from app.schemas import AssistantProposalApplyRequest


class ScenarioModelDraftMaterializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        self.db = self.Session()
        self.tenant = Tenant(id="tenant-model-drafts", name="场景草稿租户")
        self.foreign_tenant = Tenant(
            id="foreign-tenant-model-drafts",
            name="其他场景草稿租户",
        )
        self.owner = User(
            id="owner-model-drafts",
            tenant_id=self.tenant.id,
            email="model-drafts@example.test",
            password_hash="test-only",
            status="active",
        )
        self.viewer = User(
            id="viewer-model-drafts",
            tenant_id=self.tenant.id,
            email="model-drafts-viewer@example.test",
            password_hash="test-only",
            status="active",
        )
        self.foreign_owner = User(
            id="foreign-owner-model-drafts",
            tenant_id=self.foreign_tenant.id,
            email="foreign-model-drafts@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-model-drafts",
            tenant_id=self.tenant.id,
            name="项目风险治理",
            namespace="project-risk",
            status="draft",
        )
        self.db.add_all([
            self.tenant,
            self.foreign_tenant,
            self.owner,
            self.viewer,
            self.foreign_owner,
            self.scenario,
        ])
        self.db.commit()
        organization = permission_service.ensure_organization(
            self.db,
            self.tenant.id,
            owner_user_id=self.owner.id,
        )
        permission_service.assign_member_role(
            self.db,
            organization,
            user_id=self.viewer.id,
            role_key="viewer",
        )
        permission_service.ensure_organization(
            self.db,
            self.foreign_tenant.id,
            owner_user_id=self.foreign_owner.id,
        )
        self.db.commit()
        self.db.info["tenant_id"] = self.tenant.id
        self.db.info["user_id"] = self.owner.id

        self.current_tenant_id = self.tenant.id
        self.current_user_id = self.owner.id
        self.app = FastAPI()
        self.app.include_router(scenarios.router, prefix="/api")

        def override_current_user():
            return SimpleNamespace(
                id=self.current_user_id,
                tenant_id=self.current_tenant_id,
            )

        def override_db():
            request_db = self.Session()
            request_db.info["tenant_id"] = self.current_tenant_id
            request_db.info["user_id"] = self.current_user_id
            try:
                yield request_db
            finally:
                request_db.close()

        self.app.dependency_overrides[get_current_user] = override_current_user
        self.app.dependency_overrides[get_db] = override_db
        self.app.dependency_overrides[get_tenant_db] = override_db
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _schema() -> dict:
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

    def _bundle(self) -> dict:
        return scenario_model_compiler.build_source_bundle(
            "",
            [{
                "id": "draft-acceptance",
                "filename": "项目风险说明.md",
                "text": (
                    "项目以项目编号唯一标识，示例项目为 P-001，名称为滨江工程。"
                    "\n\n风险规则需要检查尚未定义的风险表达式。"
                    "\n\n风险处置流程包含一个当前不支持的外部调用节点。"
                    "\n\n项目台账来自 projects.csv，项目编号对应 project_code，名称对应 project_name。"
                ),
            }],
        )

    def _raw(self) -> dict:
        refs = [f"draft-acceptance:p{index:04d}" for index in range(1, 5)]
        return {
            "schema_version": scenario_model_compiler.SCHEMA_VERSION,
            "entities": [{
                "key": "entity.project",
                "name": "项目",
                "description": "需要进行风险治理的项目",
                "properties": [
                    {
                        "name": "项目编号",
                        "data_type": "string",
                        "is_key": True,
                        "is_title": True,
                        "is_required": True,
                    },
                    {
                        "name": "名称",
                        "data_type": "string",
                        "is_required": True,
                    },
                ],
                "evidence_refs": [refs[0]],
                "confidence": 1.0,
            }],
            "relations": [],
            "instances": [{
                "key": "instance.project.p001",
                "entity_ref": "entity.project",
                "display_name": "滨江工程",
                "values": {"项目编号": "P-001", "名称": "滨江工程"},
                "evidence_refs": [refs[0]],
                "confidence": 1.0,
            }],
            "functions": [],
            "actions": [],
            "rules": [{
                "key": "rule.project_risk",
                "name": "项目风险检查",
                "entity_ref": "entity.project",
                "condition": {"expression": "风险值超过动态阈值"},
                "severity": "warning",
                "evidence_refs": [refs[1]],
                "confidence": 0.8,
            }],
            "events": [],
            "workflows": [{
                "key": "workflow.risk_handling",
                "name": "风险处置流程",
                "trigger_type": "manual",
                "nodes": [{
                    "id": "external",
                    "type": "http",
                    "data": {"label": "调用外部风险平台"},
                }],
                "edges": [],
                "evidence_refs": [refs[2]],
                "confidence": 0.7,
            }],
            "mappings": [],
            "relation_mappings": [],
            "conceptual_mappings": [{
                "key": "conceptual_mapping.project_register",
                "mapping_kind": "object",
                "entity_ref": "entity.project",
                "source_label": "项目台账",
                "table_name": "projects.csv",
                "column_map": {
                    "项目编号": "project_code",
                    "名称": "project_name",
                },
                "binding_requirements": ["连接项目台账数据源"],
                "evidence_refs": [refs[3]],
                "confidence": 0.9,
            }],
            "unresolved": [{
                "code": "MAPPING_DEFERRED_NO_DATA_SOURCE",
                "message": "项目台账尚未连接物理数据源",
                "source_refs": [refs[3]],
                "blocking": False,
            }],
            "coverage": [
                {
                    "source_ref": refs[0],
                    "status": "modeled",
                    "reason": "项目定义与实例",
                    "change_keys": ["entity.project", "instance.project.p001"],
                },
                {
                    "source_ref": refs[1],
                    "status": "modeled",
                    "reason": "风险规则候选",
                    "change_keys": ["rule.project_risk"],
                },
                {
                    "source_ref": refs[2],
                    "status": "modeled",
                    "reason": "风险流程候选",
                    "change_keys": ["workflow.risk_handling"],
                },
                {
                    "source_ref": refs[3],
                    "status": "context",
                    "reason": "逻辑映射等待物理数据源",
                    "change_keys": ["conceptual_mapping.project_register"],
                },
            ],
        }

    def _normalized(self) -> dict:
        return scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            self._raw(),
            source_bundle=self._bundle(),
            mapping_catalog=[],
            columns_by_table={},
        )

    def _proposal(self) -> dict:
        return {
            "kind": "scenario_model",
            "proposal_id": "proposal-model-drafts",
            "payload": self._normalized(),
        }

    def _stage_saved_proposal(
        self,
        payload: dict,
        *,
        suffix: str,
    ) -> tuple[dict, AssistantThread, AssistantMessage]:
        proposal = assistant._build_proposal(
            "scenario_model",
            payload,
            self.scenario,
        )
        thread = AssistantThread(
            id=f"thread-{suffix}",
            tenant_id=self.tenant.id,
            created_by_user_id=self.owner.id,
            scenario_id=self.scenario.id,
            scope_key=(
                f"scenario:{self.scenario.id}|path:/scenarios/{self.scenario.id}"
            ),
            title="edited staging apply acceptance",
        )
        message = AssistantMessage(
            id=f"message-{suffix}",
            thread_id=thread.id,
            role="assistant",
            content="待确认场景模型草稿",
            context={"status": "waiting_confirmation"},
        )
        self.db.add_all([thread, message])
        self.db.flush()
        proposal = assistant._materialize_scenario_model_proposal(
            self.db,
            self.scenario,
            proposal,
            source_thread_id=thread.id,
            source_message_id=message.id,
        )
        message.proposal = proposal
        message.context = {
            "status": "waiting_confirmation",
            "model_run_id": proposal["proposal_id"],
        }
        self.db.commit()
        return proposal, thread, message

    def _patch_draft_by_key(
        self,
        proposal_id: str,
        resource_key: str,
        payload_update: dict,
    ) -> dict:
        listed = self.client.get(
            f"/api/scenarios/{self.scenario.id}/model-drafts",
            params={"proposal_id": proposal_id},
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        draft = next(
            item for item in listed.json()["items"]
            if item["resource_key"] == resource_key
        )
        patched = self.client.patch(
            f"/api/scenarios/{self.scenario.id}/model-drafts/{draft['id']}",
            json={
                "expected_revision": draft["revision"],
                "payload": {**draft["payload"], **payload_update},
            },
        )
        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertEqual(patched.json()["draft_status"], "needs_validation")
        return patched.json()

    def test_instances_and_unbound_mappings_are_visible_in_inert_draft_candidates(self) -> None:
        payload = self._normalized()
        by_kind = {
            item["resource_kind"]: item
            for item in payload["draft_candidates"]
        }

        self.assertIn("instance", by_kind)
        self.assertIn("conceptual_mapping", by_kind)
        self.assertEqual(
            by_kind["instance"]["payload"]["values"]["项目编号"],
            "P-001",
        )
        self.assertEqual(
            by_kind["conceptual_mapping"]["payload"]["column_map"],
            {"项目编号": "project_code", "名称": "project_name"},
        )
        self.assertTrue(all(
            item["enabled"] is False and item["publishable"] is False
            for item in payload["draft_candidates"]
        ))
        self.assertTrue(any(
            issue["code"] == "MAPPING_DEFERRED_NO_DATA_SOURCE"
            for issue in by_kind["conceptual_mapping"]["validation_issues"]
        ))
        self.assertEqual(payload["mappings"], [])
        self.assertFalse(any(
            change["resource"] in {"instance", "conceptual_mapping"}
            for change in payload["changes"]
        ))
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyInstance)),
            0,
        )
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(DataMapping)),
            0,
        )

    def test_invalid_rules_and_workflows_are_locatable_but_filtered_from_formal_apply(self) -> None:
        payload = self._normalized()
        partial, metadata = scenario_model_compiler.partial_scenario_model_payload(
            payload
        )
        candidates = {
            (item["resource_kind"], item["resource_key"]): item
            for item in payload["draft_candidates"]
        }
        rule = candidates[("rule", "rule.project_risk")]
        workflow = candidates[("workflow", "workflow.risk_handling")]

        self.assertTrue(any(
            issue["code"] == "invalid_rule_condition"
            and "draft-acceptance:p0002" in issue["source_refs"]
            for issue in rule["validation_issues"]
        ))
        self.assertTrue(any(
            issue["code"] == "unsupported_workflow_node"
            and "draft-acceptance:p0003" in issue["source_refs"]
            for issue in workflow["validation_issues"]
        ))
        self.assertNotIn(
            "rule.project_risk",
            {item["key"] for item in partial["rules"]},
        )
        self.assertNotIn(
            "workflow.risk_handling",
            {item["key"] for item in partial["workflows"]},
        )
        self.assertIn("rule.project_risk", metadata["blocked_change_keys"])
        self.assertIn("workflow.risk_handling", metadata["blocked_change_keys"])
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyRule)),
            0,
        )
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyWorkflow)),
            0,
        )
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyEntity)),
            0,
        )

    def test_materialized_drafts_are_acl_scoped_editable_and_replay_safe(self) -> None:
        proposal = self._proposal()
        summary = scenario_model_draft_service.materialize_draft_resources(
            self.db,
            self.scenario,
            proposal,
            source_thread_id="thread-model-drafts",
            source_message_id="message-model-drafts",
            compilation_job_id="job-model-drafts",
            created_by_user_id=self.owner.id,
        )
        self.db.commit()
        rows = list(self.db.scalars(
            select(ScenarioModelDraftResource).where(
                ScenarioModelDraftResource.scenario_id == self.scenario.id,
                ScenarioModelDraftResource.proposal_id == proposal["proposal_id"],
            )
        ).all())
        original_ids = {row.id for row in rows}

        self.assertEqual(summary["resource_count"], len(rows))
        self.assertGreaterEqual(summary["by_kind"].get("instance", 0), 1)
        self.assertGreaterEqual(summary["by_kind"].get("conceptual_mapping", 0), 1)
        self.assertTrue(all(not row.enabled and not row.publishable for row in rows))

        owner_response = self.client.get(
            f"/api/scenarios/{self.scenario.id}/model-drafts",
            params={"proposal_id": proposal["proposal_id"]},
        )
        self.assertEqual(owner_response.status_code, 200, owner_response.text)
        owner_items = owner_response.json()["items"]
        by_kind = {item["resource_kind"]: item for item in owner_items}
        self.assertEqual(
            by_kind["instance"]["payload"]["values"]["项目编号"],
            "P-001",
        )
        self.assertEqual(
            by_kind["conceptual_mapping"]["payload"]["column_map"],
            {"项目编号": "project_code", "名称": "project_name"},
        )
        self.assertTrue(all(
            item["enabled"] is False and item["publishable"] is False
            for item in owner_items
        ))

        edited_instance = {
            **by_kind["instance"]["payload"],
            "display_name": "用户修订后的滨江工程",
            "values": {
                **by_kind["instance"]["payload"]["values"],
                "名称": "用户修订后的滨江工程",
            },
        }
        patch_url = (
            f"/api/scenarios/{self.scenario.id}/model-drafts/"
            f"{by_kind['instance']['id']}"
        )
        patched = self.client.patch(
            patch_url,
            json={
                "expected_revision": by_kind["instance"]["revision"],
                "payload": edited_instance,
            },
        )
        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertEqual(patched.json()["revision"], 1)
        self.assertEqual(patched.json()["draft_status"], "needs_validation")
        self.assertEqual(
            patched.json()["payload"]["values"]["名称"],
            "用户修订后的滨江工程",
        )

        rejected_key_change = self.client.patch(
            patch_url,
            json={
                "expected_revision": patched.json()["revision"],
                "payload": {
                    **edited_instance,
                    "key": "instance.project.user-renamed-key",
                },
            },
        )
        self.assertEqual(rejected_key_change.status_code, 400)

        stale_patch = self.client.patch(
            patch_url,
            json={
                "expected_revision": by_kind["instance"]["revision"],
                "payload": {**edited_instance, "display_name": "过期覆盖"},
            },
        )
        self.assertEqual(stale_patch.status_code, 409, stale_patch.text)

        self.db.expire_all()
        scenario_model_draft_service.materialize_draft_resources(
            self.db,
            self.db.get(BusinessScenario, self.scenario.id),
            proposal,
            source_thread_id="thread-model-drafts",
            source_message_id="message-model-drafts",
            compilation_job_id="job-model-drafts",
            created_by_user_id=self.owner.id,
        )
        self.db.commit()
        replay_rows = list(self.db.scalars(
            select(ScenarioModelDraftResource).where(
                ScenarioModelDraftResource.scenario_id == self.scenario.id,
                ScenarioModelDraftResource.proposal_id == proposal["proposal_id"],
            )
        ).all())
        replay_instance = next(
            row for row in replay_rows if row.resource_kind == "instance"
        )
        self.assertEqual({row.id for row in replay_rows}, original_ids)
        self.assertEqual(replay_instance.revision, 1)
        self.assertEqual(
            replay_instance.payload["values"]["名称"],
            "用户修订后的滨江工程",
        )
        self.assertEqual(
            replay_instance.source_payload["values"]["名称"],
            "滨江工程",
        )

        self.current_user_id = self.viewer.id
        viewer_response = self.client.get(
            f"/api/scenarios/{self.scenario.id}/model-drafts"
        )
        self.assertEqual(viewer_response.status_code, 200, viewer_response.text)
        self.assertEqual(viewer_response.json()["items"], [])
        self.assertEqual(viewer_response.json()["total"], 0)
        self.assertEqual(viewer_response.json()["summary"]["resource_count"], 0)
        self.db.info["user_id"] = self.viewer.id
        self.assertEqual(
            scenario_model_draft_service.active_working_draft_context(
                self.db,
                self.scenario,
            ),
            [],
        )
        self.assertFalse(
            scenario_model_draft_service.has_active_working_drafts(
                self.db,
                self.scenario,
            )
        )
        continuation = assistant_orchestrator.AssistantSemanticDecision(
            goal="continue_work",
            scope="scenario_model",
            confidence="high",
            reason="继续当前活动草稿",
        )
        self.assertNotEqual(
            assistant_orchestrator.route_assistant_decision(
                continuation,
                has_active_model_drafts=False,
            ).intent,
            "scenario_model",
        )
        self.db.info["user_id"] = self.owner.id

        self.current_user_id = self.foreign_owner.id
        self.current_tenant_id = self.foreign_tenant.id
        foreign_response = self.client.get(
            f"/api/scenarios/{self.scenario.id}/model-drafts"
        )
        self.assertEqual(foreign_response.status_code, 404, foreign_response.text)

        for model in (
            OntologyEntity,
            OntologyInstance,
            DataMapping,
            OntologyRule,
            OntologyWorkflow,
            OntologySnapshot,
            OntologyRelease,
        ):
            self.assertEqual(
                self.db.scalar(select(func.count()).select_from(model)),
                0,
                model.__name__,
            )

    def test_draft_pagination_reports_global_and_page_totals_consistently(self) -> None:
        proposal = self._proposal()
        scenario_model_draft_service.materialize_draft_resources(
            self.db,
            self.scenario,
            proposal,
            created_by_user_id=self.owner.id,
        )
        self.db.commit()
        expected_ids = set(self.db.scalars(
            select(ScenarioModelDraftResource.id).where(
                ScenarioModelDraftResource.scenario_id == self.scenario.id,
                ScenarioModelDraftResource.proposal_id == proposal["proposal_id"],
                ScenarioModelDraftResource.created_by_user_id == self.owner.id,
                ScenarioModelDraftResource.draft_status.in_(
                    scenario_model_draft_service.OPEN_DRAFT_STATUSES
                ),
            )
        ).all())
        self.assertGreater(len(expected_ids), 2)

        offset = 0
        seen: list[str] = []
        while True:
            response = self.client.get(
                f"/api/scenarios/{self.scenario.id}/model-drafts",
                params={
                    "proposal_id": proposal["proposal_id"],
                    "offset": offset,
                    "limit": 2,
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            page = response.json()
            page_ids = [item["id"] for item in page["items"]]
            self.assertEqual(page["total"], len(expected_ids))
            self.assertEqual(
                page["summary"]["resource_count"],
                len(expected_ids),
            )
            self.assertEqual(
                page["page_summary"]["resource_count"],
                len(page_ids),
            )
            self.assertTrue(set(page_ids).isdisjoint(seen))
            seen.extend(page_ids)
            if not page["has_more"]:
                self.assertIsNone(page["next_offset"])
                break
            self.assertEqual(page["next_offset"], offset + len(page_ids))
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]

        self.assertEqual(set(seen), expected_ids)
        self.assertEqual(len(seen), len(expected_ids))

    def test_default_draft_api_exposes_ambiguous_old_and_new_candidates(self) -> None:
        def proposal(proposal_id: str, key: str, name: str, refs: list[str]) -> dict:
            candidate_payload = {
                "key": key,
                "name": name,
                "properties": [],
                "evidence_refs": refs,
            }
            return {
                "kind": "scenario_model",
                "proposal_id": proposal_id,
                "payload": {
                    "draft_candidates": [{
                        "resource_kind": "entity",
                        "resource_key": key,
                        "task_id": "ontology",
                        "payload": candidate_payload,
                        "evidence_refs": refs,
                        "validation_issues": [],
                        "validation_status": "ready_for_review",
                    }],
                    "entities": [],
                    "relations": [],
                    "functions": [],
                    "actions": [],
                    "rules": [],
                    "events": [],
                    "workflows": [],
                    "mappings": [],
                    "relation_mappings": [],
                    "unresolved": [],
                    "coverage": [],
                    "changes": [],
                    "tasks": [],
                },
            }

        original = proposal(
            "proposal-api-customer-original",
            "entity.customer",
            "Customer",
            ["attachment:p0001"],
        )
        scenario_model_draft_service.materialize_draft_resources(
            self.db,
            self.scenario,
            original,
            created_by_user_id=self.owner.id,
        )
        self.db.commit()
        customer = self.db.scalars(
            select(ScenarioModelDraftResource).where(
                ScenarioModelDraftResource.proposal_id
                == original["proposal_id"]
            )
        ).one()
        addition = proposal(
            "proposal-api-order-addition",
            "entity.order",
            "Order",
            ["user-request:p0001"],
        )
        scenario_model_draft_service.materialize_draft_resources(
            self.db,
            self.scenario,
            addition,
            created_by_user_id=self.owner.id,
            consumed_draft_revisions={customer.id: 0},
        )
        self.db.commit()

        response = self.client.get(
            f"/api/scenarios/{self.scenario.id}/model-drafts"
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["total"], 2)
        by_key = {item["resource_key"]: item for item in body["items"]}
        self.assertEqual(set(by_key), {"entity.customer", "entity.order"})
        self.assertEqual(by_key["entity.customer"]["draft_status"], "needs_attention")
        self.assertEqual(by_key["entity.order"]["draft_status"], "needs_attention")
        self.assertIn(
            "AMBIGUOUS_WORKING_DRAFT_LINEAGE",
            {
                issue["code"]
                for issue in by_key["entity.customer"]["validation_issues"]
            },
        )
        self.assertIn(
            "AMBIGUOUS_WORKING_DRAFT_SUCCESSOR",
            {
                issue["code"]
                for issue in by_key["entity.order"]["validation_issues"]
            },
        )

    def test_compilation_finalize_atomically_materializes_and_replays_without_overwrite(self) -> None:
        thread = AssistantThread(
            id="thread-finalize-drafts",
            tenant_id=self.tenant.id,
            created_by_user_id=self.owner.id,
            scenario_id=self.scenario.id,
            scope_key=f"scenario:{self.scenario.id}:/scenarios/{self.scenario.id}",
            title="编译物化验收",
        )
        assistant_message_id = "message-finalize-drafts"
        job = AssistantCompilationJob(
            id="job-finalize-drafts",
            request_fingerprint="f" * 64,
            tenant_id=self.tenant.id,
            created_by_user_id=self.owner.id,
            scenario_id=self.scenario.id,
            thread_id=thread.id,
            message_id=None,
            message_hash="1" * 64,
            attachment_content_hash="2" * 64,
            llm_config_fingerprint="3" * 64,
            mapping_context_fingerprint="4" * 64,
            execution_policy_fingerprint="5" * 64,
            compiler_version=scenario_model_compiler.COMPILER_VERSION,
            scenario_baseline=assistant._scenario_revision(self.scenario),
            status="running",
            progress={},
            llm_call_budget=20,
            llm_calls_used=4,
        )
        self.db.add_all([thread, job])
        self.db.commit()
        normalized = self._normalized()
        finalize_kwargs = {
            "tenant_id": self.tenant.id,
            "user_id": self.owner.id,
            "job_id": job.id,
            "thread_id": thread.id,
            "assistant_message_id": assistant_message_id,
            "scenario_id": self.scenario.id,
            "data": normalized,
            "reply": "已生成完整场景模型草稿。",
            "context": {"scenario_id": self.scenario.id},
            "sources": [],
            "thinking": [],
        }

        with patch.object(assistant, "SessionLocal", self.Session):
            proposal = assistant._finalize_compilation_success(**finalize_kwargs)

        self.db.expire_all()
        stored_job = self.db.get(AssistantCompilationJob, job.id)
        stored_message = self.db.get(AssistantMessage, assistant_message_id)
        rows = list(self.db.scalars(
            select(ScenarioModelDraftResource).where(
                ScenarioModelDraftResource.compilation_job_id == job.id
            )
        ).all())
        row_ids = {row.id for row in rows}

        self.assertEqual(stored_job.status, "succeeded")
        self.assertEqual(stored_job.message_id, assistant_message_id)
        self.assertIsNotNone(stored_message)
        self.assertEqual(stored_message.proposal["proposal_id"], proposal["proposal_id"])
        self.assertEqual(stored_job.result["proposal_id"], proposal["proposal_id"])
        self.assertEqual(
            proposal["payload"]["draft_materialization"]["resource_count"],
            len(rows),
        )
        self.assertIn("instance", {row.resource_kind for row in rows})
        self.assertIn("conceptual_mapping", {row.resource_kind for row in rows})
        self.assertTrue(all(
            row.source_thread_id == thread.id
            and row.source_message_id == assistant_message_id
            and not row.enabled
            and not row.publishable
            for row in rows
        ))

        instance = next(row for row in rows if row.resource_kind == "instance")
        patch_url = (
            f"/api/scenarios/{self.scenario.id}/model-drafts/{instance.id}"
        )
        patched_payload = {
            **instance.payload,
            "values": {
                **instance.payload["values"],
                "名称": "编译后用户修订",
            },
        }
        patched = self.client.patch(
            patch_url,
            json={
                "expected_revision": instance.revision,
                "payload": patched_payload,
            },
        )
        self.assertEqual(patched.status_code, 200, patched.text)

        with patch.object(assistant, "SessionLocal", self.Session):
            replay = assistant._finalize_compilation_success(**finalize_kwargs)

        self.db.expire_all()
        replay_rows = list(self.db.scalars(
            select(ScenarioModelDraftResource).where(
                ScenarioModelDraftResource.compilation_job_id == job.id
            )
        ).all())
        replay_instance = next(
            row for row in replay_rows if row.resource_kind == "instance"
        )
        self.assertEqual(replay["proposal_id"], proposal["proposal_id"])
        self.assertEqual({row.id for row in replay_rows}, row_ids)
        self.assertEqual(replay_instance.revision, 1)
        self.assertEqual(
            replay_instance.payload["values"]["名称"],
            "编译后用户修订",
        )
        self.assertEqual(
            replay_instance.source_payload["values"]["名称"],
            "滨江工程",
        )

    def test_third_structural_contract_failure_salvages_inert_drafts(self) -> None:
        source_ref = "structural-salvage:p0001"
        malformed = {
            "schema_version": scenario_model_compiler.SCHEMA_VERSION,
            "entities": [{
                "key": "entity.salvaged_project",
                "name": "待修复项目",
                "description": "结构错误响应中仍可识别的项目对象",
                "properties": [{
                    "name": "项目编号",
                    "data_type": "string",
                    "is_key": True,
                    "is_title": True,
                    "is_required": True,
                }],
                "evidence_refs": [source_ref],
                "confidence": 0.9,
            }],
            # One formal section violates the closed compiler contract even
            # though other candidate objects remain structurally recoverable.
            "relations": "malformed-scalar-section",
            "instances": [],
            "functions": [],
            "actions": [],
            "rules": [{
                "key": "rule.salvaged_project_check",
                "name": "待修复项目检查",
                "entity_ref": "entity.salvaged_project",
                "condition": {"expression": "项目编号 != null"},
                "severity": "warning",
                "evidence_refs": [source_ref],
                "confidence": 0.8,
            }],
            "events": [],
            "workflows": [],
            "mappings": [],
            "relation_mappings": [],
            "conceptual_mappings": [],
            "unresolved": [],
            "coverage": [{
                "source_ref": source_ref,
                "status": "modeled",
                "reason": "项目对象与检查规则",
                "change_keys": [
                    "entity.salvaged_project",
                    "rule.salvaged_project_check",
                ],
            }],
        }
        response = {"content": json.dumps(malformed, ensure_ascii=False)}
        with patch.object(
            scenario_model_compiler.llm_service,
            "chat",
            return_value=response,
        ) as provider:
            compiled = scenario_model_compiler.compile_scenario_model(
                self.db,
                self.scenario,
                message="修复并继续完整场景建模",
                documents=[{
                    "id": "structural-salvage",
                    "filename": "structural-salvage.md",
                    "text": "项目以项目编号唯一标识，并需要执行项目检查规则。",
                }],
                llm=object(),
            )
        self.assertEqual(provider.call_count, 3)
        formal_sections = (
            "entities", "relations", "functions", "actions", "rules",
            "events", "workflows", "mappings", "relation_mappings",
        )
        self.assertTrue(all(compiled.get(section) == [] for section in formal_sections))
        self.assertEqual(
            {item["resource_kind"] for item in compiled["draft_candidates"]},
            {"entity", "rule"},
        )
        self.assertIn(
            "COMPILER_CONTRACT_ERROR",
            {item["code"] for item in compiled["unresolved"]},
        )

        thread = AssistantThread(
            id="thread-structural-salvage",
            tenant_id=self.tenant.id,
            created_by_user_id=self.owner.id,
            scenario_id=self.scenario.id,
            scope_key=(
                f"scenario:{self.scenario.id}|path:/scenarios/{self.scenario.id}"
            ),
            title="structural salvage",
        )
        job = AssistantCompilationJob(
            id="job-structural-salvage",
            request_fingerprint="9" * 64,
            tenant_id=self.tenant.id,
            created_by_user_id=self.owner.id,
            scenario_id=self.scenario.id,
            thread_id=thread.id,
            message_id=None,
            message_hash="8" * 64,
            attachment_content_hash="7" * 64,
            llm_config_fingerprint="6" * 64,
            mapping_context_fingerprint="5" * 64,
            execution_policy_fingerprint="4" * 64,
            compiler_version=scenario_model_compiler.COMPILER_VERSION,
            scenario_baseline=assistant._scenario_revision(self.scenario),
            status="running",
            progress={},
            llm_call_budget=20,
            llm_calls_used=3,
        )
        self.db.add_all([thread, job])
        self.db.commit()
        with patch.object(assistant, "SessionLocal", self.Session):
            proposal = assistant._finalize_compilation_success(
                tenant_id=self.tenant.id,
                user_id=self.owner.id,
                job_id=job.id,
                thread_id=thread.id,
                assistant_message_id="message-structural-salvage",
                scenario_id=self.scenario.id,
                data=compiled,
                reply="结构损坏的输出已保留为待修复草稿。",
                context={"scenario_id": self.scenario.id},
                sources=[],
                thinking=[],
            )

        self.db.expire_all()
        rows = list(self.db.scalars(
            select(ScenarioModelDraftResource).where(
                ScenarioModelDraftResource.proposal_id == proposal["proposal_id"]
            )
        ).all())
        self.assertTrue({"entity", "rule"} <= {row.resource_kind for row in rows})
        staging_codes = {
            issue["code"]
            for row in rows
            for issue in row.validation_issues
        }
        self.assertNotIn("COMPILER_CONTRACT_ERROR", staging_codes)
        self.assertIn("formal_preflight_failed", staging_codes)
        finalized = self.db.get(
            AssistantMessage,
            "message-structural-salvage",
        ).proposal
        governance = finalized["payload"]["candidate_governance"]
        self.assertEqual(governance["revalidated_count"], len(rows))
        self.assertEqual(
            governance["eligible_count"] + governance["blocked_count"],
            len(rows),
        )
        # Recoverable candidates are classified independently, while invalid
        # salvage remains inert and the compilation run closes with gaps.
        summary = finalized["payload"]["execution_summary"]
        self.assertTrue(summary["final"])
        self.assertEqual(summary["status"], "completed_with_gaps")
        self.assertEqual(finalized["payload"]["execution_status"], "completed_with_gaps")
        self.assertEqual(finalized["payload"]["current_task_id"], "")
        self.assertFalse(finalized["requires_confirmation"])
        candidate_task_ids = {
            row.task_id for row in rows if row.task_id
        }
        task_statuses = {
            task["id"]: task["status"]
            for task in finalized["payload"]["tasks"]
        }
        self.assertTrue(candidate_task_ids)
        self.assertTrue(
            all(task_statuses[task_id] == "drafted_with_gaps" for task_id in candidate_task_ids)
        )
        stored_draft_statuses = set(self.db.scalars(
            select(ScenarioModelDraftResource.draft_status).where(
                ScenarioModelDraftResource.proposal_id == proposal["proposal_id"]
            )
        ).all())
        self.assertEqual(
            stored_draft_statuses,
            {"ready_for_review", "needs_attention"},
        )
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyEntity)),
            0,
        )
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyRule)),
            0,
        )
        for model in (
            OntologyEntity,
            OntologyInstance,
            DataMapping,
            OntologyRule,
            OntologyWorkflow,
            OntologySnapshot,
            OntologyRelease,
        ):
            self.assertEqual(
                self.db.scalar(select(func.count()).select_from(model)),
                0,
                model.__name__,
            )

    def test_empty_provider_contract_materializes_six_editable_stage_anchors(self) -> None:
        payload = scenario_model_compiler._inert_contract_salvage_payload(
            {},
            source_bundle=self._bundle(),
        )
        proposal = {
            "kind": "scenario_model",
            "proposal_id": "proposal-empty-contract-stage-anchors",
            "payload": payload,
            "status": "in_progress",
        }
        summary = scenario_model_draft_service.materialize_draft_resources(
            self.db,
            self.scenario,
            proposal,
            created_by_user_id=self.owner.id,
        )
        self.db.commit()

        rows = list(self.db.scalars(
            select(ScenarioModelDraftResource).where(
                ScenarioModelDraftResource.proposal_id
                == proposal["proposal_id"]
            )
        ).all())
        self.assertEqual(summary["resource_count"], 6)
        self.assertEqual(len(rows), 6)
        self.assertEqual(
            {row.resource_kind for row in rows},
            {
                "entity",
                "instance",
                "conceptual_mapping",
                "function",
                "rule",
                "workflow",
            },
        )
        self.assertEqual(
            {row.task_id for row in rows},
            {
                "ontology",
                "instances",
                "mapping",
                "capabilities",
                "rules",
                "workflows",
            },
        )
        self.assertTrue(all(not row.enabled and not row.publishable for row in rows))
        self.assertTrue(all(row.draft_status == "needs_attention" for row in rows))
        self.assertTrue(all(
            "COMPILER_NO_RECOVERABLE_CANDIDATE"
            in {issue["code"] for issue in row.validation_issues}
            for row in rows
        ))
        for model in (
            OntologyEntity,
            OntologyInstance,
            DataMapping,
            OntologyRule,
            OntologyWorkflow,
            OntologySnapshot,
            OntologyRelease,
        ):
            self.assertEqual(
                self.db.scalar(select(func.count()).select_from(model)),
                0,
                model.__name__,
            )

    def test_bound_data_source_mapping_is_staged_without_importing_instances(self) -> None:
        source = DataSource(
            id="source-model-drafts",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="项目主数据",
            type="postgres",
            config={},
            status="ok",
        )
        self.db.add(source)
        self.db.commit()
        raw = self._raw()
        raw["mappings"] = [{
            "key": "mapping.project_register",
            "entity_ref": "entity.project",
            "data_source_ref": source.id,
            "table_name": "projects",
            "column_map": {
                "项目编号": "project_code",
                "名称": "project_name",
            },
            "evidence_refs": ["draft-acceptance:p0004"],
            "confidence": 0.95,
        }]
        raw["conceptual_mappings"] = []
        raw["unresolved"] = []
        catalog = [{
            "data_source_id": source.id,
            "data_source_name": source.name,
            "type": source.type,
            "tables": [{
                "name": "projects",
                "columns": [
                    {"name": "project_code", "type": "TEXT", "pk": True},
                    {"name": "project_name", "type": "TEXT", "pk": False},
                ],
            }],
        }]
        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
            mapping_catalog=catalog,
            columns_by_table={
                (source.id, "projects"): {"project_code", "project_name"}
            },
        )
        proposal = {
            "kind": "scenario_model",
            "proposal_id": "proposal-bound-model-drafts",
            "payload": payload,
        }
        scenario_model_draft_service.materialize_draft_resources(
            self.db,
            self.scenario,
            proposal,
            source_thread_id="thread-bound-drafts",
            source_message_id="message-bound-drafts",
            compilation_job_id="job-bound-drafts",
            created_by_user_id=self.owner.id,
        )
        self.db.commit()

        response = self.client.get(
            f"/api/scenarios/{self.scenario.id}/model-drafts",
            params={
                "proposal_id": proposal["proposal_id"],
                "resource_kind": "mapping",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        mappings = response.json()["items"]
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0]["resource_key"], "mapping.project_register")
        self.assertEqual(mappings[0]["payload"]["table_name"], "projects")
        self.assertEqual(
            mappings[0]["payload"]["column_map"],
            {"项目编号": "project_code", "名称": "project_name"},
        )
        self.assertFalse(mappings[0]["enabled"])
        self.assertFalse(mappings[0]["publishable"])

        all_drafts = self.client.get(
            f"/api/scenarios/{self.scenario.id}/model-drafts",
            params={"proposal_id": proposal["proposal_id"]},
        )
        self.assertEqual(all_drafts.status_code, 200, all_drafts.text)
        self.assertIn(
            "instance",
            {item["resource_kind"] for item in all_drafts.json()["items"]},
        )
        for model in (
            OntologyInstance,
            DataMapping,
            OntologyRule,
            OntologyWorkflow,
            OntologySnapshot,
            OntologyRelease,
        ):
            self.assertEqual(
                self.db.scalar(select(func.count()).select_from(model)),
                0,
                model.__name__,
            )

    def test_edited_only_entity_is_preserved_as_gap_without_stale_formal_write(self) -> None:
        proposal, thread, message = self._stage_saved_proposal(
            self._normalized(),
            suffix="edited-zero-safe",
        )
        patched = self._patch_draft_by_key(
            proposal["proposal_id"],
            "entity.project",
            {"name": "用户修订项目"},
        )
        self.assertEqual(patched["revision"], 1)
        self.db.expire_all()

        applied = assistant.apply_proposal(
            AssistantProposalApplyRequest(
                kind="scenario_model",
                scenario_id=self.scenario.id,
                thread_id=thread.id,
                proposal_id=proposal["proposal_id"],
                task_id="ontology",
                allow_partial=True,
                confirm=True,
            ),
            self.db,
        )

        self.assertEqual(applied["data"]["task_status"], "drafted_with_gaps")
        self.assertEqual(applied["data"]["safe_change_count"], 0)
        self.assertTrue(applied["data"]["draft_preserved"])
        self.assertIn(
            "entity.project",
            applied["data"]["excluded_resource_keys"],
        )
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyEntity)),
            0,
        )
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyInstance)),
            0,
        )
        saved_message = self.db.get(AssistantMessage, message.id)
        self.assertNotEqual(
            saved_message.proposal["payload"]["current_task_id"],
            "ontology",
        )
        lifecycle = saved_message.proposal["payload"]
        self.assertTrue(
            lifecycle["current_task_id"]
            or lifecycle["execution_summary"]["final"],
            lifecycle,
        )
        if not lifecycle["current_task_id"]:
            self.assertEqual(
                lifecycle["execution_status"],
                "completed_with_gaps",
            )
            self.assertEqual(lifecycle["next_action"]["type"], "refine_model")
        edited_row = self.db.scalars(
            select(ScenarioModelDraftResource).where(
                ScenarioModelDraftResource.proposal_id == proposal["proposal_id"],
                ScenarioModelDraftResource.resource_key == "entity.project",
            )
        ).first()
        self.assertEqual(edited_row.draft_status, "needs_validation")
        self.assertEqual(edited_row.revision, 1)
        self.assertEqual(edited_row.payload["name"], "用户修订项目")

    def _assert_task_apply_rejects_staging_status(
        self,
        draft_status: str,
    ) -> None:
        proposal, thread, message = self._stage_saved_proposal(
            self._normalized(),
            suffix=f"staging-authority-{draft_status[:8]}",
        )
        ontology_rows = list(self.db.scalars(
            select(ScenarioModelDraftResource).where(
                ScenarioModelDraftResource.proposal_id == proposal["proposal_id"],
                ScenarioModelDraftResource.task_id == "ontology",
            )
        ).all())
        self.assertTrue(ontology_rows)
        for row in ontology_rows:
            row.draft_status = draft_status
            row.enabled = False
            row.publishable = False
        self.db.commit()

        applied = assistant.apply_proposal(
            AssistantProposalApplyRequest(
                kind="scenario_model",
                scenario_id=self.scenario.id,
                thread_id=thread.id,
                proposal_id=proposal["proposal_id"],
                task_id="ontology",
                allow_partial=True,
                confirm=True,
            ),
            self.db,
        )

        self.assertEqual(applied["data"]["task_status"], "drafted_with_gaps")
        self.assertEqual(applied["data"]["safe_change_count"], 0)
        self.assertEqual(
            applied["data"]["ineligible_draft_count"],
            len(ontology_rows),
        )
        self.assertIn(
            "STAGING_DRAFT_NOT_READY_FOR_APPLY",
            {
                issue["code"]
                for issue in applied["data"]["remaining_blockers"]
            },
        )
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyEntity)),
            0,
        )
        self.db.expire_all()
        stored_statuses = set(self.db.scalars(
            select(ScenarioModelDraftResource.draft_status).where(
                ScenarioModelDraftResource.proposal_id == proposal["proposal_id"],
                ScenarioModelDraftResource.task_id == "ontology",
            )
        ).all())
        self.assertEqual(stored_statuses, {draft_status})
        lifecycle = self.db.get(
            AssistantMessage,
            message.id,
        ).proposal["payload"]
        self.assertNotEqual(lifecycle["current_task_id"], "ontology")
        self.assertTrue(
            lifecycle["current_task_id"]
            or lifecycle["execution_summary"]["final"],
            lifecycle,
        )

    def test_task_apply_rejects_needs_attention_staging_rows(self) -> None:
        self._assert_task_apply_rejects_staging_status("needs_attention")

    def test_task_apply_rejects_deferred_staging_rows(self) -> None:
        self._assert_task_apply_rejects_staging_status("deferred")

    def test_task_apply_rejects_needs_validation_staging_rows(self) -> None:
        self._assert_task_apply_rejects_staging_status("needs_validation")

    def test_task_apply_rejects_resolved_staging_rows(self) -> None:
        self._assert_task_apply_rejects_staging_status("resolved")

    def test_task_apply_rejects_superseded_staging_rows(self) -> None:
        self._assert_task_apply_rejects_staging_status("superseded")

    def test_edited_property_uses_source_lineage_to_exclude_parent_entity(self) -> None:
        proposal, thread, _message = self._stage_saved_proposal(
            self._normalized(),
            suffix="edited-property-lineage",
        )
        listed = self.client.get(
            f"/api/scenarios/{self.scenario.id}/model-drafts",
            params={"proposal_id": proposal["proposal_id"]},
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        property_draft = next(
            item for item in listed.json()["items"]
            if item["resource_kind"] == "property"
            and item["payload"].get("name") == "名称"
        )
        self.assertEqual(
            property_draft["payload"]["entity_ref"],
            "entity.project",
        )
        patched = self.client.patch(
            (
                f"/api/scenarios/{self.scenario.id}/model-drafts/"
                f"{property_draft['id']}"
            ),
            json={
                "expected_revision": property_draft["revision"],
                # A working edit may legitimately omit immutable compiler
                # lineage; exclusion must consult source_payload instead.
                "payload": {
                    "name": "用户修订名称",
                    "data_type": "string",
                    "is_required": True,
                },
            },
        )
        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertNotIn("entity_ref", patched.json()["payload"])
        self.db.expire_all()

        applied = assistant.apply_proposal(
            AssistantProposalApplyRequest(
                kind="scenario_model",
                scenario_id=self.scenario.id,
                thread_id=thread.id,
                proposal_id=proposal["proposal_id"],
                task_id="ontology",
                allow_partial=True,
                confirm=True,
            ),
            self.db,
        )

        self.assertEqual(applied["data"]["task_status"], "drafted_with_gaps")
        self.assertIn(
            "entity.project",
            applied["data"]["excluded_resource_keys"],
        )
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyEntity)),
            0,
        )
        self.db.expire_all()
        saved_property = self.db.get(
            ScenarioModelDraftResource,
            property_draft["id"],
        )
        self.assertEqual(saved_property.draft_status, "needs_validation")
        self.assertEqual(saved_property.revision, 1)
        self.assertEqual(saved_property.payload["name"], "用户修订名称")
        self.assertEqual(
            saved_property.source_payload["entity_ref"],
            "entity.project",
        )

    def test_edited_entity_is_excluded_while_independent_entity_applies_partially(self) -> None:
        raw = self._raw()
        raw["entities"].append({
            "key": "entity.department",
            "name": "部门",
            "description": "项目归属部门",
            "properties": [{
                "name": "部门编号",
                "data_type": "string",
                "is_key": True,
                "is_title": True,
                "is_required": True,
            }],
            "evidence_refs": ["draft-acceptance:p0001"],
            "confidence": 1.0,
        })
        raw["relations"] = [{
            "key": "relation.department_projects",
            "name": "部门负责项目",
            "source_ref": "entity.department",
            "target_ref": "entity.project",
            "relation_type": "1:N",
            "evidence_refs": ["draft-acceptance:p0001"],
            "confidence": 1.0,
        }]
        raw["coverage"][0]["change_keys"].extend([
            "entity.department",
            "relation.department_projects",
        ])
        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
            mapping_catalog=[],
            columns_by_table={},
        )
        proposal, thread, _message = self._stage_saved_proposal(
            payload,
            suffix="edited-partial",
        )
        self._patch_draft_by_key(
            proposal["proposal_id"],
            "entity.project",
            {"name": "用户修订项目"},
        )
        self.db.expire_all()

        applied = assistant.apply_proposal(
            AssistantProposalApplyRequest(
                kind="scenario_model",
                scenario_id=self.scenario.id,
                thread_id=thread.id,
                proposal_id=proposal["proposal_id"],
                task_id="ontology",
                allow_partial=True,
                confirm=True,
            ),
            self.db,
        )

        self.assertEqual(applied["data"]["task_status"], "partially_applied")
        self.assertTrue(applied["data"]["partial"])
        self.assertTrue(applied["data"]["draft_preserved"])
        self.assertGreater(applied["data"]["safe_change_count"], 0)
        self.assertEqual(
            set(self.db.scalars(
                select(OntologyEntity.name).where(
                    OntologyEntity.scenario_id == self.scenario.id
                )
            ).all()),
            {"部门"},
        )
        project_row = self.db.scalars(
            select(ScenarioModelDraftResource).where(
                ScenarioModelDraftResource.proposal_id == proposal["proposal_id"],
                ScenarioModelDraftResource.resource_key == "entity.project",
            )
        ).first()
        department_row = self.db.scalars(
            select(ScenarioModelDraftResource).where(
                ScenarioModelDraftResource.proposal_id == proposal["proposal_id"],
                ScenarioModelDraftResource.resource_key == "entity.department",
            )
        ).first()
        dependent_relation_row = self.db.scalars(
            select(ScenarioModelDraftResource).where(
                ScenarioModelDraftResource.proposal_id == proposal["proposal_id"],
                ScenarioModelDraftResource.resource_key
                == "relation.department_projects",
            )
        ).first()
        self.assertEqual(project_row.draft_status, "needs_validation")
        self.assertEqual(project_row.revision, 1)
        self.assertEqual(project_row.payload["name"], "用户修订项目")
        self.assertEqual(department_row.draft_status, "applied")
        self.assertNotEqual(dependent_relation_row.draft_status, "applied")
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyRelation)),
            0,
        )
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyInstance)),
            0,
        )

    def test_resolve_requires_same_scene_kind_and_current_revision(self) -> None:
        proposal = self._proposal()
        scenario_model_draft_service.materialize_draft_resources(
            self.db,
            self.scenario,
            proposal,
            created_by_user_id=self.owner.id,
        )
        other_scenario = BusinessScenario(
            id="other-scenario-model-drafts",
            tenant_id=self.tenant.id,
            name="其他项目场景",
            namespace="other-project-risk",
            status="draft",
        )
        matching_entity = OntologyEntity(
            id="formal-project-entity",
            scenario_id=self.scenario.id,
            name="项目",
            namespace=self.scenario.namespace,
        )
        cross_scene_entity = OntologyEntity(
            id="foreign-scene-project-entity",
            scenario_id=other_scenario.id,
            name="项目",
            namespace=other_scenario.namespace,
        )
        wrong_kind_workflow = OntologyWorkflow(
            id="formal-project-workflow",
            scenario_id=self.scenario.id,
            name="项目流程",
            nodes=[],
            edges=[],
            status="draft",
            enabled=False,
        )
        self.db.add_all([
            other_scenario,
            matching_entity,
            cross_scene_entity,
            wrong_kind_workflow,
        ])
        self.db.commit()
        entity_draft = self.db.scalars(
            select(ScenarioModelDraftResource).where(
                ScenarioModelDraftResource.proposal_id == proposal["proposal_id"],
                ScenarioModelDraftResource.resource_key == "entity.project",
            )
        ).first()
        resolve_url = (
            f"/api/scenarios/{self.scenario.id}/model-drafts/"
            f"{entity_draft.id}/resolve"
        )

        stale = self.client.post(
            resolve_url,
            json={
                "expected_revision": entity_draft.revision + 1,
                "resolved_resource_id": matching_entity.id,
            },
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        wrong_kind = self.client.post(
            resolve_url,
            json={
                "expected_revision": entity_draft.revision,
                "resolved_resource_id": wrong_kind_workflow.id,
            },
        )
        self.assertEqual(wrong_kind.status_code, 409, wrong_kind.text)
        cross_scene = self.client.post(
            resolve_url,
            json={
                "expected_revision": entity_draft.revision,
                "resolved_resource_id": cross_scene_entity.id,
            },
        )
        self.assertEqual(cross_scene.status_code, 409, cross_scene.text)

        resolved = self.client.post(
            resolve_url,
            json={
                "expected_revision": entity_draft.revision,
                "resolved_resource_id": matching_entity.id,
            },
        )
        self.assertEqual(resolved.status_code, 200, resolved.text)
        self.assertEqual(resolved.json()["draft_status"], "resolved")
        self.assertEqual(resolved.json()["revision"], entity_draft.revision + 1)
        self.assertEqual(
            resolved.json()["resolved_resource_id"],
            matching_entity.id,
        )
        repeated_stale = self.client.post(
            resolve_url,
            json={
                "expected_revision": entity_draft.revision,
                "resolved_resource_id": matching_entity.id,
            },
        )
        self.assertEqual(repeated_stale.status_code, 409, repeated_stale.text)

        default_list = self.client.get(
            f"/api/scenarios/{self.scenario.id}/model-drafts",
            params={"proposal_id": proposal["proposal_id"]},
        )
        self.assertNotIn(
            entity_draft.id,
            {item["id"] for item in default_list.json()["items"]},
        )
        resolved_list = self.client.get(
            f"/api/scenarios/{self.scenario.id}/model-drafts",
            params={
                "proposal_id": proposal["proposal_id"],
                "include_resolved": True,
            },
        )
        resolved_item = next(
            item for item in resolved_list.json()["items"]
            if item["id"] == entity_draft.id
        )
        self.assertEqual(resolved_item["draft_status"], "resolved")

    def test_resolve_refreshes_source_plan_snapshot_and_canonical_job(self) -> None:
        source_bundle = scenario_model_compiler.build_source_bundle(
            "",
            [{
                "id": "resolve-plan-source",
                "filename": "resolve-plan.md",
                "text": "项目对象已经由用户在正式编辑器中建立。",
            }],
        )
        source_ref = "resolve-plan-source:p0001"
        raw = self._raw()
        raw["entities"] = [{
            "key": "entity.resolved_project",
            "name": "已人工建立项目",
            "description": "由正式编辑器人工完成的项目对象",
            "properties": [{
                "name": "项目编号",
                "data_type": "string",
                "is_key": True,
                "is_title": True,
                "is_required": True,
            }],
            "evidence_refs": [source_ref],
            "confidence": 1.0,
        }]
        for section in (
            "relations", "instances", "functions", "actions", "rules",
            "events", "workflows", "mappings", "relation_mappings",
            "conceptual_mappings", "unresolved",
        ):
            raw[section] = []
        raw["coverage"] = [{
            "source_ref": source_ref,
            "status": "modeled",
            "reason": "项目对象定义",
            "change_keys": ["entity.resolved_project"],
        }]
        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=source_bundle,
            mapping_catalog=[],
            columns_by_table={},
        )
        entity_candidate = next(
            item for item in payload["draft_candidates"]
            if item["resource_kind"] == "entity"
        )
        # Keep this acceptance fixture to one staging row. The compiler has
        # already validated the required key/title property above.
        entity_candidate["payload"]["properties"] = []
        payload["draft_candidates"] = [entity_candidate]
        payload["entities"][0]["properties"] = []
        # Force a staging-only validation blocker without adding a permanent
        # document ambiguity. Resolving the row must remove this synthetic gap.
        entity_candidate["validation_status"] = "needs_attention"

        thread = AssistantThread(
            id="thread-resolve-plan-refresh",
            tenant_id=self.tenant.id,
            created_by_user_id=self.owner.id,
            scenario_id=self.scenario.id,
            scope_key=(
                f"scenario:{self.scenario.id}|path:/scenarios/{self.scenario.id}"
            ),
            title="resolve plan refresh",
        )
        job = AssistantCompilationJob(
            id="job-resolve-plan-refresh",
            request_fingerprint="a" * 64,
            tenant_id=self.tenant.id,
            created_by_user_id=self.owner.id,
            scenario_id=self.scenario.id,
            thread_id=thread.id,
            message_id=None,
            message_hash="b" * 64,
            attachment_content_hash="c" * 64,
            llm_config_fingerprint="d" * 64,
            mapping_context_fingerprint="e" * 64,
            execution_policy_fingerprint="f" * 64,
            compiler_version=scenario_model_compiler.COMPILER_VERSION,
            scenario_baseline=assistant._scenario_revision(self.scenario),
            status="running",
            progress={},
            llm_call_budget=20,
            llm_calls_used=1,
        )
        self.db.add_all([thread, job])
        self.db.commit()
        with patch.object(assistant, "SessionLocal", self.Session):
            proposal = assistant._finalize_compilation_success(
                tenant_id=self.tenant.id,
                user_id=self.owner.id,
                job_id=job.id,
                thread_id=thread.id,
                assistant_message_id="message-resolve-plan-refresh",
                scenario_id=self.scenario.id,
                data=payload,
                reply="场景模型草稿等待人工修正。",
                context={"scenario_id": self.scenario.id},
                sources=[],
                thinking=[],
            )

        self.db.expire_all()
        draft = self.db.scalars(
            select(ScenarioModelDraftResource).where(
                ScenarioModelDraftResource.proposal_id == proposal["proposal_id"],
                ScenarioModelDraftResource.resource_kind == "entity",
            )
        ).one()
        source_message = self.db.get(
            AssistantMessage,
            "message-resolve-plan-refresh",
        )
        before_codes = {
            issue["code"]
            for task in source_message.proposal["payload"]["tasks"]
            for issue in task.get("issues", [])
        }
        self.assertIn("STAGED_RESOURCE_REQUIRES_VALIDATION", before_codes)
        before_revision = source_message.proposal["run_revision"]

        formal = OntologyEntity(
            id="formal-resolved-project",
            scenario_id=self.scenario.id,
            name="已人工建立项目",
            namespace=self.scenario.namespace,
        )
        self.db.add(formal)
        self.db.commit()
        expected_snapshot = assistant._scenario_snapshot(self.scenario)
        resolved = self.client.post(
            (
                f"/api/scenarios/{self.scenario.id}/model-drafts/"
                f"{draft.id}/resolve"
            ),
            json={
                "expected_revision": draft.revision,
                "resolved_resource_id": formal.id,
            },
        )
        self.assertEqual(resolved.status_code, 200, resolved.text)

        self.db.expire_all()
        source_message = self.db.get(
            AssistantMessage,
            "message-resolve-plan-refresh",
        )
        stored_job = self.db.get(AssistantCompilationJob, job.id)
        refreshed = source_message.proposal
        refreshed_payload = refreshed["payload"]
        summary = refreshed_payload["execution_summary"]
        refreshed_codes = {
            issue["code"]
            for task in refreshed_payload["tasks"]
            for issue in task.get("issues", [])
        }
        ontology_task = next(
            task for task in refreshed_payload["tasks"]
            if task["id"] == "ontology"
        )
        self.assertNotIn(
            "STAGED_RESOURCE_REQUIRES_VALIDATION",
            refreshed_codes,
        )
        self.assertEqual(summary["blocking_issue_count"], 0, summary)
        self.assertTrue(summary["final"] or refreshed_payload["current_task_id"])
        self.assertEqual(ontology_task["status"], "applied")
        self.assertTrue(ontology_task["apply_result"]["manual_resolution"])
        self.assertEqual(
            refreshed_payload["draft_materialization"]["resource_count"],
            0,
        )
        self.assertEqual(refreshed["base_snapshot"], expected_snapshot)
        self.assertGreater(refreshed["run_revision"], before_revision)
        self.assertEqual(
            source_message.context["run_revision"],
            refreshed["run_revision"],
        )
        self.assertEqual(stored_job.status, "succeeded")
        self.assertEqual(stored_job.result, refreshed)


if __name__ == "__main__":
    unittest.main()
