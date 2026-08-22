from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.main import app
from app.models import DataSource, OntologyEntity
from app.schemas import AssistantProposalApplyRequest, ObjectProvenanceOut, ObjectSearchItemOut, ObjectSearchOut, WorkflowIn
from app.services.policies import PolicyViolation, validate_action_params, validate_read_only_sql, validate_workflow_graph
from app.services.auth_service import hash_password, verify_password
from app.services.ontology_service import _quoted_mapping_table, preview_mapping
from app.services.workflow_service import evaluate_condition, execute_workflow, validate_workflow_definition
from app.routers.assistant import (
    _build_proposal,
    _context_scope,
    _intent,
    _scenario_snapshot,
    _snapshot_matches,
    _sse,
    apply_proposal,
)


class SQLPolicyTests(unittest.TestCase):
    def test_accepts_read_query(self) -> None:
        self.assertEqual(validate_read_only_sql("SELECT * FROM records;"), "SELECT * FROM records")
        self.assertEqual(validate_read_only_sql("WITH x AS (SELECT 1) SELECT * FROM x"), "WITH x AS (SELECT 1) SELECT * FROM x")

    def test_rejects_mutation_and_multiple_statements(self) -> None:
        for sql in ("UPDATE records SET value = 1", "WITH x AS (DELETE FROM records) SELECT * FROM x", "SELECT 1; SELECT 2"):
            with self.assertRaises(PolicyViolation):
                validate_read_only_sql(sql)


class ActionPolicyTests(unittest.TestCase):
    def test_validates_full_json_schema_and_defaults(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 2},
                "count": {"type": "integer", "minimum": 1, "default": 1},
                "enabled": {"type": "boolean"},
            },
            "required": ["name"],
            "additionalProperties": False,
        }
        self.assertEqual(
            validate_action_params(schema, {"name": "ok", "enabled": True}),
            {"name": "ok", "enabled": True, "count": 1},
        )

    def test_validates_legacy_flat_schema_and_rejects_wrong_values(self) -> None:
        self.assertEqual(
            validate_action_params({"priority": {"type": "integer"}}, {"priority": 2}),
            {"priority": 2},
        )
        with self.assertRaises(PolicyViolation):
            validate_action_params({"priority": {"type": "integer"}}, {"priority": "2"})
        with self.assertRaises(PolicyViolation):
            validate_action_params(
                {"type": "object", "properties": {"kind": {"type": "string", "enum": ["A", "B"]}}, "required": ["kind"]},
                {"kind": "C"},
            )


class WorkflowPolicyTests(unittest.TestCase):
    def test_workflow_definition_policy_is_authoritative_and_defaults_to_draft(self) -> None:
        workflow = WorkflowIn(name="审批流程")
        self.assertEqual(workflow.status, "draft")
        validate_workflow_definition(
            [
                {"id": "start", "type": "start"},
                {"id": "end", "type": "end"},
            ],
            [{"source": "start", "target": "end"}],
        )
        with self.assertRaises(PolicyViolation):
            validate_workflow_definition(
                [{"id": "start", "type": "start"}, {"id": "end", "type": "end"}],
                [{"source": "start", "target": "missing"}],
            )

    def test_draft_workflow_cannot_execute(self) -> None:
        workflow = SimpleNamespace(status="draft", enabled=True)
        with self.assertRaises(PolicyViolation):
            execute_workflow(None, workflow, {})

    def test_accepts_valid_dag(self) -> None:
        nodes = [
            {"id": "start", "type": "start"},
            {"id": "n1", "type": "action"},
            {"id": "end", "type": "end"},
        ]
        edges = [
            {"source": "start", "target": "n1", "label": ""},
            {"source": "n1", "target": "end", "label": ""},
        ]
        validate_workflow_graph(nodes, edges)

    def test_rejects_cycle(self) -> None:
        nodes = [
            {"id": "start", "type": "start"},
            {"id": "n1", "type": "action"},
            {"id": "end", "type": "end"},
        ]
        edges = [
            {"source": "start", "target": "n1", "label": ""},
            {"source": "n1", "target": "n1", "label": ""},
            {"source": "n1", "target": "end", "label": ""},
        ]
        with self.assertRaises(PolicyViolation):
            validate_workflow_graph(nodes, edges)

    def test_rejects_unknown_node_and_incomplete_rule(self) -> None:
        with self.assertRaises(PolicyViolation):
            validate_workflow_graph(
                [{"id": "start", "type": "start"}, {"id": "x", "type": "custom"}, {"id": "end", "type": "end"}],
                [{"source": "start", "target": "x"}, {"source": "x", "target": "end"}],
            )
        with self.assertRaises(PolicyViolation):
            validate_workflow_graph(
                [{"id": "start", "type": "start"}, {"id": "r", "type": "rule"}, {"id": "end", "type": "end"}],
                [{"source": "start", "target": "r"}, {"source": "r", "target": "end", "label": "true"}],
            )

    def test_rule_evaluation_remains_domain_neutral(self) -> None:
        self.assertTrue(evaluate_condition({"field": "value", "op": ">", "value": 2}, {"value": 3}))
        self.assertFalse(evaluate_condition({"field": "value", "op": "is_null"}, {"value": 0}))


