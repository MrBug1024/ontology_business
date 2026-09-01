from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

from app.models import DataSource
from app.schemas import AgentRuntimeConnectionIn, DataSourceIn
from app.routers import agents, data_sources
from app.services import datasource_service


class PostgresDataSourceConfigTests(unittest.TestCase):
    def test_use_boundary_rejects_missing_and_out_of_range_fields(self) -> None:
        secret = "test-only-password-must-not-leak"
        invalid_configs = (
            {"port": 5432, "database": "audit", "user": "reader", "password": secret},
            {"host": "db.example.test", "database": "audit", "user": "reader"},
            {"host": "db.example.test", "port": 0, "database": "audit", "user": "reader"},
            {"host": "db.example.test", "port": 65536, "database": "audit", "user": "reader"},
            {"host": "db.example.test", "port": 5432, "user": "reader"},
            {"host": "db.example.test", "port": 5432, "database": "audit"},
            {"host": "h" * 254, "port": 5432, "database": "audit", "user": "reader"},
            {"host": "db.example.test", "port": 5432, "database": "d" * 129, "user": "reader"},
            {"host": "db.example.test", "port": 5432, "database": "audit", "user": "u" * 129},
        )

        for index, config in enumerate(invalid_configs):
            with self.subTest(index=index):
                source = DataSource(
                    id=f"source-invalid-{index}",
                    name="invalid",
                    type="postgres",
                    config=config,
                )
                with self.assertRaises(ValueError) as raised:
                    datasource_service._db_url(source)
                self.assertNotIn(secret, str(raised.exception))

    def test_valid_config_is_canonicalized_without_defaults(self) -> None:
        config = datasource_service.normalize_postgres_config(
            {
                "host": " db.example.test ",
                "port": "5433",
                "database": " audit ",
                "username": " reader ",
                "password": "test-only-password",
            }
        )

        self.assertEqual(
            config,
            {
                "host": "db.example.test",
                "port": 5433,
                "database": "audit",
                "user": "reader",
                "password": "test-only-password",
            },
        )

    def test_connection_test_fails_closed_before_engine_creation(self) -> None:
        secret = "test-only-password-must-not-leak"
        source = DataSource(
            id="source-invalid-test",
            name="invalid",
            type="postgres",
            config={
                "port": 5432,
                "database": "audit",
                "user": "reader",
                "password": secret,
            },
        )

        with patch("app.services.datasource_service.create_engine") as create:
            ok, message = datasource_service.test_connection(source)

        self.assertFalse(ok)
        self.assertEqual(message, datasource_service.CONNECTION_TEST_FAILURE_MESSAGE)
        self.assertNotIn(secret, message)
        create.assert_not_called()

    def test_modeling_create_rejects_incomplete_postgres_config_before_write(self) -> None:
        secret = "test-only-password-must-not-leak"
        db = Mock()
        db.info = {"tenant_id": "tenant-test", "user_id": "user-test"}
        payload = DataSourceIn(
            name="invalid",
            type="postgres",
            config={
                "port": 5432,
                "database": "audit",
                "user": "reader",
                "password": secret,
            },
        )

        with (
            patch(
                "app.routers.data_sources.permission_service.require_tenant_permission"
            ),
            patch(
                "app.routers.data_sources.template_catalog_service."
                "lock_scenarios_for_template_write"
            ),
            patch(
                "app.routers.data_sources.tenant_service.current_tenant_id",
                return_value="tenant-test",
            ),
            self.assertRaises(HTTPException) as raised,
        ):
            data_sources.create_data_source(payload, db)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertNotIn(secret, str(raised.exception.detail))
        db.add.assert_not_called()
        db.commit.assert_not_called()

    def test_agent_runtime_create_rejects_incomplete_config_before_write(self) -> None:
        secret = "test-only-password-must-not-leak"
        db = Mock()
        agent = SimpleNamespace(id="agent-test", scenario_id="scenario-test")
        submitted = [
            AgentRuntimeConnectionIn(
                name="invalid",
                config={
                    "port": 5432,
                    "database": "audit",
                    "user": "reader",
                    "password": secret,
                },
            )
        ]

        with (
            patch("app.routers.agents._agent_runtime_sources", return_value=[]),
            self.assertRaises(HTTPException) as raised,
        ):
            agents._sync_runtime_connections(db, agent, submitted)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertNotIn(secret, str(raised.exception.detail))
        db.add.assert_not_called()
        db.flush.assert_not_called()


if __name__ == "__main__":
    unittest.main()
