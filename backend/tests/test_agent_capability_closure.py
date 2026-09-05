"""Independent policy coverage retained after retiring the legacy Agent kernel."""
from __future__ import annotations

import unittest

from app.services.policies import (
    PolicyViolation,
    validate_agent_sql_scope,
    validate_read_only_sql,
)


class AgentCapabilityPolicyTests(unittest.TestCase):
    def test_read_only_policy_rejects_postgres_mutating_select_forms(self) -> None:
        for unsafe_sql in (
            "SELECT project_code INTO archived_projects FROM projects",
            "SELECT nextval('project_sequence')",
            'SELECT "pg_sleep"(1)',
            "SELECT set_config('search_path', 'public', false)",
            "SELECT project_code FROM projects FOR UPDATE",
            "EXPLAIN ANALYZE SELECT project_code FROM projects",
        ):
            with self.assertRaises(PolicyViolation, msg=unsafe_sql):
                validate_read_only_sql(unsafe_sql)

    def test_sql_scope_rejects_noncanonical_mapping_identifiers(self) -> None:
        with self.assertRaises(PolicyViolation):
            validate_agent_sql_scope(
                "SELECT project_code FROM projects",
                {"PROJECTS": {"project_code"}},
            )
        with self.assertRaises(PolicyViolation):
            validate_agent_sql_scope(
                "SELECT project_code FROM projects",
                {"projects": {"PROJECT_CODE"}},
            )


if __name__ == "__main__":
    unittest.main()
