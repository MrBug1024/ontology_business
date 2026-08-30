from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.models import Base
from scripts import verify_postgresql_runtime as runtime
from scripts.verify_postgresql_runtime import _validate_runtime_table_privileges


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REVISION_PATH = (
    BACKEND_ROOT
    / "migrations"
    / "versions"
    / "20260827_04_close_catalog_tenant_and_reasoning_scope.py"
)


def _load_revision():
    spec = importlib.util.spec_from_file_location("catalog_scope_revision", REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _named_constraint(table_name: str, name: str):
    matches = [
        constraint
        for constraint in Base.metadata.tables[table_name].constraints
        if constraint.name == name
    ]
    assert len(matches) == 1, (table_name, name)
    return matches[0]


def test_revision_04_constraint_contract_matches_orm_metadata() -> None:
    revision = _load_revision()
    assert revision.revision == "20260827_04"
    assert revision.down_revision == "20260827_03"

    for name, table_name, columns in revision.UNIQUE_CONSTRAINTS:
        constraint = _named_constraint(table_name, name)
        assert isinstance(constraint, UniqueConstraint)
        assert tuple(column.name for column in constraint.columns) == tuple(columns)

    for name, source, target, local, remote, ondelete in revision.FOREIGN_KEYS:
        if name.endswith("_fkey"):
            continue
        constraint = _named_constraint(source, name)
        assert isinstance(constraint, ForeignKeyConstraint)
        assert tuple(column.name for column in constraint.columns) == tuple(local)
        assert tuple(element.column.table.name for element in constraint.elements) == (
            target,
        ) * len(remote)
        assert tuple(element.column.name for element in constraint.elements) == tuple(remote)
        assert constraint.ondelete == ondelete

    for name, table_name, _condition in revision.CHECK_CONSTRAINTS:
        constraint = _named_constraint(table_name, name)
        assert isinstance(constraint, CheckConstraint)


def test_scope_columns_are_non_nullable_where_identity_requires_them() -> None:
    revision = _load_revision()
    lifecycle_nullable = {
        # Revision 11 permits only the whole physical blob pair to become NULL
        # after a guarded temporary-attachment expiry transition. Tenant scope
        # and every live pair remain constrained by the composite foreign keys.
        ("data_asset_versions", "bucket_data_source_id"),
    }
    for table_name, column_names in revision.REQUIRED_COLUMNS.items():
        table = Base.metadata.tables[table_name]
        for column_name in column_names:
            expected_nullable = (table_name, column_name) in lifecycle_nullable
            assert table.c[column_name].nullable is expected_nullable, (
                table_name,
                column_name,
            )


def test_catalog_scope_tables_compile_for_postgresql() -> None:
    dialect = postgresql.dialect()
    table_names = (
        "data_asset_versions",
        "dataset_versions",
        "dataset_fragments",
        "scenario_dataset_bindings",
        "semantic_mappings",
        "semantic_field_mappings",
        "semantic_relation_mappings",
        "reasoning_terms",
        "derivation_runs",
        "derivation_run_inputs",
        "assertions",
        "derivation_evidence",
    )
    for table_name in table_names:
        ddl = str(CreateTable(Base.metadata.tables[table_name]).compile(dialect=dialect))
        assert "FOREIGN KEY" in ddl


def test_reasoning_evidence_is_pinned_to_the_assertion_run_input() -> None:
    expected = {
        "fk_derivation_evidence_assertion_run",
        "fk_derivation_evidence_fragment_input",
        "fk_derivation_evidence_pinned_input",
        "fk_assertions_subject_scope",
        "fk_assertions_object_scope",
    }
    actual = {
        constraint.name
        for table_name in ("assertions", "derivation_evidence")
        for constraint in Base.metadata.tables[table_name].constraints
    }
    assert expected <= actual
    one_source = _named_constraint(
        "derivation_evidence", "ck_derivation_evidence_one_source"
    )
    assert "dataset_fragment_id IS NULL THEN 0" in str(one_source.sqltext)
    assert "dataset_field_id IS NULL AND dataset_field_id" not in str(
        one_source.sqltext
    )


def _valid_privileges() -> dict[str, dict[str, bool]]:
    privileges: dict[str, dict[str, bool]] = {}
    for table_name in runtime.RUNTIME_IMMUTABLE_TABLES:
        privileges[table_name] = {
            "select": True,
            "insert": True,
            "update": False,
            "delete": False,
        }
    for table_name in runtime.RUNTIME_MIGRATION_LEDGER_TABLES:
        privileges[table_name] = {
            "select": True,
            "insert": False,
            "update": False,
            "delete": False,
        }
    for table_name in runtime.RUNTIME_REQUIRED_UPDATE_TABLES:
        privileges[table_name] = {
            "select": True,
            "insert": True,
            "update": True,
            "delete": True,
        }
    return privileges


def test_runtime_privilege_contract_preserves_state_updates_only() -> None:
    assert set(runtime.RUNTIME_IMMUTABLE_TABLES).isdisjoint(
        runtime.RUNTIME_REQUIRED_UPDATE_TABLES
    )
    assert set(runtime.RUNTIME_REQUIRED_UPDATE_TABLES) == {
        "dataset_heads",
        "ingestion_runs",
        "derivation_runs",
    }
    result = _validate_runtime_table_privileges(
        _valid_privileges(),
        immutable_tables=runtime.RUNTIME_IMMUTABLE_TABLES,
        ledger_tables=runtime.RUNTIME_MIGRATION_LEDGER_TABLES,
        required_update_tables=runtime.RUNTIME_REQUIRED_UPDATE_TABLES,
    )
    assert result["immutable_tables"] == len(runtime.RUNTIME_IMMUTABLE_TABLES)


@pytest.mark.parametrize(
    ("table_name", "privilege"),
    (
        ("dataset_versions", "update"),
        ("assertions", "delete"),
        ("alembic_version", "insert"),
        ("platform_migration_runs", "update"),
    ),
)
def test_runtime_privilege_contract_rejects_mutation(table_name: str, privilege: str) -> None:
    privileges = _valid_privileges()
    privileges[table_name][privilege] = True
    with pytest.raises(RuntimeError, match="mutat"):
        _validate_runtime_table_privileges(
            privileges,
            immutable_tables=runtime.RUNTIME_IMMUTABLE_TABLES,
            ledger_tables=runtime.RUNTIME_MIGRATION_LEDGER_TABLES,
            required_update_tables=runtime.RUNTIME_REQUIRED_UPDATE_TABLES,
        )