class AuthPolicyTests(unittest.TestCase):
    def test_password_hash_is_not_reversible_and_verifies(self) -> None:
        encoded = hash_password("Password123")
        self.assertNotEqual(encoded, "Password123")
        self.assertTrue(verify_password("Password123", encoded))
        self.assertFalse(verify_password("wrong-password", encoded))


class AssistantIntentTests(unittest.TestCase):
    def test_routes_business_requests_to_safe_draft_intents(self) -> None:
        self.assertEqual(_intent("请根据资料建立供应商实体和关系", "ask"), "ontology")
        self.assertEqual(_intent("把异常处理编排成审批工作流", "ask"), "workflow")
        self.assertEqual(_intent("帮我看看当前页面", "ask"), "chat")
        self.assertEqual(_intent("继续分析", "draft"), "ontology")
        self.assertEqual(_intent("帮我创建场景", "explain"), "explain")
        self.assertEqual(_intent("立即应用这份草稿", "apply"), "apply_guidance")
        self.assertEqual(_intent("执行这个工作流", "execute"), "execute_guidance")

    def test_session_scope_includes_current_route_and_scenario(self) -> None:
        self.assertEqual(_context_scope("scenario-1", "/scenarios/scenario-1?tab=ontology"), "scenario:scenario-1|path:/scenarios/scenario-1")
        self.assertEqual(_context_scope(None, "/dashboard"), "scenario:global|path:/dashboard")

    def test_sse_event_is_framed_as_json_data(self) -> None:
        event = _sse("token", "你好")
        self.assertTrue(event.startswith("data: "))
        self.assertTrue(event.endswith("\n\n"))
        self.assertIn('"type": "token"', event)

    def test_change_set_has_identity_diff_and_baseline(self) -> None:
        scenario = SimpleNamespace(
            entities=[SimpleNamespace(name="客户")],
            relations=[SimpleNamespace(name="客户拥有订单")],
            workflows=[SimpleNamespace(name="历史流程")],
        )
        proposal = _build_proposal(
            "ontology",
            {
                "entities": [
                    {"name": "客户", "properties": []},
                    {"name": "订单", "properties": [{"name": "订单号"}]},
                ],
                "relations": [{"name": "客户拥有订单", "source": "客户", "target": "订单"}],
            },
            scenario,
        )
        self.assertEqual(len(proposal["proposal_id"]), 32)
        self.assertEqual(proposal["status"], "pending")
        self.assertEqual(proposal["base_snapshot"]["entity_names"], ["客户"])
        self.assertEqual([item["operation"] for item in proposal["changes"]], ["skip", "add", "skip"])
        self.assertEqual(len(proposal["base_snapshot"]["revision"]), 64)
        scenario.entities[0].description = "并发修改了实体定义"
        self.assertFalse(
            _snapshot_matches(
                proposal["base_snapshot"],
                _scenario_snapshot(scenario),
            )
        )

    def test_apply_request_requires_confirmation_and_saved_proposal_identity(self) -> None:
        request = AssistantProposalApplyRequest(
            kind="workflow",
            scenario_id="scenario-1",
            thread_id="thread-1",
            proposal_id="proposal-1",
            confirm=True,
        )
        self.assertTrue(request.confirm)
        self.assertEqual(request.payload, {})

    def test_apply_proposal_rejects_without_explicit_confirmation(self) -> None:
        request = AssistantProposalApplyRequest(
            kind="workflow",
            scenario_id="scenario-1",
            thread_id="thread-1",
            proposal_id="proposal-1",
        )
        with self.assertRaises(HTTPException) as error:
            apply_proposal(request, None)
        self.assertEqual(error.exception.status_code, 409)

    def test_apply_proposal_uses_saved_payload_and_marks_message_applied(self) -> None:
        scenario = SimpleNamespace(id="scenario-1", entities=[], relations=[], workflows=[])
        thread = SimpleNamespace(id="thread-1", scenario_id="scenario-1")
        proposal_message = SimpleNamespace(id="message-1", proposal={})
        saved_proposal = {
            "proposal_id": "proposal-1",
            "kind": "workflow",
            "status": "pending",
            "base_snapshot": _scenario_snapshot(scenario),
            "payload": {
                "name": "审批草稿",
                "nodes": [{"id": "start", "type": "start"}, {"id": "end", "type": "end"}],
                "edges": [{"source": "start", "target": "end"}],
            },
        }

        class FakeDb:
            info = {"tenant_id": "tenant-1", "user_id": "user-1"}

            def __init__(self) -> None:
                self.added = []
                self.committed = False

            def add(self, value) -> None:
                self.added.append(value)

            def commit(self) -> None:
                self.committed = True

            def flush(self) -> None:
                return None

            def rollback(self) -> None:
                return None

            def execute(self, _statement):
                return self

            def scalars(self):
                return self

            def first(self):
                return scenario

            def expire(self, _value, _attributes) -> None:
                return None

        db = FakeDb()
        request = AssistantProposalApplyRequest(
            kind="workflow",
            scenario_id="scenario-1",
            thread_id="thread-1",
            proposal_id="proposal-1",
            confirm=True,
            payload={"name": "客户端篡改的工作流", "nodes": [], "edges": []},
        )
        fake_workflow = SimpleNamespace(id="workflow-1")
        claim = SimpleNamespace(status="applying", result={}, applied_at=None)
        with patch("app.routers.assistant._scenario", return_value=scenario), patch(
            "app.routers.assistant._find_saved_proposal",
            return_value=(thread, proposal_message, saved_proposal),
        ), patch(
            "app.routers.assistant._claim_proposal_application",
            return_value=(claim, True),
        ), patch("app.routers.assistant.OntologyWorkflow", return_value=fake_workflow):
            result = apply_proposal(request, db)

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["workflow_id"], "workflow-1")
        self.assertEqual(proposal_message.proposal["status"], "applied")
        self.assertEqual(proposal_message.proposal["payload"]["name"], "审批草稿")
        self.assertTrue(db.committed)


