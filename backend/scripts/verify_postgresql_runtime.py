"""Run non-destructive acceptance checks for the PostgreSQL/MinIO cutover."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

from sqlalchemy import func, inspect, select


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


BOOKKEEPING_SCENARIO_ID = "56e2006148e8499e8599f5c7c8145e60"
MEDICAL_SCENARIO_ID = "cc5d3ff36d2a468596dfa9f8ef2995da"
BOOKKEEPING_SOURCE_ID = "68fcb44b941a40d48c7aba1efb14e7f6"
MEDICAL_SOURCE_ID = "a2d20a398ed744e7839acb910f377d6a"


def _quote(identifier: str) -> str:
    return f'"{identifier.replace(chr(34), chr(34) * 2)}"'


def _scalar(result: dict[str, Any], column: str) -> Any:
    if result.get("row_count") != 1 or column not in result.get("columns", []):
        raise RuntimeError(f"query did not return one {column} value")
    index = result["columns"].index(column)
    return result["rows"][0][index]


def _assert_credential_free(config: Any) -> None:
    serialized = json.dumps(config or {}, ensure_ascii=False, sort_keys=True).casefold()
    forbidden = ("password", "secret", "credential", "access_key", "token")
    if any(key in serialized for key in forbidden):
        raise RuntimeError("dataset connector contains a credential-like key")


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


def main() -> int:
    from app.config import get_settings
    from app.database import SessionLocal, engine, init_db
    from app.models import (
        BusinessScenario,
        DataSource,
        DatasetFragment,
        LogicalDataset,
    )
    from app.services import (
        cache_service,
        datasource_service,
        medical_audit_service,
        runtime_definition_service,
    )
    from scripts.migrate_mysql_to_postgresql import (
        BOOKKEEPING_DERIVED_RELATIONS,
        BOOKKEEPING_TABLES,
        MEDICAL_DERIVED_RELATIONS,
        MEDICAL_TABLES,
        RUNTIME_IMMUTABLE_TABLES,
        RUNTIME_MIGRATION_LEDGER_TABLES,
        RUNTIME_REQUIRED_UPDATE_TABLES,
    )

    init_db()
    runtime_settings = get_settings()
    with engine.connect() as connection:
        runtime_role = _verify_runtime_role(
            connection,
            expected_role=runtime_settings.postgresql_user.strip(),
        )
        runtime_role["table_privileges"] = _verify_runtime_table_privileges(
            connection,
            immutable_tables=RUNTIME_IMMUTABLE_TABLES,
            ledger_tables=RUNTIME_MIGRATION_LEDGER_TABLES,
            required_update_tables=RUNTIME_REQUIRED_UPDATE_TABLES,
        )
    catalog_inspector = inspect(engine)
    physical_relations = set(catalog_inspector.get_table_names(schema="public"))
    physical_relations.update(catalog_inspector.get_view_names(schema="public"))
    get_materialized_views = getattr(
        catalog_inspector, "get_materialized_view_names", None
    )
    if get_materialized_views is not None:
        physical_relations.update(get_materialized_views(schema="public"))
    business_relation_names = {
        *BOOKKEEPING_TABLES,
        *MEDICAL_TABLES,
        *BOOKKEEPING_DERIVED_RELATIONS,
        *MEDICAL_DERIVED_RELATIONS,
    }
    leaked_business_relations = sorted(physical_relations & business_relation_names)
    if leaked_business_relations:
        raise RuntimeError(
            "business relations leaked into PostgreSQL: "
            + ", ".join(leaked_business_relations)
        )

    cache_key = "migration:postgresql-runtime-e2e:v1"
    with SessionLocal() as session:
        scenario_ids = set(session.scalars(select(BusinessScenario.id)))
        if scenario_ids != {BOOKKEEPING_SCENARIO_ID, MEDICAL_SCENARIO_ID}:
            raise RuntimeError("PostgreSQL does not contain exactly the retained scenarios")

        bookkeeping_source = session.get(DataSource, BOOKKEEPING_SOURCE_ID)
        medical_source = session.get(DataSource, MEDICAL_SOURCE_ID)
        if bookkeeping_source is None or medical_source is None:
            raise RuntimeError("retained dataset connector is missing")
        for source in (bookkeeping_source, medical_source):
            if source.type != "dataset" or source.scenario_id is not None:
                raise RuntimeError("legacy SQL source was not converted to a shared dataset")
            _assert_credential_free(source.config)
            ok, _message = datasource_service.test_connection(source)
            if not ok:
                raise RuntimeError(f"dataset connector {source.id} is not queryable")

        bookkeeping_tables = datasource_service.list_tables(bookkeeping_source)
        medical_tables = datasource_service.list_tables(medical_source)
        if len(bookkeeping_tables) != 16 or len(medical_tables) != 6:
            raise RuntimeError("dataset relation inventory is incomplete")

        project_result = datasource_service.run_query(
            bookkeeping_source,
            'SELECT project_id, company_name FROM "audit_project_view" '
            "ORDER BY project_id",
            limit=10,
        )
        if project_result["row_count"] != 2:
            raise RuntimeError("bookkeeping derived view returned an unexpected row count")

        medical_scenario = session.get(BusinessScenario, MEDICAL_SCENARIO_ID)
        if medical_scenario is None:
            raise RuntimeError("medical scenario is missing")
        definition = runtime_definition_service.resolve_active(
            session, medical_scenario, environment="dev"
        )
        contract = medical_audit_service.resolve_mapping_contract(
            [medical_source], list(definition.mappings.values()), definition=definition
        )
        charge_table = contract.tables["charge"]
        service_column = contract.columns["charge"]["service_name"]
        top_service = datasource_service.run_query(
            medical_source,
            (
                f"SELECT {_quote(service_column)} AS service_name, COUNT(*) AS n "
                f"FROM {_quote(charge_table)} "
                f"WHERE {_quote(service_column)} IS NOT NULL "
                f"AND TRIM(CAST({_quote(service_column)} AS VARCHAR)) <> '' "
                f"GROUP BY {_quote(service_column)} "
                "ORDER BY n DESC, service_name ASC LIMIT 1"
            ),
            limit=1,
        )
        service_name = str(_scalar(top_service, "service_name"))
        audit = medical_audit_service.run_medical_audit(
            contract,
            {
                "strategy": "charge_threshold",
                "service_name": service_name,
                "threshold": 0,
                "limit": 3,
                "offset": 0,
            },
            include_sensitive=True,
            property_access=medical_audit_service.access_policy(
                sorted(medical_audit_service._ALL_MEDICAL_PROPERTIES)
            ),
        )
        if not audit.get("ok") or int(audit["summary"]["violation_count"]) <= 0:
            raise RuntimeError("medical audit did not return deterministic evidence")

        dataset_count = int(
            session.scalar(select(func.count()).select_from(LogicalDataset)) or 0
        )
        fragment_count = int(
            session.scalar(select(func.count()).select_from(DatasetFragment)) or 0
        )

    cache_service.delete(cache_key)
    if not cache_service.healthcheck():
        raise RuntimeError("Redis cache is not reachable")
    cache_payload = {"status": "ok", "authoritative": False}
    if not cache_service.set_json(cache_key, cache_payload, ttl_seconds=60):
        raise RuntimeError("Redis cache write failed")
    if cache_service.get_json(cache_key) != cache_payload:
        raise RuntimeError("Redis cache round trip failed")
    cache_service.delete(cache_key)
    if cache_service.get_json(cache_key) is not None:
        raise RuntimeError("Redis cache cleanup failed")

    print(
        json.dumps(
            {
                "postgresql_runtime": "ok",
                "postgresql_current_user": runtime_role["current_user"],
                "postgresql_public_schema_create": runtime_role[
                    "public_schema_create"
                ],
                "postgresql_business_relation_count": 0,
                "scenario_count": 2,
                "logical_dataset_count": dataset_count,
                "fragment_count": fragment_count,
                "bookkeeping_relation_count": len(bookkeeping_tables),
                "bookkeeping_project_rows": project_result["row_count"],
                "medical_relation_count": len(medical_tables),
                "medical_audit_violation_count": int(
                    audit["summary"]["violation_count"]
                ),
                "medical_audit_page_rows": audit["row_count"],
                "redis_round_trip": "ok",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
