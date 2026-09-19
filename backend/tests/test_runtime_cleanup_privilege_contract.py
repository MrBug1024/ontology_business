import importlib.util
from pathlib import Path

import pytest


def test_only_assistant_request_lineage_has_migration_authorized_delete():
    path = Path(__file__).resolve().parents[1] / "scripts" / "verify_postgresql_runtime.py"
    spec = importlib.util.spec_from_file_location("runtime_privilege_verifier", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tables = ("assistant_request_runs", "agent_turn_runs", "managed_upload_runs")
    privileges = {table: {"select": True, "insert": True, "update": True,
        "delete": table == "assistant_request_runs"} for table in tables}
    args = dict(immutable_tables=(), ledger_tables=(), required_update_tables=(), mutable_control_tables=tables)
    module._validate_runtime_table_privileges(privileges, **args)
    for table in tables:
        changed = {key: dict(value) for key, value in privileges.items()}
        changed[table]["delete"] = not changed[table]["delete"]
        with pytest.raises(RuntimeError):
            module._validate_runtime_table_privileges(changed, **args)
    privileges["assistant_request_runs"]["truncate"] = True
    with pytest.raises(RuntimeError):
        module._validate_runtime_table_privileges(privileges, **args)
