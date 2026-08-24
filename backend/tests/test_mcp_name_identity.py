from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import ValidationError

from app import database
from app.database import Base
from app.models import MCPConfig, Tenant
from app.schemas import MCPStandardImportIn


class MCPNameIdentityTests(unittest.TestCase):
    def test_import_rejects_names_that_collide_after_nfkc_normalization(self) -> None:
        with self.assertRaises(ValidationError):
            MCPStandardImportIn.model_validate({
                "mcpServers": {
                    "Firecrawl": {
                        "type": "http",
                        "url": "https://example.test/one",
                    },
                    "ＦＩＲＥＣＲＡＷＬ": {
                        "type": "http",
                        "url": "https://example.test/two",
                    },
                }
            })

    def test_database_rejects_unicode_casefold_duplicates_per_tenant(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as db:
                db.add_all([
                    Tenant(id="tenant-name-a", name="A"),
                    Tenant(id="tenant-name-b", name="B"),
                ])
                db.flush()
                db.add(MCPConfig(
                    tenant_id="tenant-name-a",
                    name=" Firecrawl ",
                    transport="streamable_http",
                    url="https://example.test/mcp",
                ))
                db.commit()
                # The same normalized name remains valid in another tenant.
                db.add(MCPConfig(
                    tenant_id="tenant-name-b",
                    name="ＦＩＲＥＣＲＡＷＬ",
                    transport="streamable_http",
                    url="https://example.test/mcp",
                ))
                db.commit()
                db.add(MCPConfig(
                    tenant_id="tenant-name-a",
                    name="ＦＩＲＥＣＲＡＷＬ",
                    transport="streamable_http",
                    url="https://example.test/other",
                ))
                with self.assertRaises(IntegrityError):
                    db.commit()
                db.rollback()
        finally:
            engine.dispose()

    def test_legacy_name_key_migration_is_idempotent(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "CREATE TABLE mcp_configs ("
                    "id VARCHAR(32) PRIMARY KEY, tenant_id VARCHAR(32), "
                    "name VARCHAR(200) NOT NULL)"
                )
                conn.exec_driver_sql(
                    "INSERT INTO mcp_configs (id, tenant_id, name) VALUES "
                    "('mcp-a', 'tenant-a', ' Firecrawl ')"
                )
            with patch.object(database, "engine", engine):
                database._migrate_mcp_name_identity()
                database._migrate_mcp_name_identity()
            inspector = inspect(engine)
            self.assertIn(
                "name_key",
                {column["name"] for column in inspector.get_columns("mcp_configs")},
            )
            identities = {
                item.get("name") for item in inspector.get_unique_constraints("mcp_configs")
            } | {
                item.get("name") for item in inspector.get_indexes("mcp_configs")
                if item.get("unique")
            }
            self.assertIn("uq_mcp_configs_tenant_name_key", identities)
            with engine.connect() as conn:
                self.assertEqual(
                    conn.exec_driver_sql(
                        "SELECT name_key FROM mcp_configs WHERE id='mcp-a'"
                    ).scalar_one(),
                    "firecrawl",
                )
        finally:
            engine.dispose()

    def test_legacy_duplicate_migration_fails_without_silent_rewrite(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "CREATE TABLE mcp_configs ("
                    "id VARCHAR(32) PRIMARY KEY, tenant_id VARCHAR(32), "
                    "name VARCHAR(200) NOT NULL)"
                )
                conn.exec_driver_sql(
                    "INSERT INTO mcp_configs (id, tenant_id, name) VALUES "
                    "('mcp-a', 'tenant-a', 'Firecrawl'), "
                    "('mcp-b', 'tenant-a', 'ＦＩＲＥＣＲＡＷＬ')"
                )
            with patch.object(database, "engine", engine):
                with self.assertRaisesRegex(RuntimeError, "mcp-a,mcp-b"):
                    database._migrate_mcp_name_identity()
            with engine.connect() as conn:
                names = conn.exec_driver_sql(
                    "SELECT name FROM mcp_configs ORDER BY id"
                ).scalars().all()
            self.assertEqual(names, ["Firecrawl", "ＦＩＲＥＣＲＡＷＬ"])
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