class ObjectRuntimeTests(unittest.TestCase):
    def test_object_runtime_routes_are_registered(self) -> None:
        paths = {route.path for route in app.routes}
        self.assertIn("/api/scenarios/{scenario_id}/objects", paths)
        self.assertIn("/api/scenarios/{scenario_id}/objects/{object_id}", paths)

    def test_object_runtime_contract_keeps_provenance_and_relation_count(self) -> None:
        item = ObjectSearchItemOut(
            id="object-1",
            scenario_id="scenario-1",
            entity_id="entity-1",
            entity_name="供应商",
            entity_color="#27b9b0",
            name="华东供应商",
            attributes={"编码": "SUP-001"},
            source="imported",
            source_ref="supplier_master:SUP-001",
            provenance=ObjectProvenanceOut(
                kind="imported",
                reference="supplier_master:SUP-001",
                data_source_name="主数据源",
                table_name="supplier_master",
                status="ok",
            ),
            relation_count=2,
            created_at=datetime.now(timezone.utc),
        )
        result = ObjectSearchOut(items=[item], total=1, limit=50, offset=0)
        self.assertEqual(result.items[0].provenance.table_name, "supplier_master")
        self.assertEqual(result.items[0].relation_count, 2)


class MappingRuntimeTests(unittest.TestCase):
    def test_mapping_runtime_routes_are_registered(self) -> None:
        paths = {route.path for route in app.routes}
        self.assertIn("/api/scenarios/mappings/{mapping_id}/preview", paths)
        self.assertIn("/api/scenarios/mappings/{mapping_id}/test", paths)
        self.assertIn("/api/scenarios/mappings/{mapping_id}/refresh-jobs", paths)
        self.assertIn("/api/scenarios/mappings/refresh-jobs/{job_id}", paths)
        self.assertIn("/api/scenarios/mappings/{mapping_id}/refresh", paths)

    def test_preview_reports_mapping_coverage_and_unmapped_columns(self) -> None:
        scenario = SimpleNamespace(id="scenario-1")
        data_source = SimpleNamespace(id="source-1", scenario_id=None, type="sqlite", name="业务库")
        entity = SimpleNamespace(
            id="entity-1",
            scenario_id="scenario-1",
            name="供应商",
            properties=[
                SimpleNamespace(name="编码", data_type="string", is_key=True, is_required=True),
                SimpleNamespace(name="名称", data_type="string", is_key=False, is_required=True),
            ],
        )
        mapping = SimpleNamespace(
            id="mapping-1",
            scenario_id="scenario-1",
            data_source_id="source-1",
            entity_id="entity-1",
            table_name="supplier_master",
            column_map={"编码": "supplier_id", "名称": "supplier_name"},
        )

        class FakeDb:
            def get(self, model, _id):
                return data_source if model is DataSource else entity if model is OntologyEntity else None

        with patch(
            "app.services.ontology_service.datasource_service.run_query",
            return_value={
                "columns": ["supplier_id", "supplier_name", "unused"],
                "rows": [["SUP-001", "华东供应商", "x"]],
                "row_count": 1,
                "truncated": False,
            },
        ):
            result = preview_mapping(FakeDb(), scenario, mapping)

        self.assertTrue(result["ok"])
        self.assertEqual(result["fields"][0]["status"], "mapped")
        self.assertEqual(result["unmapped_columns"], ["unused"])
        self.assertTrue(result["warnings"])

    def test_mapping_table_name_is_restricted_to_identifiers(self) -> None:
        self.assertEqual(_quoted_mapping_table("public.suppliers"), '"public"."suppliers"')
        with self.assertRaises(ValueError):
            _quoted_mapping_table('suppliers"; DROP TABLE users')


if __name__ == "__main__":
    unittest.main()
