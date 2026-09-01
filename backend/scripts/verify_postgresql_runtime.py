"""Run read-only deployment checks for the PostgreSQL/MinIO runtime."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


RUNTIME_IMMUTABLE_TABLES = (
    "data_asset_versions",
    "dataset_schemas",
    "dataset_relations",
    "dataset_fields",
    "dataset_versions",
    "dataset_version_assets",
    "dataset_fragments",
    "ingestion_run_inputs",
    "dataset_lineage_edges",
    "reasoning_terms",
    "derivation_run_inputs",
    "assertions",
    "derivation_evidence",
)
RUNTIME_MIGRATION_LEDGER_TABLES = (
    "alembic_version",
    "platform_migration_runs",
    "platform_migration_checkpoints",
)
RUNTIME_REQUIRED_UPDATE_TABLES = (
    "dataset_heads",
    "ingestion_runs",
    "derivation_runs",
)

_UNSAFE_RUNTIME_ROLE_FLAGS = (
    "rolsuper",
    "rolcreaterole",
    "rolcreatedb",
    "rolinherit",
    "rolbypassrls",
    "rolreplication",
)


def _validate_runtime_role_snapshot(
    role: dict[str, Any] | None,
    *,
    expected_role: str,
    memberships: tuple[str, ...],
    can_create_public: bool,
) -> dict[str, Any]:
    if not expected_role:
        raise RuntimeError("POSTGRESQL_USER is empty; runtime identity is ambiguous")
    if role is None:
        raise RuntimeError("current PostgreSQL role is not visible in pg_roles")
    current_role = str(role.get("current_user") or "")
    if current_role != expected_role:
        raise RuntimeError(
            f"PostgreSQL current_user mismatch: expected={expected_role!r}, "
            f"actual={current_role!r}"
        )
    if not bool(role.get("rolcanlogin")):
        raise RuntimeError("PostgreSQL runtime role cannot login")
    unsafe = [flag for flag in _UNSAFE_RUNTIME_ROLE_FLAGS if bool(role.get(flag))]
    if unsafe:
        raise RuntimeError(
            "PostgreSQL runtime role has unsafe flags: " + ", ".join(unsafe)
        )
    if memberships:
        raise RuntimeError(
            "PostgreSQL runtime role has role memberships: "
            + ", ".join(sorted(memberships))
        )
    if can_create_public:
        raise RuntimeError("PostgreSQL runtime role can CREATE in public schema")
    return {
        "current_user": current_role,
        "unsafe_role_flags": [],
        "role_memberships": [],
        "public_schema_create": False,
    }


def _verify_runtime_role(connection: Any, *, expected_role: str) -> dict[str, Any]:
    role = connection.exec_driver_sql(
        """
        SELECT current_user AS current_user,
               r.rolsuper, r.rolcreaterole, r.rolcreatedb, r.rolcanlogin,
               r.rolinherit, r.rolbypassrls, r.rolreplication
          FROM pg_roles AS r
         WHERE r.rolname = current_user
        """
    ).mappings().one_or_none()
    memberships = tuple(
        str(row[0])
        for row in connection.exec_driver_sql(
            """
            SELECT granted.rolname
              FROM pg_auth_members AS membership
              JOIN pg_roles AS member ON member.oid = membership.member
              JOIN pg_roles AS granted ON granted.oid = membership.roleid
             WHERE member.rolname = current_user
             ORDER BY granted.rolname
            """
        ).all()
    )
    can_create_public = bool(
        connection.exec_driver_sql(
            "SELECT has_schema_privilege(current_user, 'public', 'CREATE')"
        ).scalar_one()
    )
    return _validate_runtime_role_snapshot(
        dict(role) if role is not None else None,
        expected_role=expected_role,
        memberships=memberships,
        can_create_public=can_create_public,
    )


def _validate_runtime_table_privileges(
    privileges: dict[str, dict[str, bool]],
    *,
    immutable_tables: tuple[str, ...],
    ledger_tables: tuple[str, ...],
    required_update_tables: tuple[str, ...],
) -> dict[str, Any]:
    expected = set(immutable_tables) | set(ledger_tables) | set(required_update_tables)
    missing = sorted(expected - set(privileges))
    if missing:
        raise RuntimeError("runtime privilege snapshot missing tables: " + ", ".join(missing))
    for table_name in sorted(expected):
        if not privileges[table_name].get("select", False):
            raise RuntimeError(f"runtime role cannot SELECT {table_name}")
    for table_name in immutable_tables:
        current = privileges[table_name]
        if not current.get("insert", False):
            raise RuntimeError(f"runtime role cannot INSERT immutable {table_name}")
        if current.get("update", False) or current.get("delete", False):
            raise RuntimeError(f"runtime role can mutate immutable {table_name}")
    for table_name in ledger_tables:
        current = privileges[table_name]
        if any(current.get(name, False) for name in ("insert", "update", "delete")):
            raise RuntimeError(f"runtime role can mutate migration ledger {table_name}")
    for table_name in required_update_tables:
        if not privileges[table_name].get("update", False):
            raise RuntimeError(f"runtime role cannot UPDATE workflow state {table_name}")
    return {
        "immutable_tables": len(immutable_tables),
        "ledger_tables": len(ledger_tables),
        "required_update_tables": len(required_update_tables),
    }


def _verify_runtime_table_privileges(
    connection: Any,
    *,
    immutable_tables: tuple[str, ...],
    ledger_tables: tuple[str, ...],
    required_update_tables: tuple[str, ...],
) -> dict[str, Any]:
    table_names = tuple(
        dict.fromkeys((*immutable_tables, *ledger_tables, *required_update_tables))
    )
    privileges: dict[str, dict[str, bool]] = {}
    for table_name in table_names:
        qualified = f"public.{table_name}"
        row = connection.exec_driver_sql(
            "SELECT has_table_privilege(current_user, %s, 'SELECT'), "
            "has_table_privilege(current_user, %s, 'INSERT'), "
            "has_table_privilege(current_user, %s, 'UPDATE'), "
            "has_table_privilege(current_user, %s, 'DELETE')",
            (qualified, qualified, qualified, qualified),
        ).one()
        privileges[table_name] = {
            "select": bool(row[0]),
            "insert": bool(row[1]),
            "update": bool(row[2]),
            "delete": bool(row[3]),
        }
    return _validate_runtime_table_privileges(
        privileges,
        immutable_tables=immutable_tables,
        ledger_tables=ledger_tables,
        required_update_tables=required_update_tables,
    )


def _verify_runtime_function_privileges(connection: Any) -> None:
    """Ensure the runtime role can invoke only governed cleanup functions."""
    signatures = (
        (
            "public.purge_retired_scenario_audit(varchar,varchar)",
            "public.purge_retired_scenario_audit",
        ),
        (
            "public.detach_data_source_file_references(varchar,varchar,varchar[])",
            "public.detach_data_source_file_references",
        ),
    )
    for signature, label in signatures:
        executable = connection.exec_driver_sql(
            "SELECT has_function_privilege(current_user, %s, 'EXECUTE')",
            (signature,),
        ).scalar_one()
        if not bool(executable):
            raise RuntimeError(f"runtime role cannot EXECUTE {label}")


def main() -> int:
    from app.config import get_settings
    from app.database import engine, init_db
    from app.services import cache_service, object_storage_service

    settings = get_settings()
    init_db()
    with engine.connect() as connection:
        role = _verify_runtime_role(
            connection,
            expected_role=settings.postgresql_user.strip(),
        )
        table_privileges = _verify_runtime_table_privileges(
            connection,
            immutable_tables=RUNTIME_IMMUTABLE_TABLES,
            ledger_tables=RUNTIME_MIGRATION_LEDGER_TABLES,
            required_update_tables=RUNTIME_REQUIRED_UPDATE_TABLES,
        )
        _verify_runtime_function_privileges(connection)

    if not object_storage_service.is_configured():
        raise RuntimeError("MinIO configuration is incomplete")
    if not object_storage_service.healthcheck():
        raise RuntimeError("MinIO health check failed")

    redis_status = "not_configured"
    if settings.redis_configured:
        if not cache_service.healthcheck():
            raise RuntimeError("Redis health check failed")
        redis_status = "healthy"

    print(
        json.dumps(
            {
                "postgresql": {
                    "schema": "current",
                    "role": role,
                    "table_privileges": table_privileges,
                    "governed_functions": "executable",
                },
                "minio": "healthy",
                "redis": redis_status,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
