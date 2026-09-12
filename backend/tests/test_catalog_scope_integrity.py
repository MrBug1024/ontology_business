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
SCHEMA_FIRST_REVISION_PATH = (
    BACKEND_ROOT
    / "migrations"
    / "versions"
    / "20260905_23_make_semantic_mappings_schema_first.py"
)
AGENT_ATTACHMENT_REVISION_PATH = (
    BACKEND_ROOT
    / "migrations"
    / "versions"
    / "20260911_31_agent_attachment_ownership.py"
)
AGENT_SCOPE_REVISION_PATH = (
    BACKEND_ROOT
    / "migrations"
    / "versions"
    / "20260912_32_harden_data_source_agent_scope.py"
)


def _load_revision():
    spec = importlib.util.spec_from_file_location("catalog_scope_revision", REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_revision_from(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
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
        if name in {
            "fk_semantic_relations_source_mapping",
            "fk_semantic_relations_target_mapping",
        }:
            # Revision 23 removes only the legacy binding column from these
            # endpoint scope keys.
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


def test_revision_23_semantic_mapping_scope_is_schema_first() -> None:
    spec = importlib.util.spec_from_file_location(
        "semantic_schema_first_revision", SCHEMA_FIRST_REVISION_PATH
    )
    assert spec is not None and spec.loader is not None
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    assert revision.revision == "20260905_23"
    assert revision.down_revision == "20260904_22"

    semantic = Base.metadata.tables["semantic_mappings"]
    semantic_relation = Base.metadata.tables["semantic_relation_mappings"]
    assert semantic.c.scenario_dataset_binding_id.nullable is True
    assert semantic_relation.c.scenario_dataset_binding_id.nullable is True
    for name in (
        "fk_semantic_relations_source_mapping",
        "fk_semantic_relations_target_mapping",
    ):
        constraint = _named_constraint("semantic_relation_mappings", name)
        assert isinstance(constraint, ForeignKeyConstraint)
        assert "scenario_dataset_binding_id" not in {
            column.name for column in constraint.columns
        }

    migration = SCHEMA_FIRST_REVISION_PATH.read_text(encoding="utf-8")
    assert "duplicate scenario/entity/schema ownership" in migration
    assert "FROM semantic_relation_mappings" in migration


def _valid_agent_scope_snapshot() -> tuple[
    dict[str, set[str]], set[str], dict[str, dict[str, str | None]]
]:
    columns = {
        table_name: set(column_names)
        for table_name, column_names in runtime._AGENT_SCOPE_COLUMNS.items()
    }
    indexes = set(runtime._AGENT_SCOPE_INDEXES)
    constraints: dict[str, dict[str, str | None]] = {}
    for name, expected in runtime._AGENT_SCOPE_CONSTRAINTS.items():
        if expected["constraint_type"] == "f":
            definition = (
                "FOREIGN KEY (owner_agent_id, tenant_id) "
                "REFERENCES agents (id, tenant_id) ON DELETE RESTRICT"
            )
            delete_action = "r"
        elif name == "ck_data_sources_resource_scope":
            definition = "resource_scope IN ('modeling', 'agent_runtime')"
            delete_action = None
        else:
            definition = (
                "owner_agent_id IS NULL OR "
                "(tenant_id IS NOT NULL AND resource_scope = 'agent_runtime')"
            )
            delete_action = None
        constraints[name] = {
            "table_name": str(expected["table_name"]),
            "constraint_type": str(expected["constraint_type"]),
            "delete_action": delete_action,
            "definition": definition,
        }
    return columns, indexes, constraints


def test_agent_scope_migrations_and_runtime_contract_are_fail_closed() -> None:
    revision_31 = _load_revision_from(
        AGENT_ATTACHMENT_REVISION_PATH,
        "agent_attachment_ownership_revision",
    )
    revision_32 = _load_revision_from(
        AGENT_SCOPE_REVISION_PATH,
        "agent_scope_revision",
    )
    assert revision_31.revision == "20260911_31"
    assert revision_31.down_revision == "20260909_30"
    assert revision_32.revision == "20260912_32"
    assert revision_32.down_revision == "20260911_31"

    columns, indexes, constraints = _valid_agent_scope_snapshot()
    result = runtime._validate_agent_scope_snapshot(
        columns,
        indexes=indexes,
        constraints=constraints,
    )
    assert result == {
        "revision": "20260912_32",
        "scoped_tables": 3,
        "scoped_indexes": 4,
        "scoped_constraints": 5,
    }

    migration_31 = AGENT_ATTACHMENT_REVISION_PATH.read_text(encoding="utf-8")
    migration_32 = AGENT_SCOPE_REVISION_PATH.read_text(encoding="utf-8")
    assert migration_31.count('ondelete="RESTRICT"') == 2
    assert "_precheck_existing_scope" in migration_32
    assert "_precheck_downgrade" in migration_32
    assert "ondelete=\"RESTRICT\"" in migration_32

    data_assets = Base.metadata.tables["data_assets"]
    managed_upload_runs = Base.metadata.tables["managed_upload_runs"]
    data_sources = Base.metadata.tables["data_sources"]
    assert data_assets.c.owner_agent_id.nullable is True
    assert managed_upload_runs.c.owner_agent_id.nullable is True
    assert data_sources.c.owner_agent_id.nullable is True
    assert data_sources.c.resource_scope.nullable is False
    for table_name, index_name in (
        ("data_assets", "ix_data_assets_owner_agent_id"),
        ("managed_upload_runs", "ix_managed_upload_runs_owner_agent_id"),
        ("data_sources", "ix_data_sources_owner_agent_id"),
    ):
        assert index_name in {
            index.name for index in Base.metadata.tables[table_name].indexes
        }
    for name, table_name in (
        ("fk_data_assets_owner_agent_tenant", "data_assets"),
        ("fk_managed_upload_runs_owner_agent_tenant", "managed_upload_runs"),
        ("fk_data_sources_owner_agent_tenant", "data_sources"),
        ("ck_data_sources_resource_scope", "data_sources"),
        ("ck_data_sources_owner_scope", "data_sources"),
    ):
        assert _named_constraint(table_name, name).name == name


def test_agent_scope_runtime_contract_rejects_missing_or_cascading_metadata() -> None:
    columns, indexes, constraints = _valid_agent_scope_snapshot()
    del columns["data_assets"]
    with pytest.raises(RuntimeError, match="columns"):
        runtime._validate_agent_scope_snapshot(
            columns,
            indexes=indexes,
            constraints=constraints,
        )

    columns, indexes, constraints = _valid_agent_scope_snapshot()
    constraints["fk_data_sources_owner_agent_tenant"]["delete_action"] = "c"
    with pytest.raises(RuntimeError, match="RESTRICT"):
        runtime._validate_agent_scope_snapshot(
            columns,
            indexes=indexes,
            constraints=constraints,
        )

    columns, indexes, constraints = _valid_agent_scope_snapshot()
    indexes.remove("ix_data_assets_owner_agent_id")
    with pytest.raises(RuntimeError, match="indexes"):
        runtime._validate_agent_scope_snapshot(
            columns,
            indexes=indexes,
            constraints=constraints,
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
    for table_name in runtime.RUNTIME_APPEND_ONLY_TABLES:
        privileges[table_name] = {
            "select": True,
            "insert": True,
            "update": False,
            "delete": False,
        }
    for table_name in runtime.RUNTIME_MUTABLE_CONTROL_TABLES:
        privileges[table_name] = {
            "select": True,
            "insert": True,
            "update": True,
            "delete": False,
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
    assert set(runtime.RUNTIME_APPEND_ONLY_TABLES) == {"agent_turn_events", "release_lifecycle_events", "workflow_approval_evidence"}
    assert set(runtime.RUNTIME_MUTABLE_CONTROL_TABLES) == {
        "agent_turn_runs",
        "assistant_request_runs",
        "managed_upload_runs",
    }
    result = _validate_runtime_table_privileges(
        _valid_privileges(),
        immutable_tables=runtime.RUNTIME_IMMUTABLE_TABLES,
        ledger_tables=runtime.RUNTIME_MIGRATION_LEDGER_TABLES,
        required_update_tables=runtime.RUNTIME_REQUIRED_UPDATE_TABLES,
        append_only_tables=runtime.RUNTIME_APPEND_ONLY_TABLES,
        mutable_control_tables=runtime.RUNTIME_MUTABLE_CONTROL_TABLES,
    )
    assert result["immutable_tables"] == len(runtime.RUNTIME_IMMUTABLE_TABLES)
    assert result["append_only_tables"] == 3
    assert result["mutable_control_tables"] == 3


@pytest.mark.parametrize(
    ("table_name", "privilege"),
    (
        ("dataset_versions", "update"),
        ("assertions", "delete"),
        ("alembic_version", "insert"),
        ("platform_migration_runs", "update"),
        ("agent_turn_events", "update"),
        ("agent_turn_runs", "delete"),
        ("assistant_request_runs", "delete"),
        ("managed_upload_runs", "truncate"),
    ),
)
def test_runtime_privilege_contract_rejects_mutation(table_name: str, privilege: str) -> None:
    privileges = _valid_privileges()
    privileges[table_name][privilege] = True
    with pytest.raises(RuntimeError, match="mutat|excessive"):
        _validate_runtime_table_privileges(
            privileges,
            immutable_tables=runtime.RUNTIME_IMMUTABLE_TABLES,
            ledger_tables=runtime.RUNTIME_MIGRATION_LEDGER_TABLES,
            required_update_tables=runtime.RUNTIME_REQUIRED_UPDATE_TABLES,
            append_only_tables=runtime.RUNTIME_APPEND_ONLY_TABLES,
            mutable_control_tables=runtime.RUNTIME_MUTABLE_CONTROL_TABLES,
        )


@pytest.mark.parametrize(
    ("table_name", "privilege"),
    (
        ("agent_turn_events", "insert"),
        ("agent_turn_runs", "update"),
        ("assistant_request_runs", "insert"),
        ("managed_upload_runs", "insert"),
    ),
)
def test_runtime_privilege_contract_requires_durable_worker_access(
    table_name: str,
    privilege: str,
) -> None:
    privileges = _valid_privileges()
    privileges[table_name][privilege] = False
    with pytest.raises(RuntimeError, match="lacks"):
        _validate_runtime_table_privileges(
            privileges,
            immutable_tables=runtime.RUNTIME_IMMUTABLE_TABLES,
            ledger_tables=runtime.RUNTIME_MIGRATION_LEDGER_TABLES,
            required_update_tables=runtime.RUNTIME_REQUIRED_UPDATE_TABLES,
            append_only_tables=runtime.RUNTIME_APPEND_ONLY_TABLES,
            mutable_control_tables=runtime.RUNTIME_MUTABLE_CONTROL_TABLES,
        )
