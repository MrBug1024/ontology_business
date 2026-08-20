from __future__ import annotations

import unittest

from app.services.policies import PolicyViolation, validate_read_only_sql, validate_workflow_graph
from app.services.auth_service import hash_password, verify_password
from app.services.workflow_service import evaluate_condition


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


if __name__ == "__main__":
    unittest.main()
