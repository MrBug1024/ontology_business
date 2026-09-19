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
    "distillation_publications",
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
    "distillation_projects",
    "distillation_conversation_turns",
    "distillation_attachments",
)
RUNTIME_APPEND_ONLY_TABLES = ("agent_turn_events", "release_lifecycle_events", "workflow_approval_evidence", "distillation_turn_attachments")
RUNTIME_MUTABLE_CONTROL_TABLES = (
    "agent_turn_runs",
    "assistant_request_runs",
    "managed_upload_runs",
)
_TABLE_PRIVILEGES = (
    "select",
    "insert",
    "update",
    "delete",
    "truncate",
    "references",
    "trigger",
)

_UNSAFE_RUNTIME_ROLE_FLAGS = (
    "rolsuper",
    "rolcreaterole",
    "rolcreatedb",
    "rolinherit",
    "rolbypassrls",
    "rolreplication",
)

# Revisions 31 and 32 make managed runtime inputs explicitly Agent-scoped.
# Keep this contract here (rather than relying only on ORM metadata) so a
# deployment with a stale or partially-applied migration fails closed before
# it can accept an attachment or connector reference.
_AGENT_SCOPE_COLUMNS: dict[str, frozenset[str]] = {
    "data_assets": frozenset({"owner_agent_id"}),
    "managed_upload_runs": frozenset({"owner_agent_id"}),
    "data_sources": frozenset({"owner_agent_id", "resource_scope"}),
}
_AGENT_SCOPE_INDEXES = frozenset(
    {
        "ix_data_assets_owner_agent_id",
        "ix_managed_upload_runs_owner_agent_id",
        "ix_data_sources_owner_agent_id",
        "ix_data_sources_resource_scope",
    }
)
_AGENT_SCOPE_CONSTRAINTS: dict[str, dict[str, str | None]] = {
    "fk_data_assets_owner_agent_tenant": {
        "table_name": "data_assets",
        "constraint_type": "f",
        "delete_action": "r",
    },
    "fk_managed_upload_runs_owner_agent_tenant": {
        "table_name": "managed_upload_runs",
        "constraint_type": "f",
        "delete_action": "r",
    },
    "fk_data_sources_owner_agent_tenant": {
        "table_name": "data_sources",
        "constraint_type": "f",
        "delete_action": "r",
    },
    "ck_data_sources_resource_scope": {
        "table_name": "data_sources",
        "constraint_type": "c",
        "delete_action": None,
    },
    "ck_data_sources_owner_scope": {
        "table_name": "data_sources",
        "constraint_type": "c",
        "delete_action": None,
    },
}


def _validate_agent_scope_columns(
    columns: dict[str, set[str] | frozenset[str]],
) -> None:
    missing_columns = {
        table_name: sorted(set(required) - set(columns.get(table_name, ())))
        for table_name, required in _AGENT_SCOPE_COLUMNS.items()
        if set(required) - set(columns.get(table_name, ()))
    }
    if missing_columns:
        details = "; ".join(
            f"{table_name}: {', '.join(names)}"
            for table_name, names in sorted(missing_columns.items())
        )
        raise RuntimeError("Agent scope columns are incomplete: " + details)


def _validate_agent_scope_indexes(indexes: set[str] | frozenset[str]) -> None:
    missing_indexes = sorted(set(_AGENT_SCOPE_INDEXES) - set(indexes))
    if missing_indexes:
        raise RuntimeError(
            "Agent scope indexes are incomplete: " + ", ".join(missing_indexes)
        )


