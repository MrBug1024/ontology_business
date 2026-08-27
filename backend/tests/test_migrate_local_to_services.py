from __future__ import annotations

import argparse
import copy
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import JSON, Column, MetaData, String, Table
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects.mysql import pymysql
from sqlalchemy.schema import CreateTable

from scripts import migrate_local_to_services as migration


def _legacy_v2_manifest() -> dict:
    source = {
        "platform": {"snapshot_time": "2026-08-26T00:00:00+00:00"},
        "business": {},
        "files": [],
        "cleanup_inventory": [],
    }
    target = {"mysql": {"database": "ontology_business"}, "minio": {}}
    contract = migration._legacy_v2_manifest_contract()
    plan_digest = migration._sha256_json(
        {"contract": contract, "source": source, "target": target}
    )
    expected = {
        "format_version": 2,
        "plan_digest": plan_digest,
        "platform": {},
        "business": {},
        "files": [],
    }
    return {
        "format_version": 2,
        "contract": contract,
        "source": source,
        "target": target,
        "plan_digest": plan_digest,
        "state": {
            "executed": True,
            "verified": False,
            "cleaned": False,
            "target_expected": expected,
            "target_expected_sha256": migration._sha256_json(expected),
        },
    }


def test_fixed_platform_and_scenario_contract() -> None:
    assert migration.MANIFEST_FORMAT_VERSION == 3
    assert len(migration.SOURCE_PLATFORM_TABLES) == 58
    assert len(migration.PLATFORM_TABLES) == 59
    assert set(migration.PLATFORM_TABLES) - set(migration.SOURCE_PLATFORM_TABLES) == {
        "object_deletion_jobs"
    }
    assert migration.EXCLUDED_LEGACY_PLATFORM_TABLES == {
        "incident_case_history",
        "incident_cases",
        "ontology_advanced_assets",
        "ontology_advanced_records",
        "ontology_advanced_runs",
        "ontology_model_feedback",
    }
    assert migration.TARGET_SCENARIO_IDS == (
        "56e2006148e8499e8599f5c7c8145e60",
        "cc5d3ff36d2a468596dfa9f8ef2995da",
    )
    contract = migration._manifest_contract()
    assert contract["transient_data_policy"] == {
        "email_verification_codes": "exclude_all",
        "auth_sessions": "expires_after_manifest_snapshot",
    }
    assert contract["minio_bucket_versioning_capabilities"] == [
        "Enabled",
        "Supported",
        "Unsupported",
    ]
    assert contract["minio_object_key"] == {
        "strategy": "canonical-bucket-file-id-v1",
        "unique_identity": "bucket_files.id",
        "existing_object_policy": "same-key-same-bytes-only",
    }
    assert contract["mysql_datetime_precision"] == 6
    assert (
        contract["business_view_target_semantics"]
        == "mysql-fixed-select-unordered-v1"
    )
    legacy_contract = migration._legacy_v2_manifest_contract()
    assert "mysql_datetime_precision" not in legacy_contract
    assert "business_view_target_semantics" not in legacy_contract


def test_orm_target_ddl_is_current_and_innodb() -> None:
    metadata = migration._orm_platform_metadata()
    assert set(metadata.tables) == set(migration.PLATFORM_TABLES)
    dialect = pymysql.dialect()
    bucket = metadata.tables["bucket_files"]
    attachment = metadata.tables["assistant_attachments"]
    agents = metadata.tables["agents"]
    outbox = metadata.tables["object_deletion_jobs"]
    assert "mcp_ids" not in agents.c
    assert "skill_ids" not in agents.c
    assert bucket.c.stored_path.type.length == 4096
    assert bucket.c.object_key.type.length == 2048
    assert bucket.c.object_url.type.length == 4096
    assert outbox.c.lease_token.type.length == 64
    assert "lease_generation" in outbox.c
    bucket_ddl = str(CreateTable(bucket).compile(dialect=dialect)).upper()
    attachment_ddl = str(CreateTable(attachment).compile(dialect=dialect)).upper()
    assert "PARSED_TEXT LONGTEXT" in " ".join(bucket_ddl.split())
    assert "PARSED_TEXT LONGTEXT" in " ".join(attachment_ddl.split())
    datetime_columns = migration._metadata_datetime_columns(metadata)
    assert len(datetime_columns) >= 100
    assert {
        ("action_execution_logs", "created_at"),
        ("users", "created_at"),
        ("object_deletion_jobs", "created_at"),
    } <= set(datetime_columns)
    assert {
        int(getattr(column.type, "fsp", 0) or 0)
        for column in datetime_columns.values()
    } == {migration.MYSQL_DATETIME_PRECISION}
    assert {
        str(column.type.compile(dialect=dialect)).upper()
        for column in datetime_columns.values()
    } == {"DATETIME(6)"}
    schemas = migration._platform_target_schemas({})
    assert schemas["action_execution_logs"]["datetime_precisions"]["created_at"] == 6
    assert schemas["users"]["datetime_precisions"]["created_at"] == 6
    assert schemas["object_deletion_jobs"]["datetime_precisions"]["created_at"] == 6
    for table in metadata.tables.values():
        ddl = str(CreateTable(table).compile(dialect=dialect)).upper()
        assert "ENGINE=INNODB" in ddl
        if any(key[0] == table.name for key in datetime_columns):
            assert "DATETIME(6)" in ddl


def test_previous_manifest_format_is_rejected_after_datetime_contract_change() -> None:
    with pytest.raises(migration.MigrationError, match="格式版本"):
        migration.validate_manifest({"format_version": 2})


def test_empty_target_table_source_schema_stays_v2_stable() -> None:
    current = migration._platform_target_schemas({})["object_deletion_jobs"]
    stable = migration._source_snapshot_schema_for_empty_target_table(current)
    temporal_types = {
        column["type"]
        for column in stable["columns"]
        if column["name"] in {
            "next_attempt_at",
            "created_at",
            "updated_at",
            "completed_at",
        }
    }
    assert temporal_types == {"DATETIME"}
    assert "datetime_precisions" not in stable
    assert current["column_types"]["created_at"] == "DATETIME(6)"


def test_v2_supersede_manifest_is_strict_and_descriptor_is_immutable() -> None:
    old = _legacy_v2_manifest()
    descriptor = migration._validate_v2_supersede_manifest(old)
    assert descriptor == {
        "mode": migration.SUPERSEDE_MODE_V2_DATETIME6,
        "old_plan_digest": old["plan_digest"],
        "old_expected_sha256": old["state"]["target_expected_sha256"],
    }

    new_manifest = {
        "format_version": migration.MANIFEST_FORMAT_VERSION,
        "contract": migration._manifest_contract(),
        "source": old["source"],
        "target": old["target"],
        "supersedes": descriptor,
    }
    new_manifest["plan_digest"] = migration._sha256_json(
        migration._manifest_immutable_payload(new_manifest)
    )
    migration.validate_manifest(new_manifest)
    tampered_descriptor = copy.deepcopy(new_manifest)
    tampered_descriptor["supersedes"]["old_expected_sha256"] = "f" * 64
    with pytest.raises(migration.MigrationError, match="不可变"):
        migration.validate_manifest(tampered_descriptor)

    for mutate in (
        lambda item: item["state"].update(verified=True),
        lambda item: item["state"].update(cleaned=True),
        lambda item: item["state"].update(
            target_expected_sha256="0" * 64
        ),
        lambda item: item["contract"].update(unreviewed=True),
    ):
        invalid = copy.deepcopy(old)
        mutate(invalid)
        with pytest.raises(migration.MigrationError):
            migration._validate_v2_supersede_manifest(invalid)


def test_superseding_dry_run_reuses_snapshot_and_requires_identical_source_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _legacy_v2_manifest()
    old_path = tmp_path / "old-v2.json"
    old_path.write_text(json.dumps(old), encoding="utf-8")
    captured: dict[str, object] = {}

    def rebuild(_paths, **kwargs):
        captured.update(kwargs)
        return {
            "source": copy.deepcopy(old["source"]),
            "target": copy.deepcopy(old["target"]),
            "supersedes": copy.deepcopy(kwargs["supersedes"]),
        }

    monkeypatch.setattr(migration, "build_dry_run_manifest", rebuild)
    rebuilt = migration.build_superseding_dry_run_manifest(object(), old_path)
    assert captured["snapshot_time"] == old["source"]["platform"]["snapshot_time"]
    assert rebuilt["supersedes"]["old_plan_digest"] == old["plan_digest"]

    def changed_target(_paths, **kwargs):
        result = rebuild(_paths, **kwargs)
        result["target"] = {"mysql": {"database": "other"}}
        return result

    monkeypatch.setattr(migration, "build_dry_run_manifest", changed_target)
    with pytest.raises(migration.MigrationError, match="target"):
        migration.build_superseding_dry_run_manifest(object(), old_path)


def test_assert_source_unchanged_preserves_supersedes_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = migration._validate_v2_supersede_manifest(_legacy_v2_manifest())
    manifest = {
        "plan_digest": "a" * 64,
        "source": {"platform": {"snapshot_time": "2026-08-26T00:00:00+00:00"}},
        "supersedes": descriptor,
    }
    captured = {}

    def rebuild(_paths, **kwargs):
        captured.update(kwargs)
        return {"plan_digest": manifest["plan_digest"]}

    monkeypatch.setattr(migration, "build_dry_run_manifest", rebuild)
    migration.assert_source_unchanged(object(), manifest)
    assert captured["supersedes"] == descriptor


