from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql

from app.models import Base, DataAsset, DataAssetVersion
from app.services import managed_upload_run_service


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REVISION_PATH = (
    BACKEND_ROOT
    / "migrations"
    / "versions"
    / "20260904_21_close_durable_run_scope_and_connector_profiles.py"
)
RETRY_LINEAGE_REVISION_PATH = (
    BACKEND_ROOT
    / "migrations"
    / "versions"
    / "20260905_24_add_durable_retry_lineage.py"
)


def _load_revision():
    spec = importlib.util.spec_from_file_location("durable_scope_revision", REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_retry_lineage_revision():
    spec = importlib.util.spec_from_file_location(
        "durable_retry_lineage_revision", RETRY_LINEAGE_REVISION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _constraint(table_name: str, name: str):
    matches = [
        item
        for item in Base.metadata.tables[table_name].constraints
        if item.name == name
    ]
    assert len(matches) == 1, (table_name, name)
    return matches[0]


def test_revision_21_matches_durable_orm_ownership_contract() -> None:
    revision = _load_revision()
    assert revision.revision == "20260904_21"
    assert revision.down_revision == "20260904_20"

    for name, table_name, columns in revision.UNIQUE_CONSTRAINTS:
        item = _constraint(table_name, name)
        assert isinstance(item, UniqueConstraint)
        assert tuple(column.name for column in item.columns) == columns

    for name, source, target, local_columns, remote_columns in revision.FOREIGN_KEYS:
        item = _constraint(source, name)
        assert isinstance(item, ForeignKeyConstraint)
        assert tuple(column.name for column in item.columns) == local_columns
        assert tuple(element.column.table.name for element in item.elements) == (
            target,
        ) * len(remote_columns)
        assert tuple(element.column.name for element in item.elements) == remote_columns


def test_revision_24_matches_retry_lineage_orm_contract() -> None:
    revision = _load_retry_lineage_revision()
    assert revision.revision == "20260905_24"
    assert revision.down_revision == "20260905_23"
    operations = Mock()
    with patch.object(revision, "op", operations):
        revision.upgrade()

    operations.create_unique_constraint.assert_any_call(
        "uq_managed_upload_runs_parent_retry",
        "managed_upload_runs",
        ["tenant_id", "parent_run_id"],
    )
    operations.create_unique_constraint.assert_any_call(
        "uq_assistant_request_runs_parent_retry",
        "assistant_request_runs",
        ["tenant_id", "parent_run_id"],
    )
    operations.create_foreign_key.assert_any_call(
        "fk_managed_upload_runs_parent_tenant",
        "managed_upload_runs",
        "managed_upload_runs",
        ["parent_run_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    operations.create_foreign_key.assert_any_call(
        "fk_assistant_request_runs_parent_tenant",
        "assistant_request_runs",
        "assistant_request_runs",
        ["parent_run_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="CASCADE",
    )

    for table_name in ("managed_upload_runs", "assistant_request_runs"):
        table = Base.metadata.tables[table_name]
        assert table.c.parent_run_id.nullable is True
        unique = _constraint(table_name, f"uq_{table_name}_parent_retry")
        assert isinstance(unique, UniqueConstraint)
        assert tuple(column.name for column in unique.columns) == (
            "tenant_id",
            "parent_run_id",
        )
        foreign_key = _constraint(table_name, f"fk_{table_name}_parent_tenant")
        assert isinstance(foreign_key, ForeignKeyConstraint)
        assert tuple(column.name for column in foreign_key.columns) == (
            "parent_run_id",
            "tenant_id",
        )
        assert tuple(element.column.name for element in foreign_key.elements) == (
            "id",
            "tenant_id",
        )
        expected_ondelete = "CASCADE" if table_name == "assistant_request_runs" else "RESTRICT"
        assert foreign_key.ondelete == expected_ondelete

    assistant_identity = _constraint(
        "assistant_request_runs", "uq_assistant_request_runs_id_tenant"
    )
    assert isinstance(assistant_identity, UniqueConstraint)
    assert tuple(column.name for column in assistant_identity.columns) == (
        "id",
        "tenant_id",
    )


@pytest.mark.parametrize(
    "lineage_results",
    (
        (object(),),
        (None, object()),
    ),
)
def test_revision_24_downgrade_refuses_to_discard_retry_lineage(
    lineage_results: tuple[object | None, ...],
) -> None:
    revision = _load_retry_lineage_revision()
    connection = Mock()
    connection.execute.side_effect = [
        SimpleNamespace(first=lambda result=result: result)
        for result in lineage_results
    ]
    operations = Mock()
    operations.get_bind.return_value = connection

    with patch.object(revision, "op", operations):
        with pytest.raises(RuntimeError, match="retry lineage cannot be discarded"):
            revision.downgrade()

    operations.drop_index.assert_not_called()


def test_connector_structure_profile_has_jsonb_and_sha256_contract() -> None:
    table = Base.metadata.tables["connector_bindings"]
    assert table.c.structure_profile.nullable is False
    assert isinstance(
        table.c.structure_profile.type.dialect_impl(postgresql.dialect()),
        postgresql.JSONB,
    )
    assert table.c.structure_fingerprint.nullable is False
    assert table.c.structure_fingerprint.type.length == 64
    fingerprint_check = _constraint(
        "connector_bindings", "ck_connector_bindings_structure_fingerprint"
    )
    assert isinstance(fingerprint_check, CheckConstraint)
    assert "structure_fingerprint = ''" in str(fingerprint_check.sqltext)


def test_revision_21_precondition_reports_only_deterministic_counts() -> None:
    revision = _load_revision()
    counts = iter((0, 2, 0, 3))

    class Result:
        def __init__(self, count: int) -> None:
            self.count = count

        def scalar_one(self) -> int:
            return self.count

    class Connection:
        def execute(self, _statement):
            return Result(next(counts))

    with pytest.raises(RuntimeError) as exc_info:
        revision._assert_existing_ownership_integrity(Connection())

    assert str(exc_info.value) == (
        "20260904_21 ownership precondition failed; "
        "turn_user_message_mismatches=2, upload_asset_version_mismatches=3"
    )


def test_revision_21_runtime_grants_are_least_privilege() -> None:
    revision = _load_revision()
    hardened = str(revision._runtime_role_statement(hardened=True))
    assert "GRANT SELECT, INSERT, UPDATE ON TABLE\n              public.agent_turn_runs" in hardened
    assert "public.agent_turn_runs, public.managed_upload_runs" in hardened
    assert "GRANT SELECT, INSERT ON TABLE public.agent_turn_events" in hardened
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE" not in hardened

    restored = str(revision._runtime_role_statement(hardened=False))
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE" in restored


def test_managed_upload_public_result_rejects_mismatched_asset_version() -> None:
    now = datetime.now(timezone.utc)
    run = SimpleNamespace(
        id="run",
        parent_run_id=None,
        purpose="validation_asset",
        filename="records.csv",
        declared_byte_size=10,
        byte_size=10,
        status="ready",
        revision=3,
        asset_id="asset-a",
        asset_version_id="version-b",
        content_sha256="a" * 64,
        error_code="",
        error_message="",
        expires_at=now,
        created_at=now,
        updated_at=now,
        finished_at=now,
        tenant_id="tenant",
        metadata_document={},
    )
    asset = SimpleNamespace(id="asset-a", tenant_id="tenant")
    version = SimpleNamespace(
        id="version-b", tenant_id="tenant", asset_id="asset-b"
    )
    db = Mock()
    db.get.side_effect = lambda model, _identity: (
        asset if model is DataAsset else version if model is DataAssetVersion else None
    )

    with patch.object(
        managed_upload_run_service.catalog_ingestion_service,
        "managed_upload_document",
    ) as render:
        document = managed_upload_run_service._public_run(db, run)

    assert document["result"] is None
    render.assert_not_called()
