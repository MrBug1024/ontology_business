from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import ValidationError

from app.database import Base
from app.models import MCPConfig, Tenant
from app.schemas import MCPStandardImportIn
from tests.postgresql_migration_contracts import baseline_table_ddl


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

    def test_postgresql_baseline_declares_tenant_name_identity(self) -> None:
        ddl = baseline_table_ddl("mcp_configs")
        self.assertIn("name_key VARCHAR(600) NOT NULL", ddl)
        self.assertIn(
            "CONSTRAINT uq_mcp_configs_tenant_name_key UNIQUE (tenant_id, name_key)",
            ddl,
        )


if __name__ == "__main__":
    unittest.main()