def test_platform_target_width_preflight_reports_metadata_only() -> None:
    metadata = MetaData()
    Table(
        "audit_rows",
        metadata,
        Column("status", String(20)),
        Column("serialized", String(12)),
        Column("unicode_label", String(2)),
        Column("json_document", JSON),
    )
    secret = "do-not-print-this-value"
    transformed = {
        "audit_rows": [
            {
                "status": "confirmation_required",
                "serialized": {"secret": secret},
                "unicode_label": "医保",
                "json_document": {"nested": ["合法", 1]},
            }
        ]
    }
    violations = migration._platform_target_width_violations(
        transformed, metadata=metadata
    )
    assert violations == [
        {
            "table": "audit_rows",
            "column": "status",
            "target_length": 20,
            "maximum_length": 21,
            "overflow_rows": 1,
        },
        {
            "table": "audit_rows",
            "column": "serialized",
            "target_length": 12,
            "maximum_length": len(
                migration._json_dump({"secret": secret})
            ),
            "overflow_rows": 1,
        },
    ]
    with pytest.raises(migration.MigrationError) as captured:
        migration._assert_platform_target_widths(
            transformed, metadata=metadata
        )
    message = str(captured.value)
    assert "audit_rows.status(target=20,max=21,rows=1)" in message
    assert "serialized" in message
    assert secret not in message
    assert "confirmation_required" not in message


def test_platform_datetime_precision_upgrade_is_multitable_and_idempotent() -> None:
    metadata = MetaData()
    Table(
        "first_table",
        metadata,
        Column(
            "created_at", mysql.DATETIME(fsp=6), nullable=False
        ),
    )
    Table(
        "second_table",
        metadata,
        Column("updated_at", mysql.DATETIME(fsp=6), nullable=True),
    )

    class Result:
        def __init__(self, rows) -> None:
            self.rows = rows

        def mappings(self):
            return self.rows

    class Connection:
        engine = argparse.Namespace(
            url=argparse.Namespace(database="ontology_business")
        )

        def __init__(self) -> None:
            self.precisions = {
                ("first_table", "created_at"): 0,
                ("second_table", "updated_at"): 6,
            }
            self.alters: list[str] = []

        def exec_driver_sql(self, statement, _parameters=None):
            if statement.startswith("SELECT TABLE_NAME"):
                return Result(
                    [
                        {
                            "TABLE_NAME": table,
                            "COLUMN_NAME": column,
                            "DATETIME_PRECISION": precision,
                        }
                        for (table, column), precision in self.precisions.items()
                    ]
                )
            assert statement.startswith("ALTER TABLE")
            self.alters.append(statement)
            self.precisions[("first_table", "created_at")] = 6
            return None

    connection = Connection()
    migration._ensure_platform_datetime_precision(connection, metadata)
    assert len(connection.alters) == 1
    assert "ALTER TABLE `first_table` MODIFY COLUMN" in connection.alters[0]
    assert "created_at DATETIME(6) NOT NULL" in connection.alters[0]
    assert "second_table" not in connection.alters[0]
    migration._ensure_platform_datetime_precision(connection, metadata)
    assert len(connection.alters) == 1


def test_platform_schema_verify_requires_datetime_precision_six() -> None:
    expected = {
        "platform": {
            "audit_rows": {
                "columns": ["created_at"],
                "mysql_data_types": {"created_at": "datetime"},
                "nullable": {"created_at": False},
                "character_lengths": {"created_at": None},
                "datetime_precisions": {"created_at": 6},
            }
        }
    }

    class Connection:
        engine = argparse.Namespace(
            url=argparse.Namespace(database="ontology_business")
        )

        def __init__(self, precision: int) -> None:
            self.precision = precision

        def exec_driver_sql(self, _statement, _parameters=None):
            return [("created_at", "datetime", "NO", None, self.precision)]

    migration._verify_platform_column_contract(Connection(6), expected)
    with pytest.raises(migration.MigrationError, match="时间列精度"):
        migration._verify_platform_column_contract(Connection(0), expected)


def test_datetime_hash_preserves_mysql_microseconds() -> None:
    columns = ["created_at"]
    types = {"created_at": "DATETIME(6)"}
    expected_rows = [
        {"created_at": "2026-08-26T12:34:56.123456+00:00"}
    ]
    mysql_rows = [
        {"created_at": datetime(2026, 8, 26, 12, 34, 56, 123456)}
    ]
    truncated_rows = [
        {"created_at": datetime(2026, 8, 26, 12, 34, 56)}
    ]
    assert migration._hash_rows(expected_rows, columns, types) == (
        migration._hash_rows(mysql_rows, columns, types)
    )
    assert migration._hash_rows(expected_rows, columns, types) != (
        migration._hash_rows(truncated_rows, columns, types)
    )


def test_mysql_datetime0_round_half_up_thresholds() -> None:
    before_threshold = datetime(2026, 8, 26, 12, 34, 56, 499999)
    at_threshold = datetime(2026, 8, 26, 12, 34, 56, 500000)
    assert migration._mysql_datetime0_round_half_up(before_threshold) == datetime(
        2026, 8, 26, 12, 34, 56
    )
    assert migration._mysql_datetime0_round_half_up(at_threshold) == datetime(
        2026, 8, 26, 12, 34, 57
    )


def test_legacy_connector_hash_template_matches_canonical_platform_hash() -> None:
    candidate = datetime(2026, 8, 26, 12, 34, 56, 654321)
    rows = [
        {
            "id": "binding-a",
            "binding_key": "stable",
            "checked_at": candidate,
            "created_at": datetime(2026, 8, 20, 1, 2, 3, 4),
            "updated_at": candidate,
        },
        {
            "id": "binding-b",
            "binding_key": "generated",
            "checked_at": candidate,
            "created_at": candidate,
            "updated_at": candidate,
        },
    ]
    expected = {
        "columns": [
            "id",
            "binding_key",
            "checked_at",
            "created_at",
            "updated_at",
        ],
        "pk_columns": ["id"],
        "column_types": {
            "id": "VARCHAR(32)",
            "binding_key": "VARCHAR(191)",
            "checked_at": "DATETIME",
            "created_at": "DATETIME",
            "updated_at": "DATETIME",
        },
    }
    count, canonical_digest = migration._platform_rows_hash(rows, expected)
    template_count, parts = migration._legacy_connector_hash_template(
        rows,
        expected,
        {
            "binding-a": ("checked_at", "updated_at"),
            "binding-b": ("checked_at", "created_at", "updated_at"),
        },
    )
    assert template_count == count
    assert migration._legacy_connector_candidate_digest(
        parts, candidate
    ) == bytes.fromhex(canonical_digest)


@pytest.mark.parametrize("view", migration.MEDICAL_VIEWS)
def test_fixed_target_view_select_accepts_multiline_medical_select(view: str) -> None:
    select_sql = migration._fixed_target_view_select(view)
    assert select_sql.startswith("SELECT\n")
    assert len(select_sql.splitlines()) > 2