def _validate_agent_scope_constraints(
    constraints: dict[str, dict[str, Any]],
) -> None:
    missing_constraints = sorted(
        set(_AGENT_SCOPE_CONSTRAINTS) - set(constraints)
    )
    if missing_constraints:
        raise RuntimeError(
            "Agent scope constraints are incomplete: "
            + ", ".join(missing_constraints)
        )

    for name, expected in _AGENT_SCOPE_CONSTRAINTS.items():
        current = constraints[name]
        if str(current.get("table_name") or "") != expected["table_name"]:
            raise RuntimeError(f"Agent scope constraint {name} is on the wrong table")
        if str(current.get("constraint_type") or "") != expected["constraint_type"]:
            raise RuntimeError(f"Agent scope constraint {name} has the wrong type")
        if expected["delete_action"] is not None and str(
            current.get("delete_action") or ""
        ) != expected["delete_action"]:
            raise RuntimeError(
                f"Agent scope constraint {name} must use ON DELETE RESTRICT"
            )

        definition = str(current.get("definition") or "").casefold()
        if name == "ck_data_sources_resource_scope":
            required_markers = ("resource_scope", "modeling", "agent_runtime")
        elif name == "ck_data_sources_owner_scope":
            required_markers = (
                "owner_agent_id",
                "tenant_id",
                "agent_runtime",
            )
        else:
            required_markers = (
                "owner_agent_id",
                "tenant_id",
                "agents",
            )
        if any(marker not in definition for marker in required_markers):
            raise RuntimeError(
                f"Agent scope constraint {name} does not enforce the expected scope"
            )


