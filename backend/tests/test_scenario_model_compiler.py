from __future__ import annotations

import hashlib
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import (
    BusinessScenario,
    DataSource,
    FunctionDefinition,
    OntologyAction,
    OntologyEntity,
    OntologyEvent,
    OntologyProperty,
    OntologyRelation,
    OntologyRule,
    OntologyWorkflow,
    Tenant,
    User,
)
from app.routers import assistant
from app.services import permission_service, scenario_model_compiler
from app.services.policies import PolicyViolation
from tests.e2e_mock_llm import _Handler as E2EMockLLMHandler


def _schema(properties: dict | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": False,
    }


class ScenarioModelCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        tenant = Tenant(id="tenant-scenario-compiler", name="复合编译租户")
        user = User(
            id="user-scenario-compiler",
            tenant_id=tenant.id,
            email="compiler@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-compiler",
            tenant_id=tenant.id,
            name="建筑项目履约",
            namespace="construction",
            status="draft",
        )
        self.db.add_all([tenant, user, self.scenario])
        self.db.commit()
        permission_service.ensure_organization(self.db, tenant.id, owner_user_id=user.id)
        self.db.commit()
        self.db.info["tenant_id"] = tenant.id
        self.db.info["user_id"] = user.id

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _bundle(self) -> dict:
        return scenario_model_compiler.build_source_bundle(
            "请编译附件",
            [{
                "id": "construction-brief",
                "filename": "建筑项目实施文档.md",
                "text": "项目以项目编号唯一标识。项目可包含子项目。审批操作校验项目状态，审批后发布已审批事件并进入归档流程。",
            }],
        )

    def _raw(self) -> dict:
        ref = "construction-brief:p0001"
        return {
            "schema_version": "scenario_model.v1",
            "entities": [{
                "key": "entity.project",
                "name": "项目",
                "description": "建筑项目",
                "properties": [
                    {"name": "项目编号", "data_type": "string", "is_key": True, "is_title": True, "is_required": True},
                    {"name": "状态", "data_type": "string", "is_required": True, "is_enum": True, "enum_values": ["草稿", "已审批"]},
                ],
                "state_property": "状态",
                "evidence_refs": [ref],
                "confidence": 0.98,
            }],
            "relations": [{
                "key": "relation.project_children",
                "name": "项目包含子项目",
                "source_ref": "entity.project",
                "target_ref": "entity.project",
                "relation_type": "1:N",
                "evidence_refs": [ref],
                "confidence": 0.9,
            }],
            "functions": [{
                "key": "function.can_approve",
                "name": "判断项目是否可审批",
                "input_schema": _schema({"状态": {"type": "string"}}),
                "output_schema": _schema({"可审批": {"type": "boolean"}}),
                "evidence_refs": [ref],
                "confidence": 0.92,
            }],
            "actions": [{
                "key": "action.approve",
                "name": "审批项目",
                "entity_ref": "entity.project",
                "input_schema": _schema({"项目编号": {"type": "string"}}),
                "precondition": "项目处于草稿状态",
                "postcondition": "项目状态变为已审批",
                "evidence_refs": [ref],
                "confidence": 0.95,
            }],
            "rules": [{
                "key": "rule.draft_only",
                "name": "仅草稿可审批",
                "entity_ref": "entity.project",
                "condition": {"field": "状态", "op": "==", "value": "草稿"},
                "trigger_action_refs": ["action.approve"],
                "severity": "warning",
                "evidence_refs": [ref],
                "confidence": 0.95,
            }],
            "events": [{
                "key": "event.approved",
                "name": "项目已审批",
                "payload_schema": _schema({"项目编号": {"type": "string"}}),
                "trigger_source": "审批项目",
                "evidence_refs": [ref],
                "confidence": 0.95,
            }],
            "workflows": [{
                "key": "workflow.archive",
                "name": "项目审批归档",
                "trigger_type": "manual",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "开始"}},
                    {"id": "approve", "type": "action", "data": {"label": "审批", "resource_ref": "action.approve"}},
                    {"id": "end", "type": "end", "data": {"label": "结束"}},
                ],
                "edges": [
                    {"id": "e1", "source": "start", "target": "approve"},
                    {"id": "e2", "source": "approve", "target": "end"},
                ],
                "evidence_refs": [ref],
                "confidence": 0.9,
            }],
            "mappings": [],
            "relation_mappings": [],
            "unresolved": [],
            "coverage": [{
                "source_ref": ref,
                "status": "modeled",
                "reason": "对象、关系、审批规则、事件和归档流程均已建模",
                "change_keys": ["entity.project", "action.approve", "workflow.archive"],
            }],
        }

    def test_local_e2e_wage_contract_has_no_blocking_model_errors(self) -> None:
        bundle = scenario_model_compiler.build_source_bundle(
            "请编译附件",
            [{
                "id": "wage-domain-document",
                "filename": "建筑领域业务ontology设计.docx",
                "text": (
                    "建设项目、农民工、施工企业、工资台账、欠薪风险和欠薪预警。"
                    "工资逾期后生成预警并由监管人员复核。"
                ),
            }],
        )
        prompt = (
            "业务本体文档编译器\n待逐段编译的业务语义来源：\n"
            + json.dumps(
                [{"ref": bundle["paragraphs"][0]["ref"]}],
                ensure_ascii=False,
            )
        )
        raw = E2EMockLLMHandler._structured_model_response([{"content": prompt}])

        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=bundle,
            mapping_catalog=[],
            columns_by_table={},
        )

        self.assertEqual(len(payload["entities"]), 6)
        self.assertEqual(len(payload["relations"]), 5)
        self.assertEqual(len(payload["workflows"]), 1)
        self.assertFalse(
            [issue for issue in payload["unresolved"] if issue.get("blocking")]
        )

    def test_entity_constraints_and_many_to_one_are_normalized_without_cascade(self) -> None:
        raw = self._raw()
        raw["entities"][0]["properties"][0]["constraints"] = None
        raw["entities"][0]["properties"][1]["constraints"] = (
            "min_length:1,max_length:20"
        )
        raw["relations"][0]["relation_type"] = "N:1"

        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
            mapping_catalog=[],
            columns_by_table={},
        )

        codes = {item["code"] for item in payload["unresolved"]}
        self.assertNotIn("invalid_entity", codes)
        self.assertNotIn("invalid_property_constraints", codes)
        self.assertNotIn("missing_primary_key", codes)
        self.assertNotIn("invalid_relation_cardinality", codes)
        self.assertEqual(payload["relations"][0]["relation_type"], "N:1")
        properties = {item["name"]: item for item in payload["entities"][0]["properties"]}
        self.assertEqual(properties["项目编号"]["constraints"], {})
        self.assertEqual(
            properties["状态"]["constraints"],
            {"min_length": 1, "max_length": 20},
        )

    def test_unparseable_entity_constraint_is_one_explicit_blocker(self) -> None:
        raw = self._raw()
        raw["entities"][0]["properties"][0]["constraints"] = "按行业规则校验"

        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
            mapping_catalog=[],
            columns_by_table={},
        )

        codes = [item["code"] for item in payload["unresolved"]]
        self.assertEqual(codes.count("invalid_property_constraints"), 1)
        self.assertNotIn("invalid_entity", codes)
        self.assertNotIn("missing_primary_key", codes)
        self.assertEqual(
            payload["entities"][0]["properties"][0]["constraints"], {}
        )

    def test_fixed_value_property_constraint_becomes_runtime_const(self) -> None:
        raw = self._raw()
        raw["entities"][0]["properties"][1]["constraints"] = {
            "fixed_value": "草稿",
        }

        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
            mapping_catalog=[],
            columns_by_table={},
        )

        codes = {item["code"] for item in payload["unresolved"]}
        self.assertNotIn("invalid_property_constraints", codes)
        status = next(
            prop for prop in payload["entities"][0]["properties"]
            if prop["name"] == "状态"
        )
        self.assertEqual(status["constraints"], {"const": "草稿"})

    def test_relation_constraint_standard_spellings_are_canonicalized(self) -> None:
        raw = self._raw()
        raw["relations"][0].update({
            "relation_type": "N:M",
            "constraints": {
                "symmetric": "false",
                "transitive": 0,
                "irreflexive": "否",
                "source_min_cardinality": "0",
                "source_max_cardinality": "*",
                "target_min_cardinality": 1.0,
                "target_max_cardinality": "many",
            },
        })

        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
            mapping_catalog=[],
            columns_by_table={},
        )

        codes = {item["code"] for item in payload["unresolved"]}
        self.assertNotIn("invalid_relation_constraints", codes)
        self.assertEqual(
            payload["relations"][0]["constraints"],
            {"source_min_cardinality": 0, "target_min_cardinality": 1},
        )

    def test_relation_constraint_interval_shorthand_is_expanded(self) -> None:
        raw = self._raw()
        raw["relations"][0].update({
            "relation_type": "N:M",
            "constraints": {
                "source_max_cardinality": "1..*",
                "target_max_cardinality": "0..1",
            },
        })

        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
            mapping_catalog=[],
            columns_by_table={},
        )

        codes = {item["code"] for item in payload["unresolved"]}
        self.assertNotIn("invalid_relation_constraints", codes)
        self.assertEqual(
            payload["relations"][0]["constraints"],
            {
                "source_min_cardinality": 1,
                "target_min_cardinality": 0,
                "target_max_cardinality": 1,
            },
        )

    def test_ambiguous_relation_constraint_text_remains_blocking(self) -> None:
        raw = self._raw()
        raw["relations"][0]["constraints"] = {
            "source_max_cardinality": "按项目类型确定",
        }

        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
            mapping_catalog=[],
            columns_by_table={},
        )

        codes = [item["code"] for item in payload["unresolved"]]
        self.assertEqual(codes.count("invalid_relation_constraints"), 1)

    def test_workflow_node_declared_as_action_uses_unique_rule_reference(self) -> None:
        raw = self._raw()
        raw["workflows"][0]["nodes"] = [
            {"id": "start", "type": "start", "data": {"label": "开始"}},
            {
                "id": "check",
                "type": "action",
                "data": {
                    "label": "审批条件判断",
                    "resource_ref": "rule_draft_only",
                },
            },
            {"id": "approved", "type": "end", "data": {"label": "通过"}},
            {"id": "rejected", "type": "end", "data": {"label": "拒绝"}},
        ]
        raw["workflows"][0]["edges"] = [
            {"id": "e1", "source": "start", "target": "check"},
            {"id": "e2", "source": "check", "target": "approved", "label": "true"},
            {"id": "e3", "source": "check", "target": "rejected", "label": "false"},
        ]

        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
            mapping_catalog=[],
            columns_by_table={},
        )

        codes = {item["code"] for item in payload["unresolved"]}
        self.assertNotIn("missing_reference", codes)
        workflow = payload["workflows"][0]
        check = next(node for node in workflow["nodes"] if node["id"] == "check")
        self.assertEqual(check["type"], "rule")
        self.assertEqual(
            check["data"]["resource"],
            {"kind": "generated", "key": "rule.draft_only"},
        )

    def test_missing_data_source_defers_only_physical_mappings(self) -> None:
        raw = self._raw()
        raw["unresolved"] = [{
            "code": "data_source_not_configured",
            "message": "尚未配置可用数据源，物理字段映射延期",
            "source_refs": ["construction-brief:p0001"],
            "blocking": False,
        }]

        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
            mapping_catalog=[],
            columns_by_table={},
        )

        reported = [
            item for item in payload["unresolved"]
            if item["code"] == "document_reported_issue"
        ]
        self.assertEqual(len(reported), 1)
        self.assertEqual(
            reported[0]["reported_code"],
            "DATA_SOURCE_NOT_CONFIGURED",
        )
        self.assertFalse(reported[0]["blocking"])

    def test_model_cannot_mark_unknown_or_semantic_issues_nonblocking(self) -> None:
        for code in (
            "unresolved_reference",
            "source_conflict",
            "document_ambiguity",
            "unrecognized_requirement",
            "incomplete_constraint",
            "future_unknown_code",
        ):
            with self.subTest(code=code):
                raw = self._raw()
                raw["unresolved"] = [{
                    "code": code,
                    "message": "模型试图把真实建模问题标成非阻塞",
                    "source_refs": ["construction-brief:p0001"],
                    "blocking": False,
                }]

                payload = scenario_model_compiler.normalize_scenario_model(
                    self.db,
                    self.scenario,
                    raw,
                    source_bundle=self._bundle(),
                    mapping_catalog=[],
                    columns_by_table={},
                )

                reported = next(
                    item for item in payload["unresolved"]
                    if item["code"] == "document_reported_issue"
                )
                self.assertEqual(reported["reported_code"], code.upper())
                self.assertTrue(reported["blocking"])

    def test_closed_audit_codes_may_remain_nonblocking_when_requested(self) -> None:
        raw = self._raw()
        raw["unresolved"] = [
            {
                "code": "user_correction_applied",
                "message": "已按用户的明确修正替换旧定义",
                "source_refs": ["construction-brief:p0001"],
                "blocking": False,
            },
            {
                "code": "document_advisory",
                "message": "后续可以补充更多展示说明",
                "source_refs": ["construction-brief:p0001"],
                "blocking": False,
            },
        ]

        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
            mapping_catalog=[],
            columns_by_table={},
        )

        reported = [
            item for item in payload["unresolved"]
            if item["code"] == "document_reported_issue"
        ]
        self.assertEqual(
            {item["reported_code"] for item in reported},
            {"USER_CORRECTION_APPLIED", "DOCUMENT_ADVISORY"},
        )
        self.assertTrue(all(item["blocking"] is False for item in reported))

    def test_whitelisted_true_is_never_downgraded_and_mapping_needs_proof(self) -> None:
        cases = (
            ("MAPPING_DEFERRED_NO_DATA_SOURCE", True, [], True),
            ("USER_CORRECTION_APPLIED", True, [], True),
            (
                "DATA_SOURCE_UNAVAILABLE",
                False,
                [{
                    "data_source_id": "source-1",
                    "data_source_name": "已配置数据源",
                }],
                True,
            ),
        )
        for code, requested, catalog, expected in cases:
            with self.subTest(code=code, requested=requested, catalog=bool(catalog)):
                raw = self._raw()
                raw["unresolved"] = [{
                    "code": code,
                    "message": "严重性必须由平台治理",
                    "source_refs": ["construction-brief:p0001"],
                    "blocking": requested,
                }]

                payload = scenario_model_compiler.normalize_scenario_model(
                    self.db,
                    self.scenario,
                    raw,
                    source_bundle=self._bundle(),
                    mapping_catalog=catalog,
                    columns_by_table={},
                )

                reported = next(
                    item for item in payload["unresolved"]
                    if item["code"] == "document_reported_issue"
                )
                self.assertIs(reported["blocking"], expected)

    def test_final_severity_assertion_rejects_unregistered_platform_notice(self) -> None:
        unresolved = [{
            "code": "new_platform_notice_without_policy_review",
            "message": "新增的非阻塞语义必须先进入闭集",
            "source_refs": [],
            "blocking": False,
        }]

        with self.assertRaises(AssertionError):
            scenario_model_compiler._assert_unresolved_severity_policy(
                unresolved,
                mapping_is_cleanly_deferred=True,
            )

    def test_instances_and_unbound_logical_mappings_are_preserved_as_disabled_drafts(self) -> None:
        raw = self._raw()
        ref = "construction-brief:p0001"
        raw["instances"] = [{
            "key": "instance.project.p-001",
            "entity_ref": "entity.project",
            "display_name": "P-001",
            "values": {"项目编号": "P-001", "状态": "草稿"},
            "evidence_refs": [ref],
            "confidence": 0.96,
        }]
        raw["conceptual_mappings"] = [{
            "key": "conceptual_mapping.project_source",
            "mapping_kind": "object",
            "entity_ref": "entity.project",
            "source_label": "项目主数据表",
            "table_name": "project_master",
            "column_map": {"项目编号": "project_code", "状态": "status"},
            "binding_requirements": {"needs_data_source": True},
            "evidence_refs": [ref],
            "confidence": 0.9,
        }]
        raw["unresolved"] = [{
            "code": "MAPPING_DEFERRED_NO_DATA_SOURCE",
            "message": "已知逻辑字段对应关系，但数据源尚未连接",
            "source_refs": [ref],
            "blocking": False,
        }]

        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
            mapping_catalog=[],
            columns_by_table={},
        )

        drafts = {
            (item["resource_kind"], item["resource_key"]): item
            for item in payload["draft_candidates"]
        }
        instance = drafts[("instance", "instance.project.p-001")]
        logical_mapping = drafts[(
            "conceptual_mapping", "conceptual_mapping.project_source"
        )]
        self.assertTrue(instance["payload"])
        self.assertTrue(logical_mapping["payload"])
        self.assertFalse(instance["enabled"])
        self.assertFalse(instance["publishable"])
        self.assertEqual(instance["task_id"], "instances")
        self.assertEqual(instance["payload"]["values"]["项目编号"], "P-001")
        self.assertFalse(logical_mapping["enabled"])
        self.assertFalse(logical_mapping["publishable"])
        self.assertEqual(logical_mapping["validation_status"], "needs_binding")
        self.assertEqual(logical_mapping["task_id"], "mapping")
        self.assertIn(
            "MAPPING_DEFERRED_NO_DATA_SOURCE",
            {issue["code"] for issue in logical_mapping["validation_issues"]},
        )
        self.assertEqual(payload["mappings"], [])
        tasks = {
            item["id"]: item
            for item in scenario_model_compiler.build_model_task_plan(payload)
        }
        self.assertEqual(tasks["instances"]["sections"], ["instances"])
        self.assertEqual(tasks["instances"]["output_count"], 1)
        self.assertEqual(tasks["instances"]["draft_output_count"], 1)
        self.assertEqual(tasks["instances"]["draft_candidate_count"], 1)
        self.assertEqual(
            tasks["mapping"]["sections"],
            ["mappings", "relation_mappings", "conceptual_mappings"],
        )
        self.assertEqual(tasks["mapping"]["output_count"], 1)
        self.assertEqual(tasks["mapping"]["draft_output_count"], 1)
        self.assertEqual(tasks["mapping"]["draft_candidate_count"], 1)

    def test_conceptual_mapping_only_waits_for_one_data_source_gap_confirmation(self) -> None:
        raw = self._raw()
        for section in (
            "entities",
            "relations",
            "instances",
            "functions",
            "actions",
            "rules",
            "events",
            "workflows",
            "mappings",
            "relation_mappings",
        ):
            raw[section] = []
        ref = "construction-brief:p0001"
        raw["conceptual_mappings"] = [{
            "key": "conceptual_mapping.project_source",
            "mapping_kind": "object",
            "entity_ref": "entity.project",
            "source_label": "项目主数据表",
            "table_name": "project_master",
            "column_map": {"项目编号": "project_code"},
            "binding_requirements": {"needs_data_source": True},
            "evidence_refs": [ref],
            "confidence": 0.9,
        }]
        raw["unresolved"] = [{
            "code": "MAPPING_DEFERRED_NO_DATA_SOURCE",
            "message": "逻辑字段对应关系已知，但物理数据源尚未连接",
            "source_refs": [ref],
            "blocking": False,
        }]
        raw["coverage"] = [{
            "source_ref": ref,
            "status": "context",
            "reason": "已形成逻辑映射草稿",
            "change_keys": [],
        }]

        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
            mapping_catalog=[],
            columns_by_table={},
        )
        proposal = assistant._build_proposal(
            "scenario_model",
            payload,
            self.scenario,
        )

        self.assertNotIn(
            "no_applicable_changes",
            {item["code"] for item in payload["unresolved"]},
        )
        self.assertEqual(len(payload["draft_candidates"]), 1)
        self.assertEqual(
            payload["draft_candidates"][0]["resource_kind"],
            "conceptual_mapping",
        )
        tasks = {
            item["id"]: item for item in proposal["payload"]["tasks"]
        }
        self.assertEqual(tasks["mapping"]["status"], "ready")
        self.assertEqual(tasks["mapping"]["output_count"], 1)
        self.assertEqual(tasks["mapping"]["draft_output_count"], 1)
        summary = proposal["payload"]["execution_summary"]
        self.assertEqual(proposal["status"], "in_progress")
        self.assertTrue(proposal["requires_confirmation"])
        self.assertFalse(summary["final"])
        self.assertEqual(summary["status"], "waiting_for_confirmation")
        self.assertEqual(summary["current_task_id"], "mapping")
        self.assertEqual(summary["remaining_issue_group_count"], 1)
        self.assertEqual(len(summary["issue_groups"]), 1)
        self.assertEqual(
            summary["issue_groups"][0]["cause"],
            "data_source_dependency",
        )
        self.assertEqual(summary["issue_groups"][0]["count"], 1)

    def test_invalid_formal_candidate_is_not_lost_from_scene_draft_sidecar(self) -> None:
        raw = self._raw()
        raw["entities"][0]["name"] = ""

        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
            mapping_catalog=[],
            columns_by_table={},
        )

        self.assertEqual(payload["entities"], [])
        candidate = next(
            item for item in payload["draft_candidates"]
            if item["resource_kind"] == "entity"
            and item["resource_key"] == "entity.project"
        )
        self.assertEqual(candidate["payload"]["properties"][0]["name"], "项目编号")
        self.assertEqual(candidate["validation_status"], "blocked")
        self.assertIn(
            "missing_name",
            {issue["code"] for issue in candidate["validation_issues"]},
        )

    def test_draft_sidecar_matches_canonicalized_resource_key_without_duplicate(self) -> None:
        raw = self._raw()
        raw["entities"][0]["key"] = "project"

        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
            mapping_catalog=[],
            columns_by_table={},
        )

        candidates = [
            item for item in payload["draft_candidates"]
            if item["resource_kind"] == "entity"
            and item["resource_key"] == "entity.project"
        ]
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0]["formal_candidate"])
        self.assertNotIn(
            "draft_not_formally_validated",
            {issue["code"] for issue in candidates[0]["validation_issues"]},
        )

    def test_entity_json_aliases_and_enum_values_are_canonicalized(self) -> None:
        raw = self._raw()
        raw["entities"][0]["properties"][1].update({
            "is_enum": False,
            "enum_values": ["草稿", "已审批"],
        })
        raw["entities"][0]["properties"].extend([
            {
                "name": "扩展对象",
                "data_type": "object",
                "is_required": False,
                "constraints": None,
            },
            {
                "name": "扩展列表",
                "data_type": "array",
                "is_required": False,
                "constraints": None,
            },
        ])

        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
            mapping_catalog=[],
            columns_by_table={},
        )

        codes = {item["code"] for item in payload["unresolved"]}
        self.assertNotIn("invalid_entity", codes)
        self.assertNotIn("invalid_combined_entity", codes)
        properties = {
            item["name"]: item for item in payload["entities"][0]["properties"]
        }
        self.assertTrue(properties["状态"]["is_enum"])
        self.assertEqual(properties["扩展对象"]["data_type"], "json")
        self.assertEqual(properties["扩展列表"]["data_type"], "json")

    def test_attachment_and_structured_user_description_are_both_sources(self) -> None:
        document = {
            "id": "baseline-brief",
            "filename": "基线说明.md",
            "text": "审批阈值为一百万元。",
        }
        message = (
            "请编译完整业务模型并逐项保留来源；存在阻塞项时零写入。\n\n"
            "业务描述：用户明确修正：审批阈值改为二百万元，以本次描述为准。"
        )

        source_bundle = scenario_model_compiler.build_source_bundle(
            message,
            [document],
        )
        repeated = scenario_model_compiler.build_source_bundle(message, [document])

        self.assertEqual(source_bundle, repeated)
        self.assertEqual(
            [item["source_id"] for item in source_bundle["documents"]],
            ["baseline-brief", "request"],
        )
        self.assertEqual(
            [item["ref"] for item in source_bundle["paragraphs"]],
            ["baseline-brief:p0001", "request:p0001"],
        )
        request_manifest = source_bundle["documents"][1]
        self.assertEqual(request_manifest["source_kind"], "user_request")
        self.assertEqual(
            request_manifest["semantic_role"],
            "supplement_or_correction",
        )
        self.assertEqual(
            source_bundle["paragraphs"][1]["text"],
            "业务描述：用户明确修正：审批阈值改为二百万元，以本次描述为准。",
        )
        prompt = scenario_model_compiler._compiler_prompt(
            self.scenario,
            message=message,
            paragraphs=source_bundle["paragraphs"],
            mapping_catalog=[],
        )
        self.assertNotIn("请编译完整业务模型并逐项保留来源", prompt)
        self.assertIn("code=USER_CORRECTION_APPLIED", prompt)
        self.assertIn("code=SOURCE_CONFLICT", prompt)
        self.assertIn("request:p0001", prompt)

    def test_structured_empty_description_and_quick_command_are_not_sources(self) -> None:
        document = {
            "id": "only-document",
            "filename": "业务说明.md",
            "text": "订单号唯一标识订单。",
        }
        messages = [
            "请逐段编译我上传的业务文档，生成完整业务模型，并列出所有未识别、歧义和冲突项。",
            (
                "请根据业务文档编译完整业务模型。\n\n"
                "业务描述：暂无，请结合我上传的文档提取业务目标和边界。"
            ),
            "请编译附件。补充描述：无。",
            "继续吧",
            "下一步",
            "接着处理",
            "把剩下的做完",
        ]

        for message in messages:
            with self.subTest(message=message):
                source_bundle = scenario_model_compiler.build_source_bundle(
                    message,
                    [document],
                )
                self.assertEqual(
                    [item["source_id"] for item in source_bundle["documents"]],
                    ["only-document"],
                )
                self.assertEqual(
                    [item["ref"] for item in source_bundle["paragraphs"]],
                    ["only-document:p0001"],
                )

        working_only = scenario_model_compiler.build_source_bundle(
            "继续吧",
            [],
            has_working_drafts=True,
        )
        self.assertEqual(working_only["documents"], [])
        self.assertEqual(working_only["paragraphs"], [])

    def test_free_text_correction_with_attachment_is_retained_conservatively(self) -> None:
        message = "请根据附件编译，并将状态名称修正为履约状态。"
        source_bundle = scenario_model_compiler.build_source_bundle(
            message,
            [{
                "id": "baseline",
                "filename": "baseline.md",
                "text": "对象具有状态。",
            }],
        )

        request = source_bundle["paragraphs"][-1]
        self.assertEqual(request["ref"], "request:p0001")
        self.assertEqual(request["source_kind"], "user_request")
        self.assertEqual(request["text"], message)

    def test_description_only_uses_stable_request_source(self) -> None:
        message = "订单以订单号唯一标识，并在付款后发布已付款事件。"
        source_bundle = scenario_model_compiler.build_source_bundle(message, [])

        self.assertEqual(
            source_bundle["documents"],
            [{
                "source_id": "request",
                "filename": "用户补充描述与修正建议",
                "source_kind": "user_request",
                "semantic_role": "supplement_or_correction",
                "sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
                "characters": len(message),
                "paragraph_count": 1,
            }],
        )
        self.assertEqual(source_bundle["paragraphs"][0]["ref"], "request:p0001")

    def test_compile_uses_dedicated_configured_llm_timeout(self) -> None:
        document = {
            "id": "construction-brief",
            "filename": "建筑项目实施文档.md",
            "text": "项目以项目编号唯一标识。项目可包含子项目。审批操作校验项目状态，审批后发布已审批事件并进入归档流程。",
        }
        with (
            patch.object(
                scenario_model_compiler,
                "get_settings",
                return_value=SimpleNamespace(scenario_model_llm_timeout=480.0),
            ),
            patch.object(
                scenario_model_compiler.llm_service,
                "chat",
                return_value={"content": json.dumps(self._raw(), ensure_ascii=False)},
            ) as chat,
        ):
            payload = scenario_model_compiler.compile_scenario_model(
                self.db,
                self.scenario,
                message="请编译附件",
                documents=[document],
                llm=object(),
            )

        self.assertEqual(payload["schema_version"], "scenario_model.v1")
        self.assertEqual(chat.call_args.kwargs["request_timeout"], 480.0)
        self.assertEqual(chat.call_args.kwargs["max_retries"], 0)

    def test_timeout_fallback_merges_chunks_and_keeps_full_provenance(self) -> None:
        document = {
            "id": "chunked-brief",
            "filename": "分块建筑项目文档.md",
            "text": "项目以项目编号唯一标识。\n\n项目具有状态，审批操作会改变项目状态。",
        }
        source_bundle = scenario_model_compiler.build_source_bundle(
            "请编译附件", [document]
        )
        first_ref, second_ref = [
            item["ref"] for item in source_bundle["paragraphs"]
        ]

        def empty_raw() -> dict:
            return {
                "schema_version": "scenario_model.v1",
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
            }

        first_chunk = empty_raw()
        first_chunk["entities"] = [{
            "key": "entity.project",
            "name": "项目",
            "description": "建筑项目",
            "is_abstract": False,
            "properties": [{
                "name": "项目编号",
                "data_type": "string",
                "is_key": True,
                "is_title": True,
                "is_required": True,
            }],
            "evidence_refs": [first_ref],
            "confidence": 0.96,
        }]
        first_chunk["coverage"] = [{
            "source_ref": first_ref,
            "status": "modeled",
            "reason": "已建模项目及唯一编号",
            "change_keys": ["entity.project"],
        }]

        second_chunk = empty_raw()
        second_chunk["entities"] = [{
            # The second chunk chose a different key for the same named entity.
            # Merge must alias this key instead of leaving duplicate resources.
            "key": "entity.project.alias",
            "name": "项目",
            "state_property": "状态",
            "properties": [{
                "name": "状态",
                "data_type": "string",
                "is_required": True,
                "is_enum": True,
                "enum_values": ["待审批", "已审批"],
            }],
            "evidence_refs": [second_ref],
            "confidence": 0.91,
        }]
        second_chunk["actions"] = [{
            "key": "action.approve_project",
            "name": "审批项目",
            "entity_ref": "entity.project.alias",
            "input_schema": _schema({"项目编号": {"type": "string"}}),
            "precondition": "项目待审批",
            "postcondition": "项目状态已更新",
            "evidence_refs": [second_ref],
            "confidence": 0.93,
        }]
        second_chunk["coverage"] = [{
            "source_ref": second_ref,
            "status": "modeled",
            "reason": "已建模状态和审批操作",
            "change_keys": ["entity.project.alias", "action.approve_project"],
        }]

        call_number = 0

        def fake_chat(*args, **kwargs):  # noqa: ANN002, ANN003
            nonlocal call_number
            call_number += 1
            if call_number == 1:
                raise TimeoutError("整文 provider 超时")
            raw = first_chunk if call_number == 2 else second_chunk
            return {"content": json.dumps(raw, ensure_ascii=False)}

        def two_chunks(paragraphs):
            return [[paragraphs[0]], [paragraphs[1]]]

        with (
            patch.object(
                scenario_model_compiler,
                "get_settings",
                return_value=SimpleNamespace(scenario_model_llm_timeout=120.0),
            ),
            patch.object(scenario_model_compiler, "_source_chunks", side_effect=two_chunks),
            patch.object(scenario_model_compiler.llm_service, "chat", side_effect=fake_chat) as chat,
        ):
            payload = scenario_model_compiler.compile_scenario_model(
                self.db,
                self.scenario,
                message="请编译附件",
                documents=[document],
                llm=object(),
            )

        self.assertEqual(chat.call_count, 3)
        self.assertEqual(
            chat.call_args_list[0].kwargs["max_tokens"],
            scenario_model_compiler.MAX_OUTPUT_TOKENS,
        )
        self.assertTrue(all(
            call.kwargs["max_tokens"]
            == scenario_model_compiler.FALLBACK_MAX_OUTPUT_TOKENS
            for call in chat.call_args_list[1:]
        ))
        self.assertTrue(all(
            call.kwargs["max_retries"] == 0
            for call in chat.call_args_list
        ))
        self.assertEqual(len(payload["entities"]), 1)
        self.assertEqual(payload["entities"][0]["key"], "entity.project")
        self.assertEqual(
            {item["name"] for item in payload["entities"][0]["properties"]},
            {"项目编号", "状态"},
        )
        self.assertEqual(
            payload["actions"][0]["entity"],
            {"kind": "generated", "key": "entity.project"},
        )
        self.assertEqual(payload["source_refs"], sorted([first_ref, second_ref]))
        self.assertEqual(payload["coverage_summary"]["total"], 2)
        self.assertEqual(payload["coverage_summary"]["modeled"], 2)
        self.assertNotIn(
            "duplicate_change_key",
            {item["code"] for item in payload["unresolved"]},
        )
        self.assertNotIn(
            "missing_source_coverage",
            {item["code"] for item in payload["unresolved"]},
        )
        self.assertFalse(payload["unresolved"])
        # Compilation and fallback normalization remain proposal-only.
        self.assertEqual(self.db.scalar(select(func.count()).select_from(OntologyEntity)), 0)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(OntologyAction)), 0)

    def test_chunk_model_missing_schema_version_is_canonicalized(self) -> None:
        raw = self._raw()
        raw.pop("schema_version")
        source_ref = "construction-brief:p0001"
        with (
            patch.object(
                scenario_model_compiler,
                "get_settings",
                return_value=SimpleNamespace(scenario_model_llm_timeout=120.0),
            ),
            patch.object(
                scenario_model_compiler.llm_service,
                "chat",
                return_value={"content": json.dumps(raw, ensure_ascii=False)},
            ) as chat,
        ):
            payload = scenario_model_compiler._chat_raw_model(
                self.db,
                object(),
                "分块编译提示",
                max_tokens=scenario_model_compiler.FALLBACK_MAX_OUTPUT_TOKENS,
                allowed_refs={source_ref},
            )

        self.assertEqual(payload["schema_version"], "scenario_model.v1")
        self.assertEqual(chat.call_count, 1)

    def test_chunk_model_explicit_version_is_canonicalized(self) -> None:
        for supplied_version in ("scenario_model.v2", None):
            with self.subTest(supplied_version=supplied_version):
                raw = self._raw()
                raw["schema_version"] = supplied_version
                with (
                    patch.object(
                        scenario_model_compiler,
                        "get_settings",
                        return_value=SimpleNamespace(scenario_model_llm_timeout=120.0),
                    ),
                    patch.object(
                        scenario_model_compiler.llm_service,
                        "chat",
                        return_value={"content": json.dumps(raw, ensure_ascii=False)},
                    ) as chat,
                ):
                    payload = scenario_model_compiler._chat_raw_model(
                        self.db,
                        object(),
                        "分块编译提示",
                        max_tokens=scenario_model_compiler.FALLBACK_MAX_OUTPUT_TOKENS,
                        allowed_refs={"construction-brief:p0001"},
                        attempts=1,
                    )

                self.assertEqual(payload["schema_version"], "scenario_model.v1")
                self.assertEqual(chat.call_count, 1)

    def test_full_model_unsupported_schema_version_remains_strict(self) -> None:
        raw = self._raw()
        raw["schema_version"] = "scenario_model.v2"
        with self.assertRaisesRegex(ValueError, "缺少受支持的 schema_version"):
            scenario_model_compiler._validate_raw_contract(raw)

        with (
            patch.object(
                scenario_model_compiler,
                "get_settings",
                return_value=SimpleNamespace(scenario_model_llm_timeout=120.0),
            ),
            patch.object(
                scenario_model_compiler.llm_service,
                "chat",
                return_value={"content": json.dumps(raw, ensure_ascii=False)},
            ) as chat,
        ):
            with self.assertRaisesRegex(ValueError, "缺少受支持的 schema_version"):
                scenario_model_compiler._chat_raw_model(
                    self.db,
                    object(),
                    "完整文档编译提示",
                    max_tokens=scenario_model_compiler.FALLBACK_MAX_OUTPUT_TOKENS,
                    attempts=1,
                )

        self.assertEqual(chat.call_count, 1)

    def test_chunk_schema_default_does_not_bypass_provenance_scope(self) -> None:
        raw = self._raw()
        raw.pop("schema_version")
        raw["entities"][0]["evidence_refs"] = ["foreign:p0001"]
        with (
            patch.object(
                scenario_model_compiler,
                "get_settings",
                return_value=SimpleNamespace(scenario_model_llm_timeout=120.0),
            ),
            patch.object(
                scenario_model_compiler.llm_service,
                "chat",
                return_value={"content": json.dumps(raw, ensure_ascii=False)},
            ) as chat,
        ):
            with self.assertRaisesRegex(ValueError, "当前分块之外的来源段落"):
                scenario_model_compiler._chat_raw_model(
                    self.db,
                    object(),
                    "分块编译提示",
                    max_tokens=scenario_model_compiler.FALLBACK_MAX_OUTPUT_TOKENS,
                    allowed_refs={"construction-brief:p0001"},
                    attempts=1,
                )

        self.assertEqual(chat.call_count, 1)

    def test_large_json_cut_near_tail_bisects_instead_of_retrying_same_prompt(self) -> None:
        malformed = (
            '{"schema_version":"scenario_model.v1","entities":['
            + (" " * 9_000)
            + '{"key":"entity.project"'
        )
        with (
            patch.object(
                scenario_model_compiler,
                "get_settings",
                return_value=SimpleNamespace(scenario_model_llm_timeout=120.0),
            ),
            patch.object(
                scenario_model_compiler.llm_service,
                "chat",
                return_value={"content": malformed, "raw": {"choices": [{"finish_reason": "stop"}]}},
            ) as chat,
        ):
            with self.assertRaises(scenario_model_compiler._CompilerOutputTruncated):
                scenario_model_compiler._chat_raw_model(
                    self.db,
                    object(),
                    "分块编译提示",
                    max_tokens=scenario_model_compiler.FALLBACK_MAX_OUTPUT_TOKENS,
                    allowed_refs={"construction-brief:p0001"},
                )

        self.assertEqual(chat.call_count, 1)

    def test_malformed_multi_paragraph_chunk_bisects_without_identical_retries(self) -> None:
        with (
            patch.object(
                scenario_model_compiler,
                "get_settings",
                return_value=SimpleNamespace(scenario_model_llm_timeout=120.0),
            ),
            patch.object(
                scenario_model_compiler.llm_service,
                "chat",
                return_value={"content": "{not-valid-json"},
            ) as chat,
        ):
            with self.assertRaises(scenario_model_compiler._CompilerOutputTruncated):
                scenario_model_compiler._chat_raw_model(
                    self.db,
                    object(),
                    "多段分块编译提示",
                    max_tokens=scenario_model_compiler.FALLBACK_MAX_OUTPUT_TOKENS,
                    allowed_refs={"construction-brief:p0001", "construction-brief:p0002"},
                )

        self.assertEqual(chat.call_count, 1)

    def test_transient_provider_connection_is_retried_within_chunk_budget(self) -> None:
        connection_error = type("APIConnectionError", (RuntimeError,), {})
        valid = self._raw()
        with (
            patch.object(
                scenario_model_compiler,
                "get_settings",
                return_value=SimpleNamespace(scenario_model_llm_timeout=120.0),
            ),
            patch.object(
                scenario_model_compiler.llm_service,
                "chat",
                side_effect=[
                    connection_error("Connection error."),
                    {"content": json.dumps(valid, ensure_ascii=False)},
                ],
            ) as chat,
        ):
            payload = scenario_model_compiler._chat_raw_model(
                self.db,
                object(),
                "单段分块编译提示",
                max_tokens=scenario_model_compiler.FALLBACK_MAX_OUTPUT_TOKENS,
                allowed_refs={"construction-brief:p0001"},
            )

        self.assertEqual(payload["schema_version"], "scenario_model.v1")
        self.assertEqual(chat.call_count, 2)

    def test_length_truncated_full_response_immediately_uses_chunk_fallback(self) -> None:
        document = {
            "id": "construction-brief",
            "filename": "建筑项目实施文档.md",
            "text": (
                "项目以项目编号唯一标识。项目可包含子项目。"
                "审批操作校验项目状态，审批后发布已审批事件并进入归档流程。"
                "\n\n本段仅补充文档背景。"
            ),
        }
        second_ref = "construction-brief:p0002"
        context_chunk = {
            "schema_version": "scenario_model.v1",
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
            "coverage": [{
                "source_ref": second_ref,
                "status": "context",
                "reason": "仅为文档背景",
                "change_keys": [],
            }],
        }
        call_number = 0

        def fake_chat(*args, **kwargs):  # noqa: ANN002, ANN003
            nonlocal call_number
            call_number += 1
            if call_number == 1:
                return {
                    # Even parseable content must not be accepted when the
                    # provider explicitly says generation hit its token cap.
                    "content": json.dumps(self._raw(), ensure_ascii=False),
                    "raw": SimpleNamespace(
                        choices=[SimpleNamespace(finish_reason="length")]
                    ),
                }
            raw = self._raw() if call_number == 2 else context_chunk
            return {"content": json.dumps(raw, ensure_ascii=False)}

        def two_chunks(paragraphs):
            return [[paragraphs[0]], [paragraphs[1]]]

        self.assertEqual(
            scenario_model_compiler._response_finish_reason({
                "raw": {"choices": [{"finish_reason": "length"}]},
            }),
            "length",
        )
        with (
            patch.object(
                scenario_model_compiler,
                "get_settings",
                return_value=SimpleNamespace(scenario_model_llm_timeout=120.0),
            ),
            patch.object(scenario_model_compiler, "_source_chunks", side_effect=two_chunks),
            patch.object(scenario_model_compiler.llm_service, "chat", side_effect=fake_chat) as chat,
        ):
            payload = scenario_model_compiler.compile_scenario_model(
                self.db,
                self.scenario,
                message="请编译附件",
                documents=[document],
                llm=object(),
            )

        # One full call followed by exactly two chunk calls: no second
        # whole-document retry is allowed after finish_reason=length.
        self.assertEqual(chat.call_count, 3)
        self.assertEqual(
            chat.call_args_list[0].kwargs["max_tokens"],
            scenario_model_compiler.MAX_OUTPUT_TOKENS,
        )
        self.assertEqual(
            [call.kwargs["max_tokens"] for call in chat.call_args_list[1:]],
            [scenario_model_compiler.FALLBACK_MAX_OUTPUT_TOKENS] * 2,
        )
        self.assertEqual(payload["coverage_summary"]["total"], 2)
        self.assertEqual(payload["coverage_summary"]["modeled"], 1)
        self.assertEqual(payload["coverage_summary"]["context"], 1)
        self.assertFalse(payload["unresolved"])
        self.assertEqual(self.db.scalar(select(func.count()).select_from(OntologyEntity)), 0)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(OntologyAction)), 0)

    def test_truncated_chunk_bisects_only_that_branch_and_preserves_refs(self) -> None:
        document = {
            "id": "construction-brief",
            "filename": "建筑项目实施文档.md",
            "text": (
                "项目以项目编号唯一标识。项目可包含子项目。"
                "审批操作校验项目状态，审批后发布已审批事件并进入归档流程。"
                "\n\n第二段是业务背景。"
                "\n\n第三段也是业务背景。"
            ),
        }
        refs = [f"construction-brief:p{index:04d}" for index in range(1, 4)]

        def context_raw(source_ref: str) -> dict:
            return {
                "schema_version": "scenario_model.v1",
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
                "coverage": [{
                    "source_ref": source_ref,
                    "status": "context",
                    "reason": "背景段落",
                    "change_keys": [],
                }],
            }

        call_number = 0

        def fake_chat(*args, **kwargs):  # noqa: ANN002, ANN003
            nonlocal call_number
            call_number += 1
            prompt = args[1][1]["content"]
            prompt_refs = [source_ref for source_ref in refs if source_ref in prompt]
            if call_number == 1:
                return {
                    "content": "{",
                    "raw": {"choices": [{"finish_reason": "length"}]},
                }
            if prompt_refs == [refs[0]]:
                return {"content": json.dumps(self._raw(), ensure_ascii=False)}
            if prompt_refs == refs[1:]:
                return {
                    "content": "{",
                    "raw": {"choices": [{"finish_reason": "length"}]},
                }
            return {"content": json.dumps(context_raw(prompt_refs[0]), ensure_ascii=False)}

        def initial_chunks(paragraphs):
            return [[paragraphs[0]], paragraphs[1:]]

        with (
            patch.object(
                scenario_model_compiler,
                "get_settings",
                return_value=SimpleNamespace(scenario_model_llm_timeout=120.0),
            ),
            patch.object(scenario_model_compiler, "_source_chunks", side_effect=initial_chunks),
            patch.object(scenario_model_compiler.llm_service, "chat", side_effect=fake_chat) as chat,
        ):
            payload = scenario_model_compiler.compile_scenario_model(
                self.db,
                self.scenario,
                message="请编译附件",
                documents=[document],
                llm=object(),
            )

        fallback_ref_sets = [
            [source_ref for source_ref in refs if source_ref in call.args[1][1]["content"]]
            for call in chat.call_args_list[1:]
        ]
        self.assertEqual(chat.call_count, 5)
        self.assertEqual(
            fallback_ref_sets,
            [[refs[0]], refs[1:], [refs[1]], [refs[2]]],
        )
        self.assertEqual(
            [call.kwargs["max_tokens"] for call in chat.call_args_list],
            [
                scenario_model_compiler.MAX_OUTPUT_TOKENS,
                *([scenario_model_compiler.FALLBACK_MAX_OUTPUT_TOKENS] * 4),
            ],
        )
        self.assertEqual(payload["coverage_summary"]["total"], 3)
        self.assertEqual(payload["coverage_summary"]["modeled"], 1)
        self.assertEqual(payload["coverage_summary"]["context"], 2)
        self.assertFalse(payload["unresolved"])
        self.assertEqual(self.db.scalar(select(func.count()).select_from(OntologyEntity)), 0)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(OntologyAction)), 0)

    def test_single_paragraph_chunk_truncation_creates_editable_stage_placeholders(self) -> None:
        document = {
            "id": "single-source",
            "filename": "单段文档.md",
            "text": "项目以项目编号唯一标识。",
        }
        truncated = {
            "content": "{",
            "raw": {"choices": [{"finish_reason": "length"}]},
        }
        with (
            patch.object(
                scenario_model_compiler,
                "get_settings",
                return_value=SimpleNamespace(scenario_model_llm_timeout=120.0),
            ),
            patch.object(
                scenario_model_compiler.llm_service,
                "chat",
                side_effect=[truncated, truncated],
            ) as chat,
        ):
            payload = scenario_model_compiler.compile_scenario_model(
                self.db,
                self.scenario,
                message="请编译附件",
                documents=[document],
                llm=object(),
            )
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(
            [call.kwargs["max_tokens"] for call in chat.call_args_list],
            [
                scenario_model_compiler.MAX_OUTPUT_TOKENS,
                scenario_model_compiler.FALLBACK_MAX_OUTPUT_TOKENS,
            ],
        )
        self.assertEqual(payload["changes"], [])
        self.assertEqual(len(payload["draft_candidates"]), 6)
        self.assertEqual(
            {item["task_id"] for item in payload["draft_candidates"]},
            {
                "ontology",
                "instances",
                "mapping",
                "capabilities",
                "rules",
                "workflows",
            },
        )
        self.assertTrue(all(not item["publishable"] for item in payload["draft_candidates"]))
        self.assertEqual(self.db.scalar(select(func.count()).select_from(OntologyEntity)), 0)

    def test_mapping_catalog_does_not_silently_drop_the_fifty_first_source(self) -> None:
        sources = [
            DataSource(
                id=f"source-catalog-{index:02d}",
                tenant_id="tenant-scenario-compiler",
                scenario_id=self.scenario.id,
                name=f"Catalog source {index:02d}",
                type="sqlite",
                config={"path": f"catalog-{index:02d}.sqlite3"},
                status="ok",
            )
            for index in range(1, 52)
        ]
        self.db.add_all(sources)
        self.db.commit()

        def one_table(source):
            return [{
                "name": f"table_{source.id[-2:]}",
                "columns": [{"name": "id", "type": "text", "pk": True}],
            }]

        with patch.object(
            scenario_model_compiler.datasource_service,
            "list_tables",
            side_effect=one_table,
        ) as list_tables:
            context = scenario_model_compiler.prepare_compilation_context(
                self.db,
                self.scenario,
            )
        self.assertEqual(list_tables.call_count, 51)
        self.assertEqual(len(context["mapping_catalog"]), 51)
        self.assertIn(
            "source-catalog-51",
            {item["data_source_id"] for item in context["mapping_catalog"]},
        )

    def test_chunk_timeout_becomes_source_bound_placeholders_without_retry_loop(self) -> None:
        document = {
            "id": "timeout-source",
            "filename": "超时文档.md",
            "text": "项目以项目编号唯一标识。",
        }
        truncated = {
            "content": "{",
            "raw": {"choices": [{"finish_reason": "length"}]},
        }
        with (
            patch.object(
                scenario_model_compiler,
                "get_settings",
                return_value=SimpleNamespace(scenario_model_llm_timeout=120.0),
            ),
            patch.object(
                scenario_model_compiler.llm_service,
                "chat",
                side_effect=[truncated, TimeoutError("子分块 provider 超时")],
            ) as chat,
        ):
            payload = scenario_model_compiler.compile_scenario_model(
                self.db,
                self.scenario,
                message="请编译附件",
                documents=[document],
                llm=object(),
            )
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(payload["changes"], [])
        self.assertEqual(len(payload["draft_candidates"]), 6)
        self.assertIn(
            "COMPILER_CONTRACT_ERROR",
            {item["code"] for item in payload["unresolved"]},
        )
        self.assertEqual(self.db.scalar(select(func.count()).select_from(OntologyEntity)), 0)

    def test_empty_or_wrapped_contract_never_finishes_with_an_empty_workbench(self) -> None:
        document = {
            "id": "empty-contract",
            "filename": "空结构输出.md",
            "text": "订单对象包含订单编号，并需要审批规则。",
        }
        with patch.object(
            scenario_model_compiler.llm_service,
            "chat",
            return_value={"content": "{}"},
        ) as empty_chat:
            empty_payload = scenario_model_compiler.compile_scenario_model(
                self.db,
                self.scenario,
                message="继续建设完整场景模型",
                documents=[document],
                llm=object(),
            )
        self.assertEqual(empty_chat.call_count, 3)
        self.assertEqual(len(empty_payload["draft_candidates"]), 6)
        self.assertEqual(empty_payload["draft_salvage"]["formal_change_count"], 0)
        self.assertIn(
            "COMPILER_NO_RECOVERABLE_CANDIDATE",
            {
                issue["code"]
                for item in empty_payload["draft_candidates"]
                for issue in item["validation_issues"]
            },
        )

        wrapped = self._raw()
        with patch.object(
            scenario_model_compiler.llm_service,
            "chat",
            return_value={"content": json.dumps({"data": wrapped}, ensure_ascii=False)},
        ) as wrapped_chat:
            wrapped_payload = scenario_model_compiler.compile_scenario_model(
                self.db,
                self.scenario,
                message="继续建设完整场景模型",
                documents=[{
                    "id": "construction-brief",
                    "filename": "建筑项目实施文档.md",
                    "text": "项目以项目编号唯一标识。项目可包含子项目。审批操作校验项目状态，审批后发布已审批事件并进入归档流程。",
                }],
                llm=object(),
            )
        self.assertEqual(wrapped_chat.call_count, 3)
        self.assertTrue(wrapped_payload["draft_candidates"])
        self.assertIn(
            "entity",
            {item["resource_kind"] for item in wrapped_payload["draft_candidates"]},
        )
        self.assertEqual(wrapped_payload["changes"], [])

    def test_chunk_nested_ref_shape_salvages_but_foreign_scope_stays_strict(self) -> None:
        raw = self._raw()
        raw["entities"][0]["evidence_refs"] = 123
        raw["coverage"] = [{
            "source_ref": "construction-brief:p0001",
            "status": "modeled",
            "reason": "候选可识别但 evidence_refs 类型错误",
            "change_keys": ["entity.project"],
        }]
        with patch.object(
            scenario_model_compiler.llm_service,
            "chat",
            return_value={"content": json.dumps(raw, ensure_ascii=False)},
        ) as chat:
            salvaged = scenario_model_compiler._chat_raw_model(
                self.db,
                object(),
                "test",
                max_tokens=1_000,
                allowed_refs={"construction-brief:p0001"},
            )
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(salvaged["entities"][0]["evidence_refs"], [])
        self.assertIn(
            "COMPILER_CONTRACT_ERROR",
            {item["code"] for item in salvaged["unresolved"]},
        )

    def test_completed_chunk_candidates_survive_a_later_chunk_failure(self) -> None:
        document = {
            "id": "partial-chunks",
            "filename": "分段业务文档.md",
            "text": "订单以订单编号唯一标识。\n\n审批规则检查订单状态。",
        }
        first_ref = "partial-chunks:p0001"
        first_chunk = self._raw()
        for section in (
            "relations", "functions", "actions", "rules", "events",
            "workflows", "mappings", "relation_mappings",
        ):
            first_chunk[section] = []
        first_chunk["entities"][0]["evidence_refs"] = [first_ref]
        first_chunk["coverage"] = [{
            "source_ref": first_ref,
            "status": "modeled",
            "reason": "订单对象",
            "change_keys": ["entity.project"],
        }]
        truncated = {
            "content": "{",
            "raw": {"choices": [{"finish_reason": "length"}]},
        }
        malformed = {"content": "{not-json"}

        def two_chunks(paragraphs):
            values = list(paragraphs)
            return [[values[0]], [values[1]]]

        with (
            patch.object(
                scenario_model_compiler,
                "_source_chunks",
                side_effect=two_chunks,
            ),
            patch.object(
                scenario_model_compiler.llm_service,
                "chat",
                side_effect=[
                    truncated,
                    {"content": json.dumps(first_chunk, ensure_ascii=False)},
                    malformed,
                    malformed,
                    malformed,
                ],
            ) as chat,
        ):
            payload = scenario_model_compiler.compile_scenario_model(
                self.db,
                self.scenario,
                message="请编译附件",
                documents=[document],
                llm=object(),
            )
        self.assertEqual(chat.call_count, 5)
        self.assertTrue(payload["changes"])
        entity_drafts = [
            item for item in payload["draft_candidates"]
            if item["resource_kind"] == "entity"
        ]
        self.assertEqual(len(entity_drafts), 1)
        self.assertEqual(entity_drafts[0]["resource_key"], "entity.project")
        self.assertEqual(len(payload["entities"]), 1)
        self.assertEqual(payload["coverage_summary"]["modeled"], 1)
        self.assertEqual(payload["coverage_summary"]["ambiguous"], 1)
        self.assertIn(
            "COMPILER_CONTRACT_ERROR",
            {
                item.get("reported_code") or item["code"]
                for item in payload["unresolved"]
            },
        )
        self.assertGreater(payload["applyability"]["safe_change_count"], 0)
        with self.assertRaises(PolicyViolation):
            scenario_model_compiler.preflight_scenario_model(
                self.db,
                self.scenario,
                payload,
                inspect_mappings=False,
            )

    def test_staged_ontology_generation_keeps_full_contract_and_hides_unrun_stages(self) -> None:
        raw = self._raw()
        progress: list[tuple[str, str, str, str]] = []
        with patch.object(
            scenario_model_compiler.llm_service,
            "chat",
            return_value={"content": json.dumps(raw, ensure_ascii=False)},
        ) as chat:
            payload = scenario_model_compiler.compile_scenario_model(
                self.db,
                self.scenario,
                message="请先完成本体模型建设",
                documents=[{
                    "id": "construction-brief",
                    "filename": "建筑项目实施文档.md",
                    "text": "项目以项目编号唯一标识。项目可包含子项目。审批操作校验项目状态。",
                }],
                llm=object(),
                task_scope="ontology",
                on_progress=lambda step, detail, status, result: progress.append(
                    (step, detail, status, result)
                ),
            )

        prompt = chat.call_args.args[1][1]["content"]
        self.assertIn("当前只建设“本体模型”阶段", prompt)
        self.assertIn("本阶段只有 entities 和 relations 可以非空", prompt)
        self.assertEqual(payload["generation"], {
            "mode": "staged",
            "generated_task_ids": ["ontology"],
        })
        self.assertTrue(payload["entities"])
        self.assertTrue(payload["relations"])
        for section in (
            "instances", "functions", "actions", "rules", "events", "workflows",
            "mappings", "relation_mappings", "conceptual_mappings",
        ):
            self.assertEqual(payload[section], [], section)
        progressed_steps = [step for step, _detail, _status, _result in progress]
        self.assertNotIn("mapping", progressed_steps)
        self.assertNotIn("rules", progressed_steps)

    def test_later_stage_uses_a_compact_task_specific_contract(self) -> None:
        prompt = scenario_model_compiler._compiler_prompt(
            self.scenario,
            message="请继续生成对象实例",
            paragraphs=[{
                "ref": "construction-brief:p0001",
                "source_id": "construction-brief",
                "text": "项目编号为 P-001 的项目存在欠薪预警。",
            }],
            mapping_catalog=[],
            db=self.db,
            task_scope="instances",
        )

        self.assertIn("当前只建设“对象实例”阶段", prompt)
        self.assertIn("instances: [{key,entity_ref,display_name,values,evidence_refs,confidence}]", prompt)
        self.assertIn("entities, relations, functions", prompt)
        self.assertNotIn("目标：把附件与用户补充描述/修正建议共同编译", prompt)

    def test_source_chunks_preserve_ref_order_without_overlap(self) -> None:
        paragraphs = [
            {"ref": f"doc:p{index:04d}", "source_id": "doc", "text": "x" * 20}
            for index in range(1, 7)
        ]
        chunks = scenario_model_compiler._source_chunks(paragraphs, maximum=130)
        flattened_refs = [item["ref"] for chunk in chunks for item in chunk]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(flattened_refs, [item["ref"] for item in paragraphs])
        self.assertEqual(len(flattened_refs), len(set(flattened_refs)))

    def test_full_model_chunks_use_conservative_boundary(self) -> None:
        self.assertEqual(scenario_model_compiler.FALLBACK_SOURCE_CHARS, 12_000)
        self.assertEqual(
            scenario_model_compiler.STAGED_FALLBACK_SOURCE_CHARS,
            24_000,
        )

    def test_postgres_chunk_workers_respect_configured_bound(self) -> None:
        db = SimpleNamespace(get_bind=lambda: SimpleNamespace(
            dialect=SimpleNamespace(name="postgresql")
        ))
        with patch.object(
            scenario_model_compiler,
            "get_settings",
            return_value=SimpleNamespace(scenario_model_max_parallel_chunks=3),
        ):
            self.assertEqual(
                scenario_model_compiler._chunk_parallel_worker_count(db, 8),
                3,
            )
            self.assertEqual(
                scenario_model_compiler._chunk_parallel_worker_count(db, 2),
                2,
            )

    def test_chunk_extraction_runs_in_parallel_with_deterministic_merge(self) -> None:
        document = {
            "id": "parallel-source",
            "filename": "并行建模资料.md",
            "text": "第一段业务资料。\n\n第二段业务资料。\n\n第三段业务资料。",
        }
        source_bundle = scenario_model_compiler.build_source_bundle(
            "请编译附件", [document]
        )
        paragraphs = source_bundle["paragraphs"]
        refs = [item["ref"] for item in paragraphs]
        self.assertEqual(len(paragraphs), 3)
        barrier = threading.Barrier(3)
        used_calls: list[int] = []
        checkpoints: list[dict] = []

        def fake_isolated_worker(*_args, **kwargs):
            kwargs["call_budget"].consume("chunk_provider_call")
            barrier.wait(timeout=5)
            source_ref = kwargs["paragraphs"][0]["ref"]
            raw = {
                "schema_version": "scenario_model.v1",
                **{
                    section: []
                    for section in scenario_model_compiler._MODEL_OUTPUT_RESOURCE_SECTIONS
                },
                "unresolved": [],
                "coverage": [{
                    "source_ref": source_ref,
                    "status": "context",
                    "reason": "并行分段覆盖",
                    "change_keys": [],
                }],
            }
            return raw

        budget = scenario_model_compiler.LLMCallBudget(
            3,
            on_consume=lambda used, _total, _phase: used_calls.append(used),
        )
        normalize_model = scenario_model_compiler.normalize_scenario_model

        with (
            patch.object(
                scenario_model_compiler,
                "_source_chunks",
                return_value=[[item] for item in paragraphs],
            ),
            patch.object(
                scenario_model_compiler,
                "_chunk_parallel_worker_count",
                return_value=3,
            ),
            patch.object(
                scenario_model_compiler,
                "_extract_chunk_once_in_isolated_session",
                side_effect=fake_isolated_worker,
            ) as worker,
            patch.object(
                scenario_model_compiler,
                "normalize_scenario_model",
                side_effect=normalize_model,
            ) as normalize,
        ):
            payload = scenario_model_compiler._compile_scenario_model_in_chunks(
                self.db,
                self.scenario,
                message="请编译附件",
                llm=object(),
                source_bundle=source_bundle,
                mapping_catalog=[],
                columns={},
                call_budget=budget,
                on_checkpoint=lambda data, _detail: checkpoints.append(data),
            )

        self.assertEqual(worker.call_count, 3)
        self.assertGreaterEqual(len(checkpoints), 1)
        self.assertLessEqual(len(checkpoints), 3)
        self.assertEqual(normalize.call_count, len(checkpoints))
        self.assertEqual(used_calls, [1, 2, 3])
        self.assertEqual(
            [item["source_ref"] for item in payload["coverage"]],
            refs,
        )
        self.assertEqual(payload["coverage_summary"]["total"], 3)

    def test_recursive_splits_reenter_the_bounded_parallel_queue(self) -> None:
        document = {
            "id": "parallel-recursive-source",
            "filename": "递归并行建模资料.md",
            "text": "第一段。\n\n第二段。\n\n第三段。\n\n第四段。",
        }
        source_bundle = scenario_model_compiler.build_source_bundle(
            "请编译附件", [document]
        )
        paragraphs = source_bundle["paragraphs"]
        refs = [item["ref"] for item in paragraphs]
        initial_barrier = threading.Barrier(3)
        child_barrier = threading.Barrier(2)
        lock = threading.Lock()
        active = 0
        max_active = 0
        labels: list[str] = []
        checkpoints: list[dict] = []

        def fake_leaf_worker(*_args, **kwargs):
            nonlocal active, max_active
            label = kwargs["chunk_label"]
            kwargs["call_budget"].consume("chunk_provider_call")
            with lock:
                labels.append(label)
                active += 1
                max_active = max(max_active, active)
            try:
                if label in {"1", "2", "3"}:
                    initial_barrier.wait(timeout=5)
                if label == "1":
                    raise scenario_model_compiler._CompilerOutputTruncated(
                        "需要二分"
                    )
                if label in {"1.1", "1.2"}:
                    child_barrier.wait(timeout=5)
                return {
                    "schema_version": "scenario_model.v1",
                    **{
                        section: []
                        for section in scenario_model_compiler._MODEL_OUTPUT_RESOURCE_SECTIONS
                    },
                    "unresolved": [],
                    "coverage": [
                        {
                            "source_ref": item["ref"],
                            "status": "context",
                            "reason": "动态并行分段覆盖",
                            "change_keys": [],
                        }
                        for item in kwargs["paragraphs"]
                    ],
                }
            finally:
                with lock:
                    active -= 1

        budget = scenario_model_compiler.LLMCallBudget(5)
        with (
            patch.object(
                scenario_model_compiler,
                "_source_chunks",
                return_value=[paragraphs[:2], [paragraphs[2]], [paragraphs[3]]],
            ),
            patch.object(
                scenario_model_compiler,
                "_chunk_parallel_worker_count",
                return_value=3,
            ),
            patch.object(
                scenario_model_compiler,
                "_extract_chunk_once_in_isolated_session",
                side_effect=fake_leaf_worker,
            ),
        ):
            payload = scenario_model_compiler._compile_scenario_model_in_chunks(
                self.db,
                self.scenario,
                message="请编译附件",
                llm=object(),
                source_bundle=source_bundle,
                mapping_catalog=[],
                columns={},
                call_budget=budget,
                on_checkpoint=lambda data, _detail: checkpoints.append(data),
            )

        self.assertEqual(set(labels), {"1", "1.1", "1.2", "2", "3"})
        self.assertEqual(max_active, 3)
        self.assertGreaterEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[-1]["coverage_summary"]["total"], 4)
        self.assertEqual(
            [item["source_ref"] for item in payload["coverage"]],
            refs,
        )

    def test_parallel_failed_chunk_does_not_replace_completed_resources(self) -> None:
        document = {
            "id": "parallel-partial-source",
            "filename": "部分失败并行资料.md",
            "text": "项目对象。\n\n背景说明。\n\n暂不可用来源。",
        }
        source_bundle = scenario_model_compiler.build_source_bundle(
            "请编译附件", [document]
        )
        paragraphs = source_bundle["paragraphs"]
        refs = [item["ref"] for item in paragraphs]
        checkpoints: list[dict] = []

        def fake_leaf_worker(*_args, **kwargs):
            label = kwargs["chunk_label"]
            source_ref = kwargs["paragraphs"][0]["ref"]
            if label == "3":
                raise TimeoutError("第三分段暂不可用")
            raw = self._raw()
            for section in scenario_model_compiler._MODEL_OUTPUT_RESOURCE_SECTIONS:
                raw[section] = []
            if label == "1":
                entity = self._raw()["entities"][0]
                entity["evidence_refs"] = [source_ref]
                raw["entities"] = [entity]
                status = "modeled"
                change_keys = [entity["key"]]
            else:
                status = "context"
                change_keys = []
            raw["unresolved"] = []
            raw["coverage"] = [{
                "source_ref": source_ref,
                "status": status,
                "reason": "并行部分结果",
                "change_keys": change_keys,
            }]
            return raw

        with (
            patch.object(
                scenario_model_compiler,
                "_source_chunks",
                return_value=[[item] for item in paragraphs],
            ),
            patch.object(
                scenario_model_compiler,
                "_chunk_parallel_worker_count",
                return_value=3,
            ),
            patch.object(
                scenario_model_compiler,
                "_extract_chunk_once_in_isolated_session",
                side_effect=fake_leaf_worker,
            ),
        ):
            payload = scenario_model_compiler._compile_scenario_model_in_chunks(
                self.db,
                self.scenario,
                message="请编译附件",
                llm=object(),
                source_bundle=source_bundle,
                mapping_catalog=[],
                columns={},
                on_checkpoint=lambda data, _detail: checkpoints.append(data),
            )

        self.assertEqual(len(payload["entities"]), 1)
        self.assertTrue(payload["draft_candidates"])
        self.assertEqual(payload["coverage_summary"]["modeled"], 1)
        self.assertEqual(payload["coverage_summary"]["context"], 1)
        self.assertEqual(payload["coverage_summary"]["ambiguous"], 1)
        self.assertEqual(checkpoints[-1]["entities"], payload["entities"])
        self.assertIn(
            "COMPILER_CONTRACT_ERROR",
            {
                item.get("reported_code") or item["code"]
                for item in payload["unresolved"]
            },
        )

    def test_large_source_skips_full_call_and_starts_with_bounded_chunks(self) -> None:
        document = {
            "id": "large-source",
            "filename": "大型业务文档.md",
            "text": "大型文档背景段。" * 5_000,
        }
        source_bundle = scenario_model_compiler.build_source_bundle(
            "请编译附件", [document]
        )
        expected_chunks = scenario_model_compiler._source_chunks(
            source_bundle["paragraphs"]
        )
        all_refs = [item["ref"] for item in source_bundle["paragraphs"]]
        self.assertGreater(
            source_bundle["total_characters"],
            scenario_model_compiler.DIRECT_CHUNK_SOURCE_CHARS,
        )

        def fake_chat(*args, **kwargs):  # noqa: ANN002, ANN003
            prompt = args[1][1]["content"]
            prompt_refs = [source_ref for source_ref in all_refs if source_ref in prompt]
            raw = {
                "schema_version": "scenario_model.v1",
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
                "coverage": [
                    {
                        "source_ref": source_ref,
                        "status": "context",
                        "reason": "大型文档背景",
                        "change_keys": [],
                    }
                    for source_ref in prompt_refs
                ],
            }
            return {"content": json.dumps(raw, ensure_ascii=False)}

        with (
            patch.object(
                scenario_model_compiler,
                "get_settings",
                return_value=SimpleNamespace(scenario_model_llm_timeout=120.0),
            ),
            patch.object(scenario_model_compiler.llm_service, "chat", side_effect=fake_chat) as chat,
        ):
            payload = scenario_model_compiler.compile_scenario_model(
                self.db,
                self.scenario,
                message="请编译附件",
                documents=[document],
                llm=object(),
            )

        self.assertEqual(chat.call_count, len(expected_chunks))
        self.assertTrue(chat.call_args_list)
        self.assertTrue(all(
            call.kwargs["max_tokens"]
            == scenario_model_compiler.FALLBACK_MAX_OUTPUT_TOKENS
            for call in chat.call_args_list
        ))
        self.assertTrue(all(
            "这是整份文档的超时降级分块" in call.args[1][1]["content"]
            for call in chat.call_args_list
        ))
        self.assertEqual(payload["coverage_summary"]["total"], len(all_refs))
        self.assertNotIn(
            "missing_source_coverage",
            {item["code"] for item in payload["unresolved"]},
        )
        self.assertEqual(self.db.scalar(select(func.count()).select_from(OntologyEntity)), 0)

    def test_malformed_non_truncated_full_response_retries_without_chunking(self) -> None:
        document = {
            "id": "construction-brief",
            "filename": "建筑项目实施文档.md",
            "text": "项目以项目编号唯一标识。项目可包含子项目。审批操作校验项目状态，审批后发布已审批事件并进入归档流程。",
        }
        responses = [
            {
                "content": "{malformed",
                "raw": {"choices": [{"finish_reason": "stop"}]},
            },
            {"content": json.dumps(self._raw(), ensure_ascii=False)},
        ]
        with (
            patch.object(
                scenario_model_compiler,
                "get_settings",
                return_value=SimpleNamespace(scenario_model_llm_timeout=120.0),
            ),
            patch.object(
                scenario_model_compiler.llm_service,
                "chat",
                side_effect=responses,
            ) as chat,
            patch.object(scenario_model_compiler, "_source_chunks") as chunker,
        ):
            payload = scenario_model_compiler.compile_scenario_model(
                self.db,
                self.scenario,
                message="请编译附件",
                documents=[document],
                llm=object(),
            )
        self.assertEqual(chat.call_count, 2)
        chunker.assert_not_called()
        self.assertFalse(payload["unresolved"])

    def test_non_timeout_provider_error_creates_placeholders_without_chunk_loop(self) -> None:
        document = {
            "id": "construction-brief",
            "filename": "建筑项目实施文档.md",
            "text": "项目以项目编号唯一标识。",
        }
        with (
            patch.object(
                scenario_model_compiler,
                "get_settings",
                return_value=SimpleNamespace(scenario_model_llm_timeout=120.0),
            ),
            patch.object(
                scenario_model_compiler.llm_service,
                "chat",
                side_effect=RuntimeError("provider rejected request"),
            ),
            patch.object(scenario_model_compiler, "_source_chunks") as chunker,
        ):
            payload = scenario_model_compiler.compile_scenario_model(
                self.db,
                self.scenario,
                message="请编译附件",
                documents=[document],
                llm=object(),
            )
        chunker.assert_not_called()
        self.assertEqual(len(payload["draft_candidates"]), 6)
        self.assertEqual(payload["changes"], [])
        self.assertEqual(
            {issue["code"] for issue in payload["unresolved"]},
            {"COMPILER_PROVIDER_REQUEST_FAILED"},
        )

    def test_missing_or_unavailable_llm_still_creates_source_bound_placeholders(self) -> None:
        document = {
            "id": "provider-unavailable",
            "filename": "业务说明.md",
            "text": "订单以订单编号唯一标识。",
        }
        progress_events: list[tuple[str, str]] = []
        without_llm = scenario_model_compiler.compile_scenario_model(
            self.db,
            self.scenario,
            message="继续建设场景模型",
            documents=[document],
            llm=None,
            on_progress=lambda step_id, _detail, status, _result: (
                progress_events.append((step_id, status))
            ),
        )
        self.assertEqual(len(without_llm["draft_candidates"]), 6)
        self.assertEqual(
            {issue["code"] for issue in without_llm["unresolved"]},
            {"LLM_NOT_CONFIGURED"},
        )
        self.assertTrue({
            ("analyze", "done"),
            ("plan", "done"),
            ("ontology", "done"),
            ("mapping", "done"),
            ("rules", "done"),
            ("review", "done"),
        }.issubset(set(progress_events)))

        connection_error = type("APIConnectionError", (RuntimeError,), {})
        with patch.object(
            scenario_model_compiler.llm_service,
            "chat",
            side_effect=connection_error("Connection error."),
        ) as chat:
            unavailable = scenario_model_compiler.compile_scenario_model(
                self.db,
                self.scenario,
                message="继续建设场景模型",
                documents=[document],
                llm=object(),
            )
        self.assertEqual(chat.call_count, 3)
        self.assertEqual(len(unavailable["draft_candidates"]), 6)
        self.assertEqual(
            {issue["code"] for issue in unavailable["unresolved"]},
            {"COMPILER_PROVIDER_UNAVAILABLE"},
        )

    def test_compound_model_applies_all_resources_and_rollback_is_atomic(self) -> None:
        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            self._raw(),
            source_bundle=self._bundle(),
        )
        self.assertEqual(payload["schema_version"], "scenario_model.v1")
        self.assertFalse(payload["unresolved"])
        self.assertEqual(payload["coverage_summary"]["modeled"], 1)
        self.assertIn("property", {change["resource"] for change in payload["changes"]})

        result = scenario_model_compiler.apply_scenario_model(self.db, self.scenario, payload)
        self.assertEqual(result["kind"], "scenario_model")
        self.assertEqual(self.db.scalar(select(func.count()).select_from(OntologyEntity)), 1)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(OntologyRelation)), 1)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(FunctionDefinition)), 1)
        action = self.db.scalars(select(OntologyAction)).one()
        self.assertEqual(action.executor_type, "unbound")
        self.assertFalse(action.enabled)
        self.assertFalse(self.db.scalars(select(OntologyRule)).one().enabled)
        self.assertFalse(self.db.scalars(select(OntologyEvent)).one().enabled)
        workflow = self.db.scalars(select(OntologyWorkflow)).one()
        self.assertEqual(workflow.status, "draft")
        self.assertFalse(workflow.enabled)
        self.assertEqual(workflow.nodes[1]["data"]["action_id"], action.id)

        self.db.rollback()
        self.assertEqual(self.db.scalar(select(func.count()).select_from(OntologyEntity)), 0)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(OntologyAction)), 0)

    def test_missing_source_coverage_blocks_every_write(self) -> None:
        raw = self._raw()
        raw["coverage"] = []
        # Remove evidence too, so both provenance protections are exercised.
        raw["actions"][0]["evidence_refs"] = []
        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
        )
        codes = {item["code"] for item in payload["unresolved"]}
        self.assertIn("missing_evidence", codes)
        # Other resources still reference the paragraph, so coverage is
        # modeled; the missing Action evidence alone must block the transaction.
        with self.assertRaises(PolicyViolation):
            scenario_model_compiler.apply_scenario_model(self.db, self.scenario, payload)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(OntologyEntity)), 0)

    def test_existing_property_conflict_is_visible_before_confirmation(self) -> None:
        entity = OntologyEntity(scenario_id=self.scenario.id, name="项目")
        self.db.add(entity)
        self.db.flush()
        self.db.add(OntologyProperty(
            entity_id=entity.id,
            name="项目编号",
            data_type="integer",
            is_key=True,
            is_required=True,
        ))
        self.db.commit()
        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            self._raw(),
            source_bundle=self._bundle(),
        )
        self.assertIn(
            "existing_property_conflict",
            {item["code"] for item in payload["unresolved"]},
        )
        with self.assertRaises(PolicyViolation):
            scenario_model_compiler.apply_scenario_model(self.db, self.scenario, payload)

    def test_relationship_metadata_requires_a_linking_object(self) -> None:
        raw = self._raw()
        raw["relations"][0]["properties"] = [{"name": "包含日期", "data_type": "date"}]
        payload = scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
        )
        self.assertIn(
            "relationship_metadata_requires_linking_object",
            {item["code"] for item in payload["unresolved"]},
        )


if __name__ == "__main__":
    unittest.main()