@pytest.mark.parametrize(
    "invalid_body",
    (
        "SELECTIVE `医保目录编码` FROM `项目明细表`",
        "/* comment prefix */\nSELECT `医保目录编码` FROM `项目明细表`",
    ),
)
def test_fixed_target_view_select_rejects_invalid_select_prefix(
    invalid_body: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = migration.MEDICAL_VIEWS[0]
    monkeypatch.setitem(
        migration.MEDICAL_VIEW_DDL,
        view,
        f"CREATE OR REPLACE VIEW {migration._q_mysql(view)} AS\n{invalid_body}",
    )
    with pytest.raises(migration.MigrationError, match="不是 SELECT"):
        migration._fixed_target_view_select(view)


def test_business_view_uses_independent_fixed_select_and_refreshes_expected() -> None:
    view = "audit_project_view"
    columns = ["project_id", "company_name"]
    column_types = {
        "project_id": "VARCHAR(32)",
        "company_name": "VARCHAR(191)",
    }
    fixed_rows = [
        {"project_id": "p-2", "company_name": "乙公司"},
        {"project_id": "p-1", "company_name": "甲公司"},
    ]
    actual_rows = copy.deepcopy(fixed_rows)
    expected = {
        "business": {
            scenario.id: {"views": {}}
            for scenario in migration.SCENARIOS
        }
    }
    expected["business"][migration.BOOKKEEPING_SCENARIO_ID]["views"][view] = {
        "columns": columns,
        "column_types": column_types,
        # A legacy v2 manifest carries the SQLite preview hash.  Preflight
        # must not require that hash to equal the fixed MySQL SELECT result.
        "row_count": 999,
        "row_sha256": "0" * 64,
        "target_ddl_sha256": migration.hashlib.sha256(
            migration._fixed_target_view_ddl(view).encode("utf-8")
        ).hexdigest(),
    }

    class Result:
        def __init__(self, rows) -> None:
            self.rows = copy.deepcopy(rows)

        def mappings(self):
            return iter(self.rows)

    class Connection:
        def execution_options(self, **_kwargs):
            return self

        def exec_driver_sql(self, statement, _parameters=None):
            if "FROM (" in statement:
                return Result(fixed_rows)
            assert f"FROM `{view}`" in statement
            return Result(actual_rows)

    class Context:
        def __enter__(self):
            return Connection()

        def __exit__(self, *_args):
            return None

    class Engine:
        def connect(self):
            return Context()

    results = migration._business_view_target_results(
        Connection(), expected, require_expected_hash=False
    )
    target = results[migration.BOOKKEEPING_SCENARIO_ID][view]
    assert target["row_count"] == 2
    assert target["row_sha256"] != "0" * 64

    migration._refresh_business_view_expected(Engine(), expected)
    refreshed = expected["business"][migration.BOOKKEEPING_SCENARIO_ID]["views"][
        view
    ]
    assert refreshed["row_count"] == target["row_count"]
    assert refreshed["row_sha256"] == target["row_sha256"]
    migration._business_view_target_results(
        Connection(), expected, require_expected_hash=True
    )

    # Even if an arbitrary/tampered view agrees with a forged expected hash,
    # it is rejected when it differs from the independent fixed SELECT.
    actual_rows[:] = [{"project_id": "p-1", "company_name": "篡改公司"}]
    forged_count, forged_hash = migration._hash_rows_unordered(
        actual_rows, columns, column_types
    )
    refreshed["row_count"] = forged_count
    refreshed["row_sha256"] = forged_hash
    with pytest.raises(migration.MigrationError, match="固定 MySQL SELECT"):
        migration._business_view_target_results(
            Connection(), expected, require_expected_hash=True
        )


def test_object_key_and_stable_url_match_runtime_contract() -> None:
    key = migration.build_object_key(
        "ontology-business",
        "tenant_01",
        migration.MEDICAL_SCENARIO_ID,
        migration.MEDICAL_BUCKET_SOURCE_ID,
        "a" * 32,
        "医保 明细.csv",
    )
    assert key == (
        "ontology-business/tenants/tenant_01/scenarios/"
        f"{migration.MEDICAL_SCENARIO_ID}/data-sources/"
        f"{migration.MEDICAL_BUCKET_SOURCE_ID}/files/{'a' * 32}/医保 明细.csv"
    )
    url = migration.build_object_url(
        endpoint="ignored.example",
        secure=True,
        bucket="ontology",
        object_key=key,
    )
    assert url.startswith("minio://ontology/ontology-business/tenants/tenant_01/")
    assert "%E5%8C%BB%E4%BF%9D%20%E6%98%8E%E7%BB%86.csv" in url
    migration._assert_non_reusable_object_keys(
        [{"file_id": "a" * 32, "object_key": key}]
    )
    with pytest.raises(migration.MigrationError, match="file_id"):
        migration._assert_non_reusable_object_keys(
            [
                {"file_id": "a" * 32, "object_key": key},
                {"file_id": "a" * 32, "object_key": key + "-copy"},
            ]
        )


def _minimal_platform_for_transform() -> dict:
    target_schemas = migration._platform_target_schemas({})
    schemas = {
        table: {
            "pk_columns": target_schemas[table]["pk_columns"],
            "json_columns": target_schemas[table]["json_columns"],
        }
        for table in migration.PLATFORM_TABLES
    }
    rows = {table: {} for table in migration.PLATFORM_TABLES}
    for scenario in migration.SCENARIOS:
        rows["business_scenarios"][(scenario.id,)] = {
            "id": scenario.id,
            "tenant_id": "tenant_01",
            "name": scenario.name,
            "namespace": scenario.namespace,
        }
        rows["data_sources"][(scenario.sql_source_id,)] = {
            "id": scenario.sql_source_id,
            "tenant_id": "tenant_01",
            "scenario_id": scenario.id,
            "name": f"sql_{scenario.namespace}",
            "type": "sqlite",
            "config": {},
            "connector_revision": 1,
            "status": "ok",
        }
        rows["data_sources"][(scenario.bucket_source_id,)] = {
            "id": scenario.bucket_source_id,
            "tenant_id": "tenant_01",
            "scenario_id": scenario.id,
            "name": f"files_{scenario.namespace}",
            "type": "file_bucket",
            "config": {},
            "connector_revision": 1,
            "status": "ok",
        }
    return {"_schemas": schemas, "_rows": rows}


def test_file_bucket_transform_uses_runtime_prefix_key() -> None:
    platform = _minimal_platform_for_transform()
    settings = migration.ServiceSettings(
        mysql_host="db",
        mysql_port=3306,
        mysql_database="ontology_business",
        mysql_admin_user="admin",
        mysql_admin_password="admin-secret",
        mysql_runtime_user="ontology_app",
        mysql_runtime_password="runtime-secret-value",
        mysql_account_host="%",
        readonly_accounts={
            scenario.id: (scenario.readonly_user_default, "x" * 32)
            for scenario in migration.SCENARIOS
        },
        minio_endpoint="minio.example",
        minio_access_key="access",
        minio_secret_key="secret",
        minio_bucket="ontology",
        minio_prefix="ontology-business",
        minio_secure=True,
    )
    transformed = migration.transform_platform_rows(
        platform, settings, {}, executed_at="2026-08-26T00:00:00+00:00"
    )
    sources = {row["id"]: row for row in transformed["data_sources"]}
    for scenario in migration.SCENARIOS:
        config = sources[scenario.bucket_source_id]["config"]
        assert config == {
            "storage_backend": "minio",
            "bucket_name": "ontology",
            "prefix": "ontology-business",
        }
        assert "object_prefix" not in config


def test_medical_ddl_handles_duplicates_and_row_width(tmp_path: Path) -> None:
    database = tmp_path / "medical.db"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute(
        'CREATE TABLE "项目明细表" ('
        '"记账流水号", "医保目录名称", "定点医药机构名称", '
        '"就诊ID", "医保目录编码", "单价", "大段备注")'
    )
    connection.executemany(
        'INSERT INTO "项目明细表" VALUES (?,?,?,?,?,?,?)',
        [
            ("same", "项目", "机构", "visit-1", "code", "12.3", "x" * 1000),
            ("same", "项目", "机构", "visit-1", "code", "12.3", "y" * 1000),
        ],
    )
    plan = migration._medical_table_plan(connection, "项目明细表", {})
    ddl = migration._business_create_table_ddl("项目明细表", plan)
    connection.close()
    assert "DECIMAL(30,8)" in ddl
    assert "`大段备注` LONGTEXT" in ddl
    assert "UNIQUE" not in ddl.upper()
    assert "ENGINE=InnoDB" in ddl
    assert plan["columns"][0]["name"] == "__migration_row_id"


def test_transient_identity_rows_are_filtered_deterministically() -> None:
    cutoff = datetime(2026, 8, 26, tzinfo=timezone.utc)
    selected = {table: {} for table in migration.PLATFORM_TABLES}
    selected["email_verification_codes"][("code",)] = {"id": "code"}
    selected["auth_sessions"][("old",)] = {
        "id": "old",
        "expires_at": (cutoff - timedelta(seconds=1)).isoformat(),
    }
    selected["auth_sessions"][("live",)] = {
        "id": "live",
        "expires_at": (cutoff + timedelta(seconds=1)).isoformat(),
    }
    migration._semantic_filter_platform_rows(selected, snapshot_time=cutoff)
    assert selected["email_verification_codes"] == {}
    assert {row["id"] for row in selected["auth_sessions"].values()} == {"live"}


def test_missing_readonly_passwords_are_generated_without_logging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    required = {
        "ANNUAL_MYSQL_HOST": "db",
        "ANNUAL_MYSQL_DATABASE": "ontology_business",
        "ANNUAL_MYSQL_USER": "admin",
        "ANNUAL_MYSQL_PASSWORD": "admin-password",
        "MINIO_ALIYUN_ENDPOINT": "minio.example",
        "MINIO_ALIYUN_ACCESS_KEY_ID": "access",
        "MINIO_ALIYUN_ACCESS_KEY_SECRET": "secret",
        "MINIO_BUCKETNAME": "ontology",
    }
    for key, value in required.items():
        monkeypatch.setenv(key, value)
    for scenario in migration.SCENARIOS:
        monkeypatch.delenv(scenario.readonly_password_env, raising=False)
    monkeypatch.setattr(migration.secrets, "token_urlsafe", lambda _size: "R" * 43)
    settings = migration.load_service_settings(tmp_path / "missing.env")
    assert {
        password for _username, password in settings.readonly_accounts.values()
    } == {"R" * 43}
    assert settings.mysql_admin_user == "admin"
    assert settings.mysql_runtime_user == "ontology_app"
    assert settings.mysql_runtime_password == "R" * 43


def test_dry_run_target_records_read_only_minio_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = {
        "ANNUAL_MYSQL_HOST": "db",
        "ANNUAL_MYSQL_DATABASE": "ontology_business",
        "MINIO_ALIYUN_ENDPOINT": "https://minio.example/",
        "MINIO_ALIYUN_ACCESS_KEY_ID": "access",
        "MINIO_ALIYUN_ACCESS_KEY_SECRET": "secret",
        "MINIO_BUCKETNAME": "ontology",
        "MINIO_ALIYUN_FILE_PATH": "ontology-business/",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    observed: dict[str, object] = {}

    def fake_client(**kwargs):
        observed.update(kwargs)
        return object()

    monkeypatch.setattr(migration, "_new_minio_client", fake_client)
    monkeypatch.setattr(
        migration,
        "_probe_minio_versioning",
        lambda client, bucket, **_kwargs: (
            migration.MINIO_VERSIONING_UNSUPPORTED
            if client is not None and bucket == "ontology"
            else "unexpected"
        ),
    )
    target = migration._dry_run_target_settings(tmp_path / "missing.env")
    assert target["minio"] == {
        "endpoint": "minio.example",
        "bucket": "ontology",
        "prefix": "ontology-business",
        "secure": True,
        "versioning": migration.MINIO_VERSIONING_UNSUPPORTED,
        "object_key_strategy": migration.MINIO_OBJECT_KEY_STRATEGY,
    }
    assert observed == {
        "endpoint": "minio.example",
        "access_key": "access",
        "secret_key": "secret",
        "secure": True,
    }
    assert "access" not in json.dumps(target)
    assert "secret" not in json.dumps(target)


def test_migration_admin_override_is_separate_from_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = {
        "ANNUAL_MYSQL_HOST": "db",
        "ANNUAL_MYSQL_DATABASE": "ontology_business",
        "ANNUAL_MYSQL_USER": "ontology_app",
        "ANNUAL_MYSQL_PASSWORD": "runtime-password-value",
        "MIGRATION_MYSQL_ADMIN_USER": "migration_admin",
        "MIGRATION_MYSQL_ADMIN_PASSWORD": "one-time-admin-value",
        "MIGRATION_MYSQL_ACCOUNT_HOST": "10.%",
        "MINIO_ALIYUN_ENDPOINT": "minio.example",
        "MINIO_ALIYUN_ACCESS_KEY_ID": "access",
        "MINIO_ALIYUN_ACCESS_KEY_SECRET": "secret",
        "MINIO_BUCKETNAME": "ontology",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    settings = migration.load_service_settings(tmp_path / "missing.env")
    assert settings.mysql_admin_user == "migration_admin"
    assert settings.mysql_runtime_user == "ontology_app"
    assert settings.mysql_runtime_password == "runtime-password-value"
    assert settings.mysql_account_host == "10.%"
    migration._require_separate_admin(settings)


def test_runtime_env_update_is_atomic_and_never_persists_admin(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ANNUAL_MYSQL_HOST=db\nANNUAL_MYSQL_USER=root\n"
        "ANNUAL_MYSQL_PASSWORD=old\nUNCHANGED=yes\n",
        encoding="utf-8",
    )
    settings = migration.ServiceSettings(
        mysql_host="db",
        mysql_port=3306,
        mysql_database="ontology_business",
        mysql_admin_user="root",
        mysql_admin_password="one-time-root-secret",
        mysql_runtime_user="ontology_app",
        mysql_runtime_password="generated-runtime-password",
        mysql_account_host="%",
        readonly_accounts={},
        minio_endpoint="minio.example",
        minio_access_key="access",
        minio_secret_key="secret",
        minio_bucket="ontology",
        minio_prefix="ontology-business",
        minio_secure=True,
    )
    migration._atomic_update_runtime_env(env_file, settings)
    content = env_file.read_text(encoding="utf-8")
    assert "ANNUAL_MYSQL_USER=ontology_app" in content
    assert "ANNUAL_MYSQL_PASSWORD=generated-runtime-password" in content
    assert "UNCHANGED=yes" in content
    assert "one-time-root-secret" not in content
    assert "MIGRATION_MYSQL_ADMIN" not in content


@pytest.mark.parametrize(
    ("host", "sql_host"),
    [
        ("%", "%%"),
        ("10.%", "10.%%"),
        ("db.internal", "db.internal"),
        ("127.0.0.1", "127.0.0.1"),
    ],
)
def test_mysql_account_literal_is_safe_for_parameterless_sql(
    host: str, sql_host: str
) -> None:
    account = migration._mysql_account("ontology_reader", host)
    assert account == f"'ontology_reader'@'{sql_host}'"
    # This mirrors PyMySQL's formatting of SQLAlchemy's empty parameter tuple.
    assert ("SHOW GRANTS FOR " + account) % () == (
        f"SHOW GRANTS FOR 'ontology_reader'@'{host}'"
    )
    assert migration._account_grantee("ontology_reader", host) == (
        f"'ontology_reader'@'{host}'"
    )


def test_mysql_account_create_alter_bind_every_value_and_grants_escape_percent() -> None:
    class Transaction:
        def __init__(self, connection) -> None:
            self.connection = connection

        def __enter__(self):
            return self.connection

        def __exit__(self, *_args):
            return None

    class Connection:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def exec_driver_sql(self, statement, parameters=None):
            self.calls.append((statement, parameters))
            return None

    class Engine:
        def __init__(self, connection) -> None:
            self.connection = connection

        def begin(self):
            return Transaction(self.connection)

    password = "bound-%-password-'never-in-sql'"
    connection = Connection()
    settings = argparse.Namespace(
        mysql_database="ontology_business",
        mysql_account_host="%",
        readonly_accounts={
            scenario.id: (scenario.readonly_user_default, password)
            for scenario in migration.SCENARIOS
        },
    )
    existing: set[str] = set()
    migration._configure_readonly_accounts(
        Engine(connection), settings, existing_accounts=existing
    )
    create_calls = [
        call for call in connection.calls if call[0].startswith("CREATE USER")
    ]
    assert len(create_calls) == 2
    for statement, parameters in create_calls:
        assert statement == "CREATE USER %s@%s IDENTIFIED BY %s"
        assert parameters[1:] == ("%", password)
        assert password not in statement
    no_parameter_calls = [
        call
        for call in connection.calls
        if call[0].startswith(("REVOKE ", "GRANT "))
    ]
    assert no_parameter_calls
    for statement, parameters in no_parameter_calls:
        assert parameters is None
        assert "@'%%'" in statement
        assert "@'%'" in statement % ()
        assert password not in statement

    runtime_connection = Connection()
    runtime_settings = argparse.Namespace(
        mysql_database="ontology_business",
        mysql_account_host="app.internal",
        mysql_runtime_user="ontology_app",
        mysql_runtime_password=password,
    )
    migration._configure_runtime_account(
        Engine(runtime_connection),
        runtime_settings,
        existing_accounts={"ontology_app"},
    )
    alter_statement, alter_parameters = runtime_connection.calls[0]
    assert alter_statement == "ALTER USER %s@%s IDENTIFIED BY %s"
    assert alter_parameters == ("ontology_app", "app.internal", password)
    assert password not in alter_statement


@pytest.mark.parametrize(
    ("username", "host"),
    [("bad-user", "%"), ("valid_user", "bad host"), ("valid_user", "bad'host")],
)
def test_mysql_account_rejects_unsafe_identifiers(username: str, host: str) -> None:
    with pytest.raises(migration.MigrationError):
        migration._mysql_account(username, host)


@pytest.mark.parametrize("version", ["5.7.44", "10.11.8-MariaDB", "8.0.36-Percona"])
def test_mysql_server_preflight_rejects_unsupported_servers(version: str) -> None:
    with pytest.raises(migration.MigrationError):
        migration._assert_mysql_server_contract(version, "STRICT_TRANS_TABLES")


def test_mysql_server_preflight_requires_strict_mysql_8() -> None:
    migration._assert_mysql_server_contract(
        "8.0.36", "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION"
    )
    with pytest.raises(migration.MigrationError, match="STRICT"):
        migration._assert_mysql_server_contract("8.4.0", "NO_ENGINE_SUBSTITUTION")


def test_local_phase_lock_rejects_second_thread_and_releases(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    first = migration._acquire_local_phase_lock(manifest)
    errors: list[Exception] = []

    def contender() -> None:
        try:
            lock = migration._acquire_local_phase_lock(manifest)
        except Exception as exc:  # noqa: BLE001 - asserting lock failure.
            errors.append(exc)
        else:
            migration._release_local_phase_lock(lock)

    thread = threading.Thread(target=contender)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], migration.MigrationError)
    migration._release_local_phase_lock(first)
    recovered = migration._acquire_local_phase_lock(manifest)
    migration._release_local_phase_lock(recovered)


def test_mysql_phase_lock_fails_fast_and_release_allows_retry() -> None:
    class Result:
        def __init__(self, value: int) -> None:
            self.value = value

        def scalar_one(self) -> int:
            return self.value

    class Connection:
        def __init__(self, acquire_value: int) -> None:
            self.acquire_value = acquire_value
            self.statements: list[str] = []
            self.closed = False

        def exec_driver_sql(self, statement: str, _params=()):
            self.statements.append(statement)
            return Result(
                self.acquire_value if "GET_LOCK" in statement else 1
            )

        def close(self) -> None:
            self.closed = True

    class Engine:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        def connect(self) -> Connection:
            return self.connection

    settings = argparse.Namespace(
        mysql_host="db",
        mysql_port=3306,
        mysql_database="ontology_business",
    )
    busy_connection = Connection(0)
    with pytest.raises(migration.MigrationError, match="并发"):
        migration._acquire_mysql_phase_lock(Engine(busy_connection), settings)
    assert busy_connection.closed

    first_connection = Connection(1)
    lock = migration._acquire_mysql_phase_lock(Engine(first_connection), settings)
    migration._release_mysql_phase_lock(lock)
    assert first_connection.closed
    assert any("RELEASE_LOCK" in statement for statement in first_connection.statements)
    second_connection = Connection(1)
    retry = migration._acquire_mysql_phase_lock(Engine(second_connection), settings)
    migration._release_mysql_phase_lock(retry)


def test_execute_width_preflight_precedes_target_and_minio_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = {
        "confirm_execute": "MIGRATE_TOKEN",
        "state": {},
        "source": {"platform": {"snapshot_time": "2026-08-27T00:00:00+00:00"}},
    }
    paths = argparse.Namespace(
        manifest_path=tmp_path / "manifest.json",
        env_file=tmp_path / ".env",
        platform_db=tmp_path / "platform.db",
    )
    events: list[str] = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            return None

    monkeypatch.setattr(migration, "_acquire_local_phase_lock", lambda _path: object())
    monkeypatch.setattr(migration, "_release_local_phase_lock", lambda _lock: None)
    monkeypatch.setattr(migration, "load_manifest", lambda _path: manifest)
    monkeypatch.setattr(migration, "load_service_settings", lambda _path: object())
    monkeypatch.setattr(migration, "_require_separate_admin", lambda _settings: None)
    monkeypatch.setattr(migration, "_assert_target_settings", lambda *_args: None)
    monkeypatch.setattr(migration, "_mysql_engine", lambda _settings: Engine())
    monkeypatch.setattr(migration, "_verify_mysql_server", lambda _connection: None)
    monkeypatch.setattr(migration, "_acquire_mysql_phase_lock", lambda *_args: object())
    monkeypatch.setattr(migration, "_release_mysql_phase_lock", lambda _lock: None)
    monkeypatch.setattr(migration, "assert_source_unchanged", lambda *_args: None)
    monkeypatch.setattr(
        migration, "collect_platform_snapshot", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(migration, "_validate_scenario_contract", lambda *_args: None)
    monkeypatch.setattr(
        migration, "_assert_collected_platform_matches_manifest", lambda *_args: None
    )
    monkeypatch.setattr(migration, "_manifest_upload_preview", lambda _manifest: {})
    monkeypatch.setattr(
        migration, "transform_platform_rows", lambda *_args, **_kwargs: {"rows": []}
    )

    def fail_width(_transformed, **_kwargs):
        events.append("width-preflight")
        raise migration.MigrationError("target width")

    monkeypatch.setattr(migration, "_assert_platform_target_widths", fail_width)
    monkeypatch.setattr(
        migration,
        "_prepare_target_schema",
        lambda *_args, **_kwargs: events.append("target-write"),
    )
    monkeypatch.setattr(
        migration,
        "_upload_files_to_minio",
        lambda *_args, **_kwargs: events.append("minio-write"),
    )
    with pytest.raises(migration.MigrationError, match="target width"):
        migration.execute_migration(paths, confirmation="MIGRATE_TOKEN")
    assert events == ["width-preflight"]


def test_cleanup_gate_fails_closed_on_tamper() -> None:
    expected = {"plan_digest": "a" * 64}
    manifest = {
        "format_version": migration.MANIFEST_FORMAT_VERSION,
        "contract": migration._manifest_contract(),
        "source": {},
        "target": {},
    }
    manifest["plan_digest"] = migration._sha256_json(
        migration._manifest_immutable_payload(manifest)
    )
    manifest["confirm_cleanup"] = "CLEANUP_TOKEN"
    manifest["state"] = {
        "executed": True,
        "verified": True,
        "target_expected": expected,
        "target_expected_sha256": migration._sha256_json(expected),
    }
    migration.assert_cleanup_allowed(
        manifest,
        confirmation="CLEANUP_TOKEN",
        verified_expected_sha256=migration._sha256_json(expected),
    )
    tampered = copy.deepcopy(manifest)
    tampered["state"]["target_expected"]["extra"] = True
    with pytest.raises(migration.MigrationError):
        migration.assert_cleanup_allowed(
            tampered,
            confirmation="CLEANUP_TOKEN",
            verified_expected_sha256=migration._sha256_json(expected),
        )


def _permission_error(*, winerror: int, message: str) -> PermissionError:
    error = PermissionError(message)
    error.winerror = winerror
    return error


def test_unlink_inventory_file_revalidates_every_sharing_violation_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "transient.sqlite"
    content = b"verified before every unlink attempt"
    path.write_bytes(content)
    item = {
        "size": len(content),
        "sha256": migration.hashlib.sha256(content).hexdigest(),
    }
    original_assert = migration._assert_inventory_file
    original_hash = migration._sha256_file
    original_unlink = Path.unlink
    validation_calls: list[Path] = []
    hash_calls: list[Path] = []
    unlink_calls: list[Path] = []
    sleeps: list[float] = []

    def assert_inventory_file(candidate: Path, expected) -> None:
        validation_calls.append(candidate)
        original_assert(candidate, expected)

    def sha256_file(candidate: Path, **kwargs) -> str:
        hash_calls.append(candidate)
        return original_hash(candidate, **kwargs)

    def transient_unlink(candidate: Path, *args, **kwargs) -> None:
        if candidate == path:
            unlink_calls.append(candidate)
            if len(unlink_calls) <= 2:
                raise _permission_error(winerror=32, message="sharing violation")
        original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(migration, "_assert_inventory_file", assert_inventory_file)
    monkeypatch.setattr(migration, "_sha256_file", sha256_file)
    monkeypatch.setattr(Path, "unlink", transient_unlink)
    monkeypatch.setattr(migration.gc, "collect", lambda: 0)
    monkeypatch.setattr(migration.time, "sleep", sleeps.append)

    migration._unlink_inventory_file(path, item, retry_delays=(0.01, 0.02))

    assert not path.exists()
    assert validation_calls == [path, path, path]
    assert hash_calls == [path, path, path]
    assert unlink_calls == [path, path, path]
    assert sleeps == [0.01, 0.02]


def test_unlink_inventory_file_does_not_retry_non_sharing_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "denied.sqlite"
    content = b"retain on ordinary permission error"
    path.write_bytes(content)
    item = {
        "size": len(content),
        "sha256": migration.hashlib.sha256(content).hexdigest(),
    }
    original_unlink = Path.unlink
    unlink_calls = 0
    sleeps: list[float] = []
    denied = _permission_error(winerror=5, message="access denied")

    def denied_unlink(candidate: Path, *args, **kwargs) -> None:
        nonlocal unlink_calls
        if candidate == path:
            unlink_calls += 1
            raise denied
        original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied_unlink)
    monkeypatch.setattr(migration.time, "sleep", sleeps.append)

    with pytest.raises(PermissionError) as raised:
        migration._unlink_inventory_file(path, item, retry_delays=(0.01, 0.02))

    assert raised.value is denied
    assert unlink_calls == 1
    assert sleeps == []
    assert path.read_bytes() == content


def test_unlink_inventory_file_propagates_terminal_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "still-open.sqlite"
    content = b"retain after retry budget is exhausted"
    path.write_bytes(content)
    item = {
        "size": len(content),
        "sha256": migration.hashlib.sha256(content).hexdigest(),
    }
    original_assert = migration._assert_inventory_file
    original_unlink = Path.unlink
    validation_calls = 0
    unlink_errors: list[PermissionError] = []
    sleeps: list[float] = []

    def assert_inventory_file(candidate: Path, expected) -> None:
        nonlocal validation_calls
        validation_calls += 1
        original_assert(candidate, expected)

    def always_shared(candidate: Path, *args, **kwargs) -> None:
        if candidate == path:
            error = _permission_error(winerror=32, message="still shared")
            unlink_errors.append(error)
            raise error
        original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(migration, "_assert_inventory_file", assert_inventory_file)
    monkeypatch.setattr(Path, "unlink", always_shared)
    monkeypatch.setattr(migration.gc, "collect", lambda: 0)
    monkeypatch.setattr(migration.time, "sleep", sleeps.append)

    with pytest.raises(PermissionError) as raised:
        migration._unlink_inventory_file(path, item, retry_delays=(0.01, 0.02))

    assert raised.value is unlink_errors[-1]
    assert validation_calls == 3
    assert len(unlink_errors) == 3
    assert sleeps == [0.01, 0.02]
    assert path.read_bytes() == content


class _TrackingSourceCursor:
    def __init__(self, batches, *, fail_fetch: int | None = None) -> None:
        self._batches = iter(batches)
        self._fail_fetch = fail_fetch
        self.fetch_calls = 0
        self.close_calls = 0

    def fetchmany(self, _batch_size):
        self.fetch_calls += 1
        if self.fetch_calls == self._fail_fetch:
            raise RuntimeError("source cursor failed")
        return next(self._batches, [])

    def close(self) -> None:
        self.close_calls += 1


class _TrackingSourceConnection:
    def __init__(self, cursor: _TrackingSourceCursor) -> None:
        self.cursor = cursor
        self.statements: list[str] = []

    def execute(self, statement: str):
        self.statements.append(statement)
        return self.cursor


def test_source_row_iterator_closes_cursor_after_success() -> None:
    cursor = _TrackingSourceCursor([[{"id": 1}], [{"id": 2}], []])
    connection = _TrackingSourceConnection(cursor)

    rows = list(
        migration._source_row_iterator(
            connection, "records", {"source_order": ["id"]}, batch_size=1
        )
    )

    assert rows == [{"id": 1}, {"id": 2}]
    assert cursor.close_calls == 1
    assert connection.statements == [
        'SELECT rowid AS "__source_rowid", * FROM "records" ORDER BY "id"'
    ]


def test_source_row_iterator_closes_cursor_when_generator_is_closed_early() -> None:
    cursor = _TrackingSourceCursor([[{"id": 1}, {"id": 2}], []])
    iterator = migration._source_row_iterator(
        _TrackingSourceConnection(cursor),
        "records",
        {"source_order": ["id"]},
    )

    assert next(iterator) == {"id": 1}
    iterator.close()

    assert cursor.close_calls == 1


def test_source_row_iterator_closes_cursor_after_fetch_exception() -> None:
    cursor = _TrackingSourceCursor([[{"id": 1}]], fail_fetch=2)
    iterator = migration._source_row_iterator(
        _TrackingSourceConnection(cursor),
        "records",
        {"source_order": ["id"]},
    )

    assert next(iterator) == {"id": 1}
    with pytest.raises(RuntimeError, match="source cursor failed"):
        next(iterator)

    assert cursor.close_calls == 1


class _TrackingViewCursor:
    def __init__(self, rows) -> None:
        self._rows = iter(rows)
        self.close_calls = 0

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._rows)

    def close(self) -> None:
        self.close_calls += 1


class _TrackingViewConnection:
    def __init__(self, cursor: _TrackingViewCursor) -> None:
        self.cursor = cursor

    def execute(self, statement: str):
        assert statement == 'SELECT * FROM "audit_view"'
        return self.cursor


def test_view_manifest_closes_result_cursor_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _TrackingViewCursor([{"id": "b"}, {"id": "a"}])
    monkeypatch.setattr(
        migration,
        "_table_info",
        lambda _connection, _view: [{"name": "id"}],
    )

    result = migration._view_manifest(
        _TrackingViewConnection(cursor), "audit_view"
    )

    assert result["row_count"] == 2
    assert result["columns"] == ["id"]
    assert cursor.close_calls == 1


def test_view_manifest_closes_result_cursor_on_hash_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _TrackingViewCursor([{"id": "a"}, {"id": "b"}])
    monkeypatch.setattr(
        migration,
        "_table_info",
        lambda _connection, _view: [{"name": "id"}],
    )

    def fail_after_first_row(rows, _columns, _types):
        assert next(iter(rows)) == {"id": "a"}
        raise RuntimeError("view hash failed")

    monkeypatch.setattr(migration, "_hash_rows_unordered", fail_after_first_row)

    with pytest.raises(RuntimeError, match="view hash failed"):
        migration._view_manifest(_TrackingViewConnection(cursor), "audit_view")

    assert cursor.close_calls == 1


def test_cleanup_holds_each_phase_lock_once_and_fresh_verify_failure_deletes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    retained = data_root / "retain.sqlite"
    retained.write_bytes(b"must survive a failed fresh verify")
    paths = migration.RuntimePaths(
        backend_root=tmp_path,
        data_root=data_root,
        platform_db=retained,
        buckets_root=data_root / "buckets",
        manifest_path=tmp_path / "migration.json",
        env_file=tmp_path / ".env",
    )
    calls = {
        "local_acquire": 0,
        "local_release": 0,
        "mysql_acquire": 0,
        "mysql_release": 0,
        "locked_verify": 0,
    }
    local_handle = object()
    mysql_handle = object()

    class ConnectionContext:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return None

    class Engine:
        def connect(self):
            return ConnectionContext()

        def dispose(self):
            return None

    def acquire_local(_path):
        calls["local_acquire"] += 1
        return local_handle

    def acquire_mysql(*_args):
        calls["mysql_acquire"] += 1
        return mysql_handle

    def release_local(handle):
        assert handle is local_handle
        calls["local_release"] += 1

    def release_mysql(handle):
        assert handle is mysql_handle
        calls["mysql_release"] += 1

    def fail_fresh_verify(*_args, **_kwargs):
        calls["locked_verify"] += 1
        raise migration.MigrationError("fresh verify rejected target")

    monkeypatch.setattr(migration, "load_manifest", lambda _path: {})
    monkeypatch.setattr(migration, "load_service_settings", lambda _path: object())
    monkeypatch.setattr(migration, "_require_separate_admin", lambda _settings: None)
    monkeypatch.setattr(migration, "_assert_target_settings", lambda *_args: None)
    monkeypatch.setattr(migration, "_mysql_engine", lambda _settings: Engine())
    monkeypatch.setattr(migration, "_verify_mysql_server", lambda _connection: None)
    monkeypatch.setattr(migration, "_acquire_local_phase_lock", acquire_local)
    monkeypatch.setattr(migration, "_release_local_phase_lock", release_local)
    monkeypatch.setattr(migration, "_acquire_mysql_phase_lock", acquire_mysql)
    monkeypatch.setattr(migration, "_release_mysql_phase_lock", release_mysql)
    monkeypatch.setattr(
        migration, "_verify_migration_locked", fail_fresh_verify, raising=False
    )

    def reject_nested_public_verify(*_args, **_kwargs):
        raise AssertionError("cleanup must not re-enter public verify_migration")

    monkeypatch.setattr(migration, "verify_migration", reject_nested_public_verify)
    with pytest.raises(migration.MigrationError, match="fresh verify rejected"):
        migration.cleanup_local_data(paths, confirmation="CLEANUP_TOKEN")

    assert calls == {
        "local_acquire": 1,
        "local_release": 1,
        "mysql_acquire": 1,
        "mysql_release": 1,
        "locked_verify": 1,
    }
    assert retained.read_bytes() == b"must survive a failed fresh verify"


def test_running_rebuild_keeps_control_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def exec_driver_sql(self, statement: str, *_args, **_kwargs):
            self.statements.append(statement)
            return None

    connection = FakeConnection()
    monkeypatch.setattr(
        migration,
        "_target_objects",
        lambda _connection: {
            migration.CONTROL_TABLE: {"type": "BASE TABLE", "engine": "InnoDB"},
            "data_sources": {"type": "BASE TABLE", "engine": "MyISAM"},
            "audit_project_view": {"type": "VIEW", "engine": ""},
        },
    )
    migration._drop_owned_running_schema(
        connection,
        managed_tables={"data_sources"},
        managed_views={"audit_project_view"},
    )
    joined = "\n".join(connection.statements)
    assert "DROP TABLE `data_sources`" in joined
    assert "DROP VIEW `audit_project_view`" in joined
    assert migration.CONTROL_TABLE not in joined


def test_only_empty_stale_running_plan_can_be_taken_over() -> None:
    manifest = {"plan_digest": "b" * 64}
    assert migration._is_safe_stale_running_takeover(
        {"plan_digest": "a" * 64, "status": "running", "expected": {}},
        manifest,
    )
    assert not migration._is_safe_stale_running_takeover(
        {"plan_digest": "b" * 64, "status": "running", "expected": {}},
        manifest,
    )
    for state in (
        {"plan_digest": "a" * 64, "status": "executed", "expected": {}},
        {
            "plan_digest": "a" * 64,
            "status": "running",
            "expected": {"plan_digest": "a" * 64},
        },
    ):
        with pytest.raises(migration.MigrationError, match="受控接管"):
            migration._is_safe_stale_running_takeover(state, manifest)

    class Result:
        rowcount = 1

    class Connection:
        def __init__(self) -> None:
            self.statement = ""
            self.parameters = ()

        def exec_driver_sql(self, statement, parameters):
            self.statement = statement
            self.parameters = parameters
            return Result()

    connection = Connection()
    migration._replace_stale_running_control(
        connection,
        old_plan_digest="a" * 64,
        new_plan_digest="b" * 64,
    )
    assert f"UPDATE `{migration.CONTROL_TABLE}`" in connection.statement
    assert "status='running'" in connection.statement
    assert "expected_json='{}'" in connection.statement
    assert connection.parameters[0] == "b" * 64
    assert connection.parameters[-1] == "a" * 64


def test_remote_supersede_state_rejects_default_executed_and_verified() -> None:
    old = _legacy_v2_manifest()
    descriptor = migration._validate_v2_supersede_manifest(old)
    new = {
        "plan_digest": "b" * 64,
        "supersedes": descriptor,
    }
    expected = old["state"]["target_expected"]
    state = {
        "plan_digest": old["plan_digest"],
        "status": "executed",
        "expected": expected,
    }
    assert migration._remote_supersede_expected(state, new) == expected
    with pytest.raises(migration.MigrationError, match="受控接管"):
        migration._is_safe_stale_running_takeover(state, new)

    verified = {**state, "status": "verified"}
    with pytest.raises(migration.MigrationError, match="严格"):
        migration._remote_supersede_expected(verified, new)
    wrong_digest = {**state, "plan_digest": "c" * 64}
    with pytest.raises(migration.MigrationError, match="digest"):
        migration._remote_supersede_expected(wrong_digest, new)
    wrong_expected = copy.deepcopy(state)
    wrong_expected["expected"]["extra"] = True
    with pytest.raises(migration.MigrationError, match="SHA-256"):
        migration._remote_supersede_expected(wrong_expected, new)

    adopted = {
        "plan_digest": new["plan_digest"],
        "status": "running",
        "expected": {},
    }
    assert migration._remote_supersede_expected(adopted, new) is None


def test_legacy_platform_reconstructs_old_hash_and_rejects_non_time_tamper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Use users, not the formerly special-cased action_execution_logs table,
    # to prove that the DATETIME(0) rule applies to every platform table.
    columns = ["id", "display_name", "created_at"]
    column_types = {
        "id": "VARCHAR(32)",
        "display_name": "VARCHAR(120)",
        "created_at": "DATETIME",
    }
    reconstructed = {
        "users": [
            {
                "id": "user-1",
                "display_name": "甲",
                "created_at": "2026-08-26T12:34:56.499999+00:00",
            },
            {
                "id": "user-2",
                "display_name": "乙",
                "created_at": "2026-08-26T12:34:56.500000+00:00",
            },
        ]
    }
    row_count, row_hash = migration._hash_rows(
        reconstructed["users"], columns, column_types
    )
    expected = {
        "platform": {
            "users": {
                "columns": columns,
                "pk_columns": ["id"],
                "column_types": column_types,
                "row_count": row_count,
                "row_sha256": row_hash,
            }
        }
    }
    remote_rows = [
        {
            "id": "user-1",
            "display_name": "甲",
            "created_at": datetime(2026, 8, 26, 12, 34, 56),
        },
        {
            "id": "user-2",
            "display_name": "乙",
            "created_at": datetime(2026, 8, 26, 12, 34, 57),
        },
    ]
    monkeypatch.setattr(migration, "PLATFORM_TABLES", ("users",))
    monkeypatch.setattr(migration, "_orm_platform_metadata", lambda: object())
    monkeypatch.setattr(
        migration,
        "_metadata_datetime_columns",
        lambda _metadata: {("users", "created_at"): object()},
    )
    monkeypatch.setattr(
        migration,
        "_mysql_rows",
        lambda *_args, **_kwargs: iter(copy.deepcopy(remote_rows)),
    )
    migration._verify_legacy_platform_rows(object(), expected, reconstructed)

    wrong_old_hash = copy.deepcopy(expected)
    wrong_old_hash["platform"]["users"]["row_sha256"] = "f" * 64
    with pytest.raises(migration.MigrationError, match="重建|expected|哈希"):
        migration._verify_legacy_platform_rows(
            object(), wrong_old_hash, reconstructed
        )

    tampered = copy.deepcopy(remote_rows)
    tampered[0]["display_name"] = "被篡改"
    monkeypatch.setattr(
        migration,
        "_mysql_rows",
        lambda *_args, **_kwargs: iter(copy.deepcopy(tampered)),
    )
    with pytest.raises(migration.MigrationError, match="非时间字段"):
        migration._verify_legacy_platform_rows(object(), expected, reconstructed)


def test_legacy_executed_at_requires_exactly_one_hash_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quantized = datetime(2026, 8, 26, 12, 34, 57)
    candidates = [
        datetime(2026, 8, 26, 12, 34, 56, 500000),
        datetime(2026, 8, 26, 12, 34, 56, 750000),
        datetime(2026, 8, 26, 12, 34, 57, 250000),
    ]
    expected_digest = bytes.fromhex("ab" * 32)
    expected = {
        "platform": {
            "connector_bindings": {
                "columns": ["id", "checked_at", "updated_at"],
                "pk_columns": ["id"],
                "column_types": {
                    "id": "VARCHAR(32)",
                    "checked_at": "DATETIME",
                    "updated_at": "DATETIME",
                },
                "row_count": 1,
                "row_sha256": expected_digest.hex(),
            }
        }
    }
    remote_rows = [
        {
            "id": "binding-1",
            "checked_at": quantized,
            "updated_at": quantized,
        }
    ]
    transformed = {
        "connector_bindings": [
            {
                "id": "binding-1",
                "checked_at": datetime(1900, 1, 1),
                "updated_at": datetime(1900, 1, 1),
            }
        ]
    }
    hit_candidates: set[datetime] = set()

    monkeypatch.setattr(
        migration,
        "_mysql_rows",
        lambda *_args, **_kwargs: iter(copy.deepcopy(remote_rows)),
    )
    monkeypatch.setattr(
        migration,
        "transform_platform_rows",
        lambda *_args, **_kwargs: copy.deepcopy(transformed),
    )
    monkeypatch.setattr(
        migration,
        "_legacy_migration_binding_fields",
        lambda *_args: {"binding-1": ("checked_at", "updated_at")},
    )
    def controlled_candidates(
        _quantized,
        *,
        snapshot_time,
        control_updated_at,
    ):
        assert snapshot_time == datetime(2026, 8, 26, 12, 0)
        assert control_updated_at == datetime(2026, 8, 26, 13, 0)
        return iter(candidates)

    monkeypatch.setattr(
        migration, "_legacy_executed_at_candidates", controlled_candidates
    )
    monkeypatch.setattr(
        migration,
        "_legacy_connector_hash_template",
        lambda *_args: (1, (b"",)),
    )
    monkeypatch.setattr(
        migration,
        "_legacy_connector_candidate_digest",
        lambda _parts, candidate: (
            expected_digest if candidate in hit_candidates else b"\x00" * 32
        ),
    )
    platform = {"snapshot_time": "2026-08-26T12:00:00+00:00"}
    # created_at is a preserved v1 crash anchor and may predate the final v2
    # snapshot.  The v2 executed control update is the candidate upper bound.
    control_state = {
        "created_at": "2026-08-25T08:00:00+00:00",
        "updated_at": "2026-08-26T13:00:00+00:00",
    }

    hit_candidates.add(candidates[1])
    recovered = migration._recover_legacy_executed_at(
        object(), expected, platform, object(), {}, control_state
    )
    assert recovered == "2026-08-26T12:34:56.750000+00:00"

    hit_candidates.clear()
    with pytest.raises(migration.MigrationError, match="命中数=0"):
        migration._recover_legacy_executed_at(
            object(), expected, platform, object(), {}, control_state
        )

    hit_candidates.update(candidates[:2])
    with pytest.raises(migration.MigrationError, match="命中数=2"):
        migration._recover_legacy_executed_at(
            object(), expected, platform, object(), {}, control_state
        )

    stale_update = {
        "created_at": "2026-08-25T08:00:00+00:00",
        "updated_at": "2026-08-26T11:59:59.999999+00:00",
    }
    with pytest.raises(migration.MigrationError, match="updated_at 早于 snapshot"):
        migration._recover_legacy_executed_at(
            object(), expected, platform, object(), {}, stale_update
        )


def test_supersede_preflight_orders_minio_database_then_cas_and_retry_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _legacy_v2_manifest()
    descriptor = migration._validate_v2_supersede_manifest(old)
    manifest = {"plan_digest": "b" * 64, "supersedes": descriptor}
    old_expected = old["state"]["target_expected"]
    events: list[str] = []

    class Context:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return None

    class Engine:
        def connect(self):
            return Context()

        def begin(self):
            return Context()

    monkeypatch.setattr(migration, "_read_control_state", lambda _connection: {})
    monkeypatch.setattr(
        migration,
        "_remote_supersede_expected",
        lambda _state, _manifest: old_expected,
    )
    monkeypatch.setattr(migration, "_minio_client", lambda _settings: object())
    monkeypatch.setattr(
        migration,
        "_verify_minio_files",
        lambda *_args: events.append("minio"),
    )
    monkeypatch.setattr(
        migration,
        "_verify_legacy_v2_database",
        lambda *_args: events.append("database"),
    )
    monkeypatch.setattr(
        migration,
        "_cas_superseded_control_to_running",
        lambda *_args, **_kwargs: events.append("cas"),
    )
    migration._preflight_and_adopt_v2_executed_target(
        Engine(), object(), manifest, {}
    )
    assert events == ["minio", "database", "cas"]

    events.clear()
    monkeypatch.setattr(
        migration,
        "_remote_supersede_expected",
        lambda _state, _manifest: None,
    )
    migration._preflight_and_adopt_v2_executed_target(
        Engine(), object(), manifest, {}
    )
    assert events == []


def test_supersede_minio_failure_prevents_database_and_cas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _legacy_v2_manifest()
    manifest = {
        "plan_digest": "b" * 64,
        "supersedes": migration._validate_v2_supersede_manifest(old),
    }
    events: list[str] = []

    class Context:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return None

    class Engine:
        def connect(self):
            return Context()

    monkeypatch.setattr(migration, "_read_control_state", lambda _connection: {})
    monkeypatch.setattr(
        migration,
        "_remote_supersede_expected",
        lambda *_args: old["state"]["target_expected"],
    )
    monkeypatch.setattr(migration, "_minio_client", lambda _settings: object())

    def fail_minio(*_args):
        events.append("minio")
        raise migration.MigrationError("MinIO tamper")

    monkeypatch.setattr(migration, "_verify_minio_files", fail_minio)
    monkeypatch.setattr(
        migration,
        "_verify_legacy_v2_database",
        lambda *_args: events.append("database"),
    )
    monkeypatch.setattr(
        migration,
        "_cas_superseded_control_to_running",
        lambda *_args, **_kwargs: events.append("cas"),
    )
    with pytest.raises(migration.MigrationError, match="tamper"):
        migration._preflight_and_adopt_v2_executed_target(
            Engine(), object(), manifest, {}
        )
    assert events == ["minio"]


def test_supersede_cas_is_exact_and_precedes_managed_drop() -> None:
    old = _legacy_v2_manifest()
    manifest = {
        "plan_digest": "b" * 64,
        "supersedes": migration._validate_v2_supersede_manifest(old),
    }

    class Result:
        rowcount = 1

    class Connection:
        def __init__(self) -> None:
            self.statement = ""
            self.parameters = ()

        def exec_driver_sql(self, statement, parameters):
            self.statement = statement
            self.parameters = parameters
            return Result()

    connection = Connection()
    migration._cas_superseded_control_to_running(
        connection,
        manifest=manifest,
        old_expected=old["state"]["target_expected"],
    )
    assert "status='running'" in connection.statement
    assert "status='executed'" in connection.statement
    assert "BINARY expected_json=BINARY %s" in connection.statement
    assert "DROP" not in connection.statement
    assert connection.parameters[0] == manifest["plan_digest"]
    assert connection.parameters[3] == old["plan_digest"]
    assert connection.parameters[4] == migration._canonical_json(
        old["state"]["target_expected"]
    )


def test_minio_versioning_probe_never_mutates_bucket() -> None:
    class NotImplementedS3(Exception):
        code = "NotImplemented"

    class Client:
        def __init__(self, result=None, error: Exception | None = None) -> None:
            self.result = result
            self.error = error

        def get_bucket_versioning(self, _bucket):
            if self.error:
                raise self.error
            return type("Versioning", (), {"status": self.result})()

        def set_bucket_versioning(self, *_args, **_kwargs):
            raise AssertionError("能力探测不得修改 bucket")

        def list_objects(self, *_args, **kwargs):
            assert kwargs["include_version"] is True
            return []

    assert migration._probe_minio_versioning(Client("Enabled"), "ontology") == "Enabled"
    assert migration._probe_minio_versioning(Client(""), "ontology") == "Supported"

    class ReadVersionsUnsupported(Client):
        def list_objects(self, *_args, **_kwargs):
            raise NotImplementedS3()

    assert (
        migration._probe_minio_versioning(
            ReadVersionsUnsupported(""), "ontology"
        )
        == "Unsupported"
    )
    assert (
        migration._probe_minio_versioning(
            Client(error=NotImplementedS3()), "ontology"
        )
        == "Unsupported"
    )
    with pytest.raises(migration.MigrationError, match="dry-run manifest"):
        migration._assert_minio_versioning_capability(
            Client(error=NotImplementedS3()), "ontology", "Enabled"
        )


def test_unversioned_minio_upload_retry_is_idempotent_and_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"durable-unversioned-object"
    digest = migration.hashlib.sha256(content).hexdigest()
    source = tmp_path / "source.bin"
    source.write_bytes(content)
    file_id = "a" * 32
    key = (
        "ontology-business/tenants/t/scenarios/"
        f"{migration.MEDICAL_SCENARIO_ID}/data-sources/"
        f"{migration.MEDICAL_BUCKET_SOURCE_ID}/files/{file_id}/source.bin"
    )

    class NotImplementedS3(Exception):
        code = "NotImplemented"

    class Stat:
        size = len(content)
        metadata = {"x-amz-meta-sha256": digest}
        version_id = None
        etag = "etag-unversioned"

    class Response:
        def __init__(self) -> None:
            self.done = False

        def read(self, _size):
            if self.done:
                return b""
            self.done = True
            return content

        def close(self):
            return None

        def release_conn(self):
            return None

    class Listed:
        is_dir = False

        def __init__(self, object_name: str) -> None:
            self.object_name = object_name

    class FakeClient:
        def __init__(self) -> None:
            self.put_calls = 0
            self.list_calls = 0
            self.extra_object = False

        def bucket_exists(self, _bucket):
            return True

        def get_bucket_versioning(self, _bucket):
            raise NotImplementedS3()

        def set_bucket_versioning(self, *_args, **_kwargs):
            raise AssertionError("Unsupported 服务不得调用 set_bucket_versioning")

        def stat_object(self, *_args, **kwargs):
            assert "version_id" not in kwargs
            return Stat()

        def get_object(self, *_args, **kwargs):
            assert "version_id" not in kwargs
            return Response()

        def put_object(self, *_args, **_kwargs):
            self.put_calls += 1
            raise AssertionError("相同对象重试不得覆盖")

        def list_objects(self, *_args, **kwargs):
            assert kwargs == {"prefix": key.split("data-sources/", 1)[0], "recursive": True}
            self.list_calls += 1
            listed = [Listed(key)]
            if self.extra_object:
                listed.append(Listed(key + ".unexpected"))
            return listed

    settings = argparse.Namespace(
        minio_endpoint="minio.example",
        minio_bucket="ontology",
        minio_prefix="ontology-business",
        minio_secure=True,
    )
    manifest = {
        "target": {
            "minio": {
                "versioning": migration.MINIO_VERSIONING_UNSUPPORTED,
                "object_key_strategy": migration.MINIO_OBJECT_KEY_STRATEGY,
            }
        },
        "source": {
            "files": [
                {
                    "file_id": file_id,
                    "scenario_id": migration.MEDICAL_SCENARIO_ID,
                    "data_source_id": migration.MEDICAL_BUCKET_SOURCE_ID,
                    "source_path": str(source),
                    "size": len(content),
                    "sha256": digest,
                    "mime": "application/octet-stream",
                    "bucket_name": "ontology",
                    "object_key": key,
                    "object_url": migration.build_object_url(
                        endpoint="", secure=True, bucket="ontology", object_key=key
                    ),
                }
            ]
        },
    }
    monkeypatch.setattr(migration, "EXPECTED_BUCKET_FILE_COUNT", 1)
    client = FakeClient()
    first = migration._upload_files_to_minio(client, settings, manifest)
    second = migration._upload_files_to_minio(client, settings, manifest)
    assert first[file_id]["object_version_id"] == ""
    assert second == first
    assert first[file_id]["etag"] == "etag-unversioned"
    assert client.put_calls == 0
    assert client.list_calls == 2
    client.extra_object = True
    with pytest.raises(migration.MigrationError, match="对象集合"):
        migration._verify_minio_files(
            client,
            {
                "files": list(first.values()),
                "minio": {
                    "versioning": migration.MINIO_VERSIONING_UNSUPPORTED,
                    "object_key_strategy": migration.MINIO_OBJECT_KEY_STRATEGY,
                    "prefix": "ontology-business",
                },
            },
        )


def test_minio_verify_does_not_fall_back_from_missing_recorded_version() -> None:
    class MissingVersion(Exception):
        code = "NoSuchVersion"

    class Stat:
        size = 3
        metadata = {"x-amz-meta-sha256": "a" * 64}
        version_id = "new-version"

    class FakeClient:
        def stat_object(self, _bucket, _key, **kwargs):
            if kwargs.get("version_id") == "deleted-old-version":
                raise MissingVersion()
            return Stat()

    with pytest.raises(migration.MigrationError, match="不存在"):
        migration._assert_minio_object(
            FakeClient(),
            bucket="ontology",
            object_key="same-key",
            expected_size=3,
            expected_sha256="a" * 64,
            expected_version_id="deleted-old-version",
        )


def test_minio_metadata_digest_never_replaces_byte_verification() -> None:
    class Stat:
        size = 3
        metadata = {"x-amz-meta-sha256": migration.hashlib.sha256(b"good").hexdigest()}
        version_id = "v1"

    class Response:
        def __init__(self) -> None:
            self._sent = False

        def read(self, _size):
            if self._sent:
                return b""
            self._sent = True
            return b"bad"

        def close(self):
            return None

        def release_conn(self):
            return None

    class FakeClient:
        def get_bucket_versioning(self, _bucket):
            return type("Versioning", (), {"status": "Enabled"})()

        def stat_object(self, *_args, **_kwargs):
            return Stat()

        def get_object(self, *_args, **_kwargs):
            return Response()

    with pytest.raises(migration.MigrationError, match="SHA-256"):
        migration._assert_minio_object(
            FakeClient(),
            bucket="ontology",
            object_key="key",
            expected_size=3,
            expected_sha256=migration.hashlib.sha256(b"good").hexdigest(),
            expected_version_id="v1",
        )


def test_minio_verify_rejects_extra_versions_and_delete_markers() -> None:
    content = b"abc"
    digest = migration.hashlib.sha256(content).hexdigest()

    class Stat:
        size = len(content)
        metadata = {"x-amz-meta-sha256": digest}
        version_id = "v1"
        etag = "etag-v1"

    class Response:
        def __init__(self) -> None:
            self.done = False

        def read(self, _size):
            if self.done:
                return b""
            self.done = True
            return content

        def close(self):
            return None

        def release_conn(self):
            return None

    class Listed:
        def __init__(self, version_id: str, delete_marker: bool) -> None:
            self.object_name = (
                "ontology-business/tenants/t/scenarios/"
                f"{migration.MEDICAL_SCENARIO_ID}/data-sources/d/files/f/name"
            )
            self.version_id = version_id
            self.is_delete_marker = delete_marker
            self.is_dir = False

    class FakeClient:
        def get_bucket_versioning(self, _bucket):
            return type("Versioning", (), {"status": "Enabled"})()

        def stat_object(self, *_args, **_kwargs):
            return Stat()

        def get_object(self, *_args, **_kwargs):
            return Response()

        def list_objects(self, *_args, **kwargs):
            assert kwargs["include_version"] is True
            return [Listed("v1", False), Listed("v0", True)]

    key = Listed("v1", False).object_name
    expected = {
        "files": [
            {
                "file_id": "f",
                "scenario_id": migration.MEDICAL_SCENARIO_ID,
                "bucket_name": "ontology",
                "object_key": key,
                "object_url": migration.build_object_url(
                    endpoint="", secure=True, bucket="ontology", object_key=key
                ),
                "object_version_id": "v1",
                "etag": "etag-v1",
                "size": len(content),
                "sha256": digest,
            }
        ],
        "minio": {
            "versioning": migration.MINIO_VERSIONING_ENABLED,
            "object_key_strategy": migration.MINIO_OBJECT_KEY_STRATEGY,
            "prefix": "ontology-business",
        },
    }
    with pytest.raises(migration.MigrationError, match="版本集合"):
        migration._verify_minio_files(FakeClient(), expected)


def test_recollected_platform_must_equal_reviewed_manifest() -> None:
    reviewed = {"path": "platform.db", "tables": {"a": {"row_count": 1}}}
    platform = {**copy.deepcopy(reviewed), "_rows": {}, "_schemas": {}}
    migration._assert_collected_platform_matches_manifest(
        platform, {"source": {"platform": reviewed}}
    )
    platform["tables"]["a"]["row_count"] = 2
    with pytest.raises(migration.MigrationError, match="实际采集"):
        migration._assert_collected_platform_matches_manifest(
            platform, {"source": {"platform": reviewed}}
        )


def test_default_env_path_is_backend_env(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    arguments = argparse.Namespace(
        backend_root=str(backend),
        data_root=None,
        platform_db=None,
        buckets_root=None,
        manifest=None,
        env_file=None,
    )
    paths = migration._default_paths(arguments)
    assert paths.env_file == backend.resolve() / ".env"