def _validate_agent_scope_snapshot(
    columns: dict[str, set[str] | frozenset[str]],
    *,
    indexes: set[str] | frozenset[str],
    constraints: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Validate the durable ownership contract introduced by rev31/32.

    The function deliberately accepts a detached metadata snapshot so tests
    can exercise every fail-closed branch without requiring a live PostgreSQL
    instance.  ``pg_constraint.confdeltype`` uses ``r`` for RESTRICT; a
    different action would allow deleting an Agent to silently discard or
    reassign its runtime inputs.
    """

    _validate_agent_scope_columns(columns)
    _validate_agent_scope_indexes(indexes)
    _validate_agent_scope_constraints(constraints)
    return {
        "revision": "20260912_32",
        "scoped_tables": len(_AGENT_SCOPE_COLUMNS),
        "scoped_indexes": len(_AGENT_SCOPE_INDEXES),
        "scoped_constraints": len(_AGENT_SCOPE_CONSTRAINTS),
    }


def _verify_agent_scope_contract(connection: Any) -> dict[str, Any]:
    """Read and validate the PostgreSQL metadata required by rev31/32."""

    table_names = tuple(_AGENT_SCOPE_COLUMNS)
    column_rows = connection.exec_driver_sql(
        """
        SELECT table_name, column_name
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name IN ('data_assets', 'managed_upload_runs', 'data_sources')
        """
    ).all()
    columns: dict[str, set[str]] = {table_name: set() for table_name in table_names}
    for row in column_rows:
        table_name, column_name = str(row[0]), str(row[1])
        if table_name in columns:
            columns[table_name].add(column_name)

    index_rows = connection.exec_driver_sql(
        """
        SELECT indexname
          FROM pg_indexes
         WHERE schemaname = 'public'
           AND indexname IN (
               'ix_data_assets_owner_agent_id',
               'ix_managed_upload_runs_owner_agent_id',
               'ix_data_sources_owner_agent_id',
               'ix_data_sources_resource_scope'
           )
        """
    ).all()
    indexes = {str(row[0]) for row in index_rows}

    constraint_rows = connection.exec_driver_sql(
        """
        SELECT table_row.relname AS table_name,
               constraint_row.conname,
               constraint_row.contype,
               constraint_row.confdeltype,
               pg_get_constraintdef(constraint_row.oid) AS definition
          FROM pg_constraint AS constraint_row
          JOIN pg_class AS table_row
            ON table_row.oid = constraint_row.conrelid
          JOIN pg_namespace AS namespace_row
            ON namespace_row.oid = table_row.relnamespace
         WHERE namespace_row.nspname = 'public'
           AND constraint_row.conname IN (
               'fk_data_assets_owner_agent_tenant',
               'fk_managed_upload_runs_owner_agent_tenant',
               'fk_data_sources_owner_agent_tenant',
               'ck_data_sources_resource_scope',
               'ck_data_sources_owner_scope'
           )
        """
    ).all()
    constraints = {
        str(row[1]): {
            "table_name": str(row[0]),
            "constraint_type": str(row[2]),
            "delete_action": str(row[3]) if row[3] is not None else None,
            "definition": str(row[4] or ""),
        }
        for row in constraint_rows
    }
    return _validate_agent_scope_snapshot(
        columns,
        indexes=indexes,
        constraints=constraints,
    )


def _verify_scenario_audit_purge_contract(connection: Any) -> None:
    """Ensure the privileged scenario purge function keeps its tenant fence."""
    row = connection.exec_driver_sql(
        """
        SELECT procedure.prosecdef,
               procedure.proconfig,
               pg_get_functiondef(procedure.oid) AS definition,
               EXISTS (
                   SELECT 1
                     FROM aclexplode(
                         COALESCE(
                             procedure.proacl,
                             acldefault('f', procedure.proowner)
                         )
                     ) AS acl
                    WHERE acl.grantee = 0
                      AND acl.privilege_type = 'EXECUTE'
               ) AS public_execute
          FROM pg_proc AS procedure
         WHERE procedure.oid = to_regprocedure(
             'public.purge_retired_scenario_audit(character varying, character varying)'
         )
        """
    ).one_or_none()
    if row is None or not bool(row[0]):
        raise RuntimeError("tenant-fenced scenario audit purge function is missing")
    if bool(row[3]):
        raise RuntimeError("scenario audit purge function is executable by PUBLIC")
    if "search_path=pg_catalog, public" not in set(row[1] or []):
        raise RuntimeError("scenario audit purge function search_path is not fixed")
    definition = str(row[2] or "").casefold()
    required_markers = (
        "evidence.tenant_id = p_tenant_id",
        "evidence.action_scenario_id = p_scenario_id",
    )
    if any(marker not in definition for marker in required_markers):
        raise RuntimeError("scenario audit purge function is missing its tenant fence")


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
    append_only_tables: tuple[str, ...] = (),
    mutable_control_tables: tuple[str, ...] = (),
) -> dict[str, Any]:
    expected = (
        set(immutable_tables)
        | set(ledger_tables)
        | set(required_update_tables)
        | set(append_only_tables)
        | set(mutable_control_tables)
    )
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
    for table_name in append_only_tables:
        current = privileges[table_name]
        if not current.get("select", False) or not current.get("insert", False):
            raise RuntimeError(
                f"runtime role lacks append-only access to {table_name}"
            )
        if any(
            current.get(name, False)
            for name in ("update", "delete", "truncate", "references", "trigger")
        ):
            raise RuntimeError(
                f"runtime role can mutate append-only state {table_name}"
            )
    for table_name in mutable_control_tables:
        current = privileges[table_name]
        if not all(current.get(name, False) for name in ("select", "insert", "update")):
            raise RuntimeError(
                f"runtime role lacks mutable control access to {table_name}"
            )
        # Revision 34 permits lineage cleanup for this table only.
        expected_delete = table_name == "assistant_request_runs"
        if bool(current.get("delete", False)) != expected_delete:
            raise RuntimeError(f"runtime cleanup privilege differs for {table_name}")
        if any(
            current.get(name, False)
            for name in ("truncate", "references", "trigger")
        ):
            raise RuntimeError(
                f"runtime role has excessive retained-control privileges on {table_name}"
            )
    return {
        "immutable_tables": len(immutable_tables),
        "ledger_tables": len(ledger_tables),
        "required_update_tables": len(required_update_tables),
        "append_only_tables": len(append_only_tables),
        "mutable_control_tables": len(mutable_control_tables),
    }


def _verify_runtime_table_privileges(
    connection: Any,
    *,
    immutable_tables: tuple[str, ...],
    ledger_tables: tuple[str, ...],
    required_update_tables: tuple[str, ...],
    append_only_tables: tuple[str, ...] = (),
    mutable_control_tables: tuple[str, ...] = (),
) -> dict[str, Any]:
    table_names = tuple(
        dict.fromkeys(
            (
                *immutable_tables,
                *ledger_tables,
                *required_update_tables,
                *append_only_tables,
                *mutable_control_tables,
            )
        )
    )
    privileges: dict[str, dict[str, bool]] = {}
    for table_name in table_names:
        qualified = f"public.{table_name}"
        row = connection.exec_driver_sql(
            "SELECT "
            + ", ".join(
                "has_table_privilege(current_user, %s, '"
                + privilege.upper()
                + "')"
                for privilege in _TABLE_PRIVILEGES
            ),
            (qualified,) * len(_TABLE_PRIVILEGES),
        ).one()
        privileges[table_name] = {
            privilege: bool(row[index])
            for index, privilege in enumerate(_TABLE_PRIVILEGES)
        }
    return _validate_runtime_table_privileges(
        privileges,
        immutable_tables=immutable_tables,
        ledger_tables=ledger_tables,
        required_update_tables=required_update_tables,
        append_only_tables=append_only_tables,
        mutable_control_tables=mutable_control_tables,
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


def _verify_access_governance_privileges(connection: Any) -> None:
    expected = {
        "access_audit_events": {"select", "insert"},
        "access_governance_guard": {"select", "update"},
        "workspace_invitations": {"select", "insert", "update", "delete"},
        "auth_rate_limits": {"select", "insert", "update", "delete"},
    }
    for table, allowed in expected.items():
        for privilege in _TABLE_PRIVILEGES:
            granted = connection.exec_driver_sql(
                "SELECT has_table_privilege(current_user, %s, %s)",
                (f"public.{table}", privilege.upper()),
            ).scalar_one()
            if bool(granted) != (privilege in allowed):
                raise RuntimeError(f"runtime access-governance privileges differ for {table}")


def _verify_capability_status_storage(connection: Any) -> int:
    from app.models import CapabilityInvocation

    expected = CapabilityInvocation.__table__.c.status.type.length
    capacity = connection.exec_driver_sql(
        "SELECT character_maximum_length FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'capability_invocations' "
        "AND column_name = 'status'"
    ).scalar_one()
    if capacity is None or capacity < expected:
        raise RuntimeError("capability invocation status storage is too narrow")
    return int(capacity)


def _validate_external_asset_privileges(privileges: dict[str, bool]) -> None:
    allowed = {"select", "insert", "delete"}
    for privilege in _TABLE_PRIVILEGES:
        if bool(privileges.get(privilege)) != (privilege in allowed):
            raise RuntimeError(
                f"runtime external_scenario_assets {privilege.upper()} privilege is incorrect"
            )


def _verify_external_scenario_contract(connection: Any) -> dict[str, Any]:
    """Verify rev35 ownership grants and validated tenant/scenario constraints."""
    row = connection.exec_driver_sql(
        "SELECT " + ", ".join(
            "has_table_privilege(current_user, 'public.external_scenario_assets', '"
            + privilege.upper() + "')" for privilege in _TABLE_PRIVILEGES
        )
    ).one()
    _validate_external_asset_privileges({
        privilege: bool(row[index]) for index, privilege in enumerate(_TABLE_PRIVILEGES)
    })
    expected = {
        "ck_external_api_keys_bound_active": (
            "external_api_keys", "CHECK (status <> 'active' OR scenario_id IS NOT NULL)"),
        "fk_external_api_keys_scenario_tenant": (
            "external_api_keys", "FOREIGN KEY (scenario_id, tenant_id) "
            "REFERENCES business_scenarios(id, tenant_id) ON DELETE CASCADE"),
        "fk_external_scenario_assets_asset": (
            "external_scenario_assets", "FOREIGN KEY (asset_id, tenant_id) "
            "REFERENCES data_assets(id, tenant_id) ON DELETE CASCADE"),
        "fk_external_scenario_assets_scenario": (
            "external_scenario_assets", "FOREIGN KEY (scenario_id, tenant_id) "
            "REFERENCES business_scenarios(id, tenant_id) ON DELETE RESTRICT"),
    }
    rows = connection.exec_driver_sql(
        """
        SELECT relation.relname, constraint_row.conname, constraint_row.convalidated,
               pg_get_constraintdef(constraint_row.oid)
          FROM pg_constraint AS constraint_row
          JOIN pg_class AS relation ON relation.oid = constraint_row.conrelid
          JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
         WHERE namespace.nspname = 'public'
           AND constraint_row.conname IN (%s, %s, %s, %s)
        """, tuple(expected),
    ).all()
    actual = {str(row[1]): row for row in rows}

    def normalized(definition: str) -> str:
        value = definition.casefold().replace("::text", "").replace("public.", "")
        return "".join(value.split()).replace("(", "").replace(")", "")

    for name, (table, definition) in expected.items():
        current = actual.get(name)
        if (current is None or str(current[0]) != table or not current[2]
                or normalized(str(current[3])) != normalized(definition)):
            raise RuntimeError(f"external scenario constraint {name} is missing or incorrect")
    return {"ownership_privileges": "select_insert_delete", "validated_constraints": len(expected)}


def _verify_distillation_conversation_contract(connection: Any) -> dict[str, Any]:
    """Read-only checks of durable ownership, request uniqueness and active claim exclusion."""
    privileges = connection.exec_driver_sql("SELECT " + ", ".join(
        "has_table_privilege(current_user, 'public.distillation_conversation_turns', '" + item.upper() + "')"
        for item in _TABLE_PRIVILEGES)).one()
    for index, privilege in enumerate(_TABLE_PRIVILEGES):
        if bool(privileges[index]) != (privilege in {"select", "insert", "update"}):
            raise RuntimeError(f"runtime distillation conversation {privilege.upper()} privilege is incorrect")
    expected = {
        "fk_distillation_turn_project_tenant": ("f", "FOREIGN KEY (project_id, tenant_id) REFERENCES distillation_projects(id, tenant_id) ON DELETE RESTRICT"),
        "uq_distillation_turn_request": ("u", "UNIQUE (project_id, request_id)"),
        "uq_distillation_turn_number": ("u", "UNIQUE (project_id, turn_number)"),
        "ck_distillation_turn_status": ("c", None),
        "ck_distillation_turn_counters": ("c", None),
    }
    rows = connection.exec_driver_sql("""
        SELECT conname, contype, convalidated, pg_get_constraintdef(oid)
        FROM pg_constraint WHERE conrelid = 'public.distillation_conversation_turns'::regclass
    """).all()
    actual = {str(row[0]): row for row in rows}
    normalize = lambda value: "".join(value.replace("public.", "").split()).casefold()
    for name, (kind, definition) in expected.items():
        row = actual.get(name)
        if row is None or str(row[1]) != kind or not row[2] or (definition and normalize(row[3]) != normalize(definition)):
            raise RuntimeError(f"distillation conversation constraint {name} is missing or incorrect")
    index = connection.exec_driver_sql("""
        SELECT i.indisunique, i.indisvalid, pg_get_indexdef(i.indexrelid, 1, true), pg_get_expr(i.indpred, i.indrelid)
        FROM pg_index AS i JOIN pg_class AS c ON c.oid = i.indexrelid
        WHERE i.indrelid = 'public.distillation_conversation_turns'::regclass
          AND c.relname = 'uq_distillation_turn_active'
    """).one_or_none()
    predicate = "((status)::text = ANY ((ARRAY['queued'::character varying, 'running'::character varying])::text[]))"
    if index is None or not index[0] or not index[1] or index[2] != "project_id" or normalize(index[3] or "") != normalize(predicate):
        raise RuntimeError("distillation active turn exclusion index is missing or incorrect")
    return {"privileges": "select_insert_update", "validated_constraints": len(expected), "active_turn_unique": True}


def main() -> int:
    from app.config import get_settings
    from app.database import engine, init_db
    from app.services import cache_service, object_storage_service
    from scripts.verify_distillation_storage import verify_attachment_contract, verify_discovery_contract

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
            append_only_tables=RUNTIME_APPEND_ONLY_TABLES,
            mutable_control_tables=RUNTIME_MUTABLE_CONTROL_TABLES,
        )
        agent_scope = _verify_agent_scope_contract(connection)
        external_scenario_scope = _verify_external_scenario_contract(connection)
        distillation_conversation = _verify_distillation_conversation_contract(connection)
        distillation_attachments = verify_attachment_contract(connection)
        distillation_discovery = verify_discovery_contract(connection)
        _verify_scenario_audit_purge_contract(connection)
        _verify_runtime_function_privileges(connection)
        _verify_access_governance_privileges(connection)
        status_capacity = _verify_capability_status_storage(connection)

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
                    "capability_status_capacity": status_capacity,
                    "role": role,
                    "table_privileges": table_privileges,
                    "agent_scope": agent_scope,
                    "distillation_conversation": distillation_conversation,
                    "distillation_attachments": distillation_attachments,
                    "distillation_discovery": distillation_discovery,
                    "external_scenario_scope": external_scenario_scope,
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
