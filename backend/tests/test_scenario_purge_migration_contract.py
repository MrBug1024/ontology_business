"""Static contract checks for the scenario-audit purge hardening revision."""
from __future__ import annotations

from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    BACKEND_ROOT
    / "migrations"
    / "versions"
    / "20260912_33_harden_scenario_audit_purge_tenant.py"
)


def test_scenario_audit_purge_revision_fences_evidence_by_tenant() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "20260912_33"' in source
    assert 'down_revision: Union[str, None] = "20260912_32"' in source
    assert "CREATE OR REPLACE FUNCTION public.purge_retired_scenario_audit" in source
    assert "WHERE evidence.tenant_id = p_tenant_id" in source
    assert "evidence.action_scenario_id = p_scenario_id" in source
    assert "SET search_path = pg_catalog, public" in source
    assert "REVOKE ALL ON FUNCTION public.purge_retired_scenario_audit" in source


def test_runtime_schema_revision_matches_latest_migration() -> None:
    source = (BACKEND_ROOT / "app" / "database.py").read_text(encoding="utf-8")
    assert 'POSTGRESQL_SCHEMA_REVISION = "20260912_33"' in source
