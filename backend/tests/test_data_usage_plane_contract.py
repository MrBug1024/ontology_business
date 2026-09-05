from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import CheckConstraint

from app.database import Base
from app.models import DataAsset, LogicalDataset


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REVISION_PATH = (
    BACKEND_ROOT
    / "migrations"
    / "versions"
    / "20260904_18_add_usage_planes_and_agent_turns.py"
)
ISOLATION_REVISION_PATH = (
    BACKEND_ROOT
    / "migrations"
    / "versions"
    / "20260904_20_retire_non_modeling_metadata.py"
)


def _check(table_name: str, constraint_name: str) -> CheckConstraint:
    matches = [
        item
        for item in Base.metadata.tables[table_name].constraints
        if item.name == constraint_name
    ]
    assert len(matches) == 1
    return matches[0]


def test_catalog_entities_have_authoritative_fail_closed_usage_plane() -> None:
    assert DataAsset.__table__.c.usage_plane.default.arg == "generated_output"
    assert LogicalDataset.__table__.c.usage_plane.default.arg == "generated_output"
    for table_name, constraint_name in (
        ("data_assets", "ck_data_assets_usage_plane"),
        ("logical_datasets", "ck_logical_datasets_usage_plane"),
    ):
        sql = str(_check(table_name, constraint_name).sqltext)
        assert "modeling_material" in sql
        assert "invocation_input" in sql
        assert "generated_output" in sql


def test_revision_adds_usage_planes_turn_queue_and_runtime_grants() -> None:
    spec = importlib.util.spec_from_file_location("usage_plane_revision", REVISION_PATH)
    assert spec is not None and spec.loader is not None
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    assert revision.revision == "20260904_18"
    assert revision.down_revision == "20260831_17"

    with patch.object(revision, "op") as migration_op:
        migration_op.get_bind.return_value.dialect.name = "sqlite"
        revision.upgrade()
        assert {call.args[0] for call in migration_op.create_table.call_args_list} == {
            "agent_turn_runs",
            "agent_turn_events",
        }
        assert [call.args[1].name for call in migration_op.add_column.call_args_list] == [
            "usage_plane",
            "usage_plane",
        ]

    grant = str(revision._runtime_role_statement("GRANT"))
    assert "agent_turn_runs" in grant
    assert "agent_turn_events" in grant
    migration = REVISION_PATH.read_text(encoding="utf-8")
    assert "fk_agent_turn_runs_agent_tenant" in migration
    assert "fk_agent_turn_runs_preparation_tenant" in migration
    assert "fk_agent_turn_runs_parent_tenant" in migration


def test_revision_retires_pollution_and_withdraws_unproven_snapshot_dependencies() -> None:
    spec = importlib.util.spec_from_file_location(
        "source_isolation_revision",
        ISOLATION_REVISION_PATH,
    )
    assert spec is not None and spec.loader is not None
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    assert revision.revision == "20260904_20"
    assert revision.down_revision == "20260904_19"

    with patch.object(revision, "op") as migration_op:
        migration_op.get_bind.return_value.dialect.name = "postgresql"
        revision.upgrade()

    statements = [str(call.args[0]) for call in migration_op.execute.call_args_list]
    assert len(statements) == 5
    assert "ontology_releases" in statements[0]
    assert "status = 'rolled_back'" in statements[0]
    assert revision.WITHDRAW_REASON in statements[0]
    assert "dataset_schema_hash" in statements[0]
    assert "semantic_mapping_ids" in statements[0]
    assert "jsonb_array_length" in statements[0]
    for mutable_authoring_join in (
        "JOIN scenario_capability_ports",
        "JOIN semantic_mappings",
        "JOIN logical_datasets",
    ):
        assert mutable_authoring_join not in statements[0]
    assert "semantic_relation_mappings" in statements[1]
    assert "semantic_mappings" in statements[2]
    assert "scenario_capability_ports" in statements[3]
    assert "scenario_dataset_bindings" in statements[4]
    assert all("modeling_material" in statement for statement in statements[1:])
    assert all("UPDATE ontology_snapshots" not in statement for statement in statements)

    with pytest.raises(RuntimeError, match="irreversible"):
        revision.downgrade()
