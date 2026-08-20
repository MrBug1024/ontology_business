from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.main import app
from app.schemas import ObjectProvenanceOut, ObjectSearchItemOut, ObjectSearchOut
from app.services.policies import PolicyViolation, validate_read_only_sql, validate_workflow_graph
from app.services.auth_service import hash_password, verify_password
from app.services.workflow_service import evaluate_condition
from app.routers.assistant import _context_scope, _intent, _sse


class SQLPolicyTests(unittest.TestCase):
    def test_accepts_read_query(self) -> None:
        self.assertEqual(validate_read_only_sql("SELECT * FROM records;"), "SELECT * FROM records")
        self.assertEqual(validate_read_only_sql("WITH x AS (SELECT 1) SELECT * FROM x"), "WITH x AS (SELECT 1) SELECT * FROM x")

    def test_rejects_mutation_and_multiple_statements(self) -> None:
        for sql in ("UPDATE records SET value = 1", "WITH x AS (DELETE FROM records) SELECT * FROM x", "SELECT 1; SELECT 2"):
            with self.assertRaises(PolicyViolation):
                validate_read_only_sql(sql)


class WorkflowPolicyTests(unittest.TestCase):
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

    def test_session_scope_includes_current_route_and_scenario(self) -> None:
        self.assertEqual(_context_scope("scenario-1", "/scenarios/scenario-1?tab=ontology"), "scenario:scenario-1|path:/scenarios/scenario-1")
        self.assertEqual(_context_scope(None, "/dashboard"), "scenario:global|path:/dashboard")

    def test_sse_event_is_framed_as_json_data(self) -> None:
        event = _sse("token", "你好")
        self.assertTrue(event.startswith("data: "))
        self.assertTrue(event.endswith("\n\n"))
        self.assertIn('"type": "token"', event)


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


if __name__ == "__main__":
    unittest.main()
