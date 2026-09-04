"""Helpers for asserting the PostgreSQL Alembic schema contract in unit tests."""
from __future__ import annotations

from functools import lru_cache
from importlib import import_module
from io import StringIO
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BASELINE_MIGRATION = "migrations.versions.20260827_01_initial_governed_postgresql_platform_"
_MIGRATION_MODULES = {
    "20260827_01": _BASELINE_MIGRATION,
    "20260903_07": "migrations.versions.20260903_07_add_resource_ownership_fields",
    "20260903_08": "migrations.versions.20260903_08_harden_email_verification_attempts",
    "20260903_09": "migrations.versions.20260903_09_persist_agent_mcp_session_conversations",
    "20260903_10": "migrations.versions.20260903_10_serialize_agent_mcp_session_turns",
    "20260903_11": "migrations.versions.20260903_11_bind_agent_workflow_runs_to_conversations",
    "20260904_12": "migrations.versions.20260904_12_add_cross_workspace_invitations",
    "20260904_13": "migrations.versions.20260904_13_canonicalize_agent_mcp_conversations",
}


@lru_cache(maxsize=None)
def render_postgresql_upgrade(revision: str) -> str:
    """Render a migration with the PostgreSQL dialect without a live database."""
    module = import_module(_MIGRATION_MODULES[revision])
    output = StringIO()
    context = MigrationContext.configure(
        url="postgresql://",
        opts={"as_sql": True, "output_buffer": output},
    )
    with Operations.context(context):
        module.upgrade()
    return output.getvalue()


def baseline_table_ddl(table_name: str) -> str:
    """Return the baseline ``CREATE TABLE`` statement for a platform table."""
    sql = render_postgresql_upgrade("20260827_01")
    marker = f"CREATE TABLE {table_name} ("
    start = sql.index(marker)
    end = sql.index("\n);\n", start) + len("\n);\n")
    return sql[start:end]


@lru_cache(maxsize=1)
def migration_revisions() -> dict[str, str | tuple[str, ...] | None]:
    """Return the current Alembic revision graph keyed by revision ID."""
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    return {
        str(revision.revision): revision.down_revision
        for revision in script.walk_revisions()
    }


@lru_cache(maxsize=1)
def migration_heads() -> tuple[str, ...]:
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    return tuple(ScriptDirectory.from_config(config).get_heads())
