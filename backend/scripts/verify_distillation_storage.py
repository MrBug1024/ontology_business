"""Read-only deployment checks for temporary conversation input ownership."""
from __future__ import annotations

from typing import Any


def verify_attachment_contract(connection: Any) -> dict[str, Any]:
    privileges = ("select", "insert", "update", "delete", "truncate", "references", "trigger")
    for table, allowed in (
        ("distillation_attachments", {"select", "insert", "update"}),
        ("distillation_turn_attachments", {"select", "insert"}),
    ):
        values = connection.exec_driver_sql("SELECT " + ", ".join(
            "has_table_privilege(current_user, %s, '" + item.upper() + "')" for item in privileges),
            ("public." + table,) * len(privileges)).one()
        if any(bool(value) != (privilege in allowed) for privilege, value in zip(privileges, values)):
            raise RuntimeError(f"runtime temporary-input privileges are incorrect for {table}")
    expected = {
        "uq_distillation_turn_owner": ("distillation_conversation_turns", "u",
            "UNIQUE (id, project_id, tenant_id, created_by)"),
        "fk_distillation_attachment_project": ("distillation_attachments", "f",
            "FOREIGN KEY (project_id, tenant_id) REFERENCES distillation_projects(id, tenant_id) ON DELETE RESTRICT"),
        "fk_distillation_attachment_scenario": ("distillation_attachments", "f",
            "FOREIGN KEY (scenario_id, tenant_id) REFERENCES business_scenarios(id, tenant_id) ON DELETE RESTRICT"),
        "uq_distillation_attachment_owner": ("distillation_attachments", "u",
            "UNIQUE (id, project_id, tenant_id, created_by)"),
        "uq_distillation_attachment_request": ("distillation_attachments", "u",
            "UNIQUE (project_id, created_by, request_id)"),
        "fk_distillation_turn_attachment_turn_owner": ("distillation_turn_attachments", "f",
            "FOREIGN KEY (turn_id, project_id, tenant_id, user_id) REFERENCES distillation_conversation_turns(id, project_id, tenant_id, created_by) ON DELETE RESTRICT"),
        "fk_distillation_turn_attachment_input_owner": ("distillation_turn_attachments", "f",
            "FOREIGN KEY (attachment_id, project_id, tenant_id, user_id) REFERENCES distillation_attachments(id, project_id, tenant_id, created_by) ON DELETE RESTRICT"),
    }
    rows = connection.exec_driver_sql("""
        SELECT c.conname, r.relname, c.contype, c.convalidated, pg_get_constraintdef(c.oid)
          FROM pg_constraint AS c JOIN pg_class AS r ON r.oid = c.conrelid
          JOIN pg_namespace AS n ON n.oid = r.relnamespace
         WHERE n.nspname = 'public' AND r.relname IN
            ('distillation_attachments', 'distillation_turn_attachments', 'distillation_conversation_turns')
    """).all()
    actual = {str(row[0]): row for row in rows}
    normalize = lambda value: "".join(value.replace("public.", "").split()).casefold()
    for name, (table, kind, definition) in expected.items():
        row = actual.get(name)
        if (row is None or str(row[1]) != table or str(row[2]) != kind or not row[3]
                or normalize(str(row[4])) != normalize(definition)):
            raise RuntimeError(f"temporary-input constraint {name} is missing or incorrect")
    checks = {
        "ck_distillation_attachment_status": ("status", "ready", "bound", "removed", "expired"),
        "ck_distillation_attachment_size": ("byte_size", "10485760", "char_length(parsed_text)", "200000"),
        "ck_distillation_attachment_expiry_content": ("status", "ready", "bound", "parsed_text", "''"),
    }
    for name, markers in checks.items():
        row = actual.get(name)
        if (row is None or str(row[1]) != "distillation_attachments" or str(row[2]) != "c" or not row[3]
                or any(marker not in str(row[4]) for marker in markers)):
            raise RuntimeError(f"temporary-input bound {name} is missing or incorrect")
    indexes = connection.exec_driver_sql("""
        SELECT c.relname, i.indisvalid, pg_get_indexdef(i.indexrelid)
          FROM pg_index AS i JOIN pg_class AS c ON c.oid = i.indexrelid
         WHERE i.indrelid IN ('public.distillation_attachments'::regclass, 'public.distillation_turn_attachments'::regclass)
    """).all()
    by_name = {str(row[0]): row for row in indexes}
    for name, columns in {
        "ix_distillation_attachments_expires_at": "(expires_at)",
        "ix_distillation_turn_attachments_input": "(attachment_id, turn_id)",
    }.items():
        row = by_name.get(name)
        if row is None or not row[1] or columns not in str(row[2]):
            raise RuntimeError(f"temporary-input cleanup index {name} is missing or incorrect")
    return {"ownership_constraints": len(expected), "retention_checks": len(checks), "links": "append_only"}


def verify_discovery_contract(connection: Any) -> dict[str, Any]:
    for table in ("distillation_system_access",):
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            actual = connection.exec_driver_sql("SELECT has_table_privilege(current_user, %s, %s)", (table, privilege)).scalar_one()
            if bool(actual) != (privilege in {"SELECT", "INSERT"}):
                raise RuntimeError(f"discovery privileges are incorrect for {table}")
    rows = connection.exec_driver_sql("""SELECT column_name, privilege_type FROM information_schema.column_privileges
        WHERE table_schema='public' AND table_name='distillation_system_access' AND grantee=current_user
          AND privilege_type='UPDATE'""").all()
    if set(rows) != {("revoked_at", "UPDATE")}:
        raise RuntimeError("system access may only update revocation time")
    for table, constraint in (("distillation_system_access", "fk_distillation_system_access_project"),):
        row = connection.exec_driver_sql("""SELECT c.convalidated, pg_get_constraintdef(c.oid)
            FROM pg_constraint c JOIN pg_class r ON c.conrelid=r.oid JOIN pg_namespace n ON n.oid=r.relnamespace
            WHERE n.nspname='public' AND r.relname=%s AND c.conname=%s""", (table, constraint)).one_or_none()
        if not row or not row[0] or "FOREIGN KEY (project_id, tenant_id)" not in row[1] or "ON DELETE RESTRICT" not in row[1]:
            raise RuntimeError("discovery project ownership constraint is missing")
    return {"system_access": "encrypted_scoped_revocable"}
