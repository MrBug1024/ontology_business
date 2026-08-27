from __future__ import annotations

import ast
from contextlib import contextmanager
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import String, create_engine, inspect, text
from sqlalchemy.dialects import mysql, sqlite

from app import database, external_api_models
from app.models import ActionExecutionLog, ScenarioModelDraftResource


PERSISTED_ACTION_EXECUTION_STATUSES = {
    "running",
    "success",
    "failed",
    "confirmation_required",
    "dry_run",
    "awaiting_approval",
}


def test_action_execution_status_column_fits_the_persisted_domain() -> None:
    installed_length = ActionExecutionLog.__table__.c.status.type.length

    assert installed_length == 32
    assert max(map(len, PERSISTED_ACTION_EXECUTION_STATUSES)) <= installed_length
    assert len("confirmation_required") == 21


def test_every_orm_datetime_preserves_microseconds_only_on_mysql() -> None:
    # Reference the split-out credential model so its tables are registered.
    assert external_api_models.ExternalApiKey.__table__.name == "external_api_keys"
    mysql_dialect = mysql.dialect()
    sqlite_dialect = sqlite.dialect()
    datetime_columns = [
        (table.name, column.name, column.type)
        for table in database.Base.metadata.tables.values()
        for column in table.columns
        if str(column.type.compile(dialect=sqlite_dialect)).upper() == "DATETIME"
    ]

    assert datetime_columns
    assert all(
        str(column_type.compile(dialect=mysql_dialect)).upper() == "DATETIME(6)"
        for _table, _column, column_type in datetime_columns
    )
    assert all(
        str(column_type.compile(dialect=sqlite_dialect)).upper() == "DATETIME"
        for _table, _column, column_type in datetime_columns
    )


def test_nullable_orphan_repair_statement_uses_mysql_identifier_quoting() -> None:
    statement = database._nullable_orphan_repair_statement(
        "child-table",
        "parent-id",
        "parent-table",
    )

    sql = str(statement.compile(dialect=mysql.dialect()))

    assert "UPDATE `child-table`" in sql
    assert "`child-table`.`parent-id`" in sql
    assert "FROM `parent-table`" in sql
    assert '"child-table"' not in sql


def test_mysql_add_column_ddl_uses_dialect_identifier_quoting() -> None:
    connection = SimpleNamespace(dialect=mysql.dialect())

    sql = database._add_column_ddl(
        connection,
        "ontology-entities",
        "api-name",
        "VARCHAR(100) NOT NULL DEFAULT ''",
    )

    assert sql.startswith(
        "ALTER TABLE `ontology-entities` ADD COLUMN `api-name` VARCHAR(100)"
    )


def test_relation_duplicate_query_aliases_mysql_derived_table() -> None:
    sql = str(
        database._relation_duplicate_groups_statement().compile(
            dialect=mysql.dialect()
        )
    )

    assert ") AS duplicate_edges" in sql


class CandidateIdsResult:
    def scalars(self):
        return self

    def all(self) -> list[str]:
        return ["property-1", "property-2"]


class TitlePromotionConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: Any, *_args: Any, **_kwargs: Any):
        compiled = str(statement.compile(dialect=mysql.dialect()))
        self.statements.append(compiled)
        if len(self.statements) == 1:
            return CandidateIdsResult()
        return None


def test_mysql_title_key_promotion_never_updates_from_self_subquery() -> None:
    connection = TitlePromotionConnection()

    database._promote_legacy_title_keys(connection)

    assert len(connection.statements) == 2
    assert connection.statements[0].startswith("SELECT MIN(candidate.id)")
    assert connection.statements[1].startswith("UPDATE ontology_properties")
    assert "SELECT" not in connection.statements[1]


def test_driver_sql_calls_never_use_sqlalchemy_named_parameters() -> None:
    source_path = Path(database.__file__)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    violations: list[int] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "exec_driver_sql"
            and node.args
        ):
            continue
        literal_text = "".join(
            child.value
            for child in ast.walk(node.args[0])
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        )
        if re.search(r"(?<!:):[A-Za-z_][A-Za-z0-9_]*", literal_text):
            violations.append(node.lineno)

    assert violations == []


def test_nullable_orphan_repair_preserves_valid_rows(tmp_path: Path) -> None:
    isolated_engine = create_engine(f"sqlite:///{(tmp_path / 'orphans.db').as_posix()}")
    try:
        with isolated_engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE business_scenarios (id VARCHAR(32) PRIMARY KEY)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE assistant_audit_logs ("
                "id VARCHAR(32) PRIMARY KEY, scenario_id VARCHAR(32) NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO business_scenarios (id) VALUES ('valid')"
            )
            connection.exec_driver_sql(
                "INSERT INTO assistant_audit_logs (id, scenario_id) VALUES "
                "('keep', 'valid'), ('repair', 'missing'), ('empty', NULL)"
            )

        with patch.object(database, "engine", isolated_engine):
            database._repair_nullable_orphan_references()

        with isolated_engine.connect() as connection:
            rows = dict(
                connection.execute(
                    text("SELECT id, scenario_id FROM assistant_audit_logs")
                ).all()
            )
        assert rows == {"keep": "valid", "repair": None, "empty": None}
    finally:
        isolated_engine.dispose()


def test_bucket_storage_metadata_migration_is_idempotent(tmp_path: Path) -> None:
    isolated_engine = create_engine(f"sqlite:///{(tmp_path / 'bucket.db').as_posix()}")
    try:
        with isolated_engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE bucket_files (id VARCHAR(32) PRIMARY KEY)"
            )
            connection.exec_driver_sql("INSERT INTO bucket_files (id) VALUES ('legacy')")

        with patch.object(database, "engine", isolated_engine):
            database._migrate_bucket_storage_metadata()
            database._migrate_bucket_storage_metadata()

        with isolated_engine.connect() as connection:
            columns = {
                item["name"]: item for item in inspect(connection).get_columns("bucket_files")
            }
            row = connection.execute(
                text(
                    "SELECT storage_provider, bucket_name, object_key, "
                    "object_version_id, etag, object_url FROM bucket_files "
                    "WHERE id = 'legacy'"
                )
            ).one()
        expected = {
            "storage_provider",
            "bucket_name",
            "object_key",
            "object_version_id",
            "etag",
            "object_url",
        }
        assert expected.issubset(columns)
        assert all(columns[name]["nullable"] is False for name in expected)
        assert tuple(row) == ("local", "", "", "", "", "")
    finally:
        isolated_engine.dispose()


def test_attachment_storage_metadata_migration_is_idempotent(tmp_path: Path) -> None:
    isolated_engine = create_engine(
        f"sqlite:///{(tmp_path / 'attachments.db').as_posix()}"
    )
    try:
        with isolated_engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE assistant_attachments (id VARCHAR(32) PRIMARY KEY)"
            )
            connection.exec_driver_sql(
                "INSERT INTO assistant_attachments (id) VALUES ('legacy')"
            )

        with patch.object(database, "engine", isolated_engine):
            database._migrate_assistant_attachment_lifecycle()
            database._migrate_assistant_attachment_lifecycle()

        with isolated_engine.connect() as connection:
            columns = {
                item["name"]: item
                for item in inspect(connection).get_columns("assistant_attachments")
            }
            row = connection.execute(
                text(
                    "SELECT storage_provider, bucket_name, object_key, "
                    "object_version_id, etag, object_url FROM assistant_attachments "
                    "WHERE id = 'legacy'"
                )
            ).one()
        expected = {
            "storage_provider",
            "bucket_name",
            "object_key",
            "object_version_id",
            "etag",
            "object_url",
        }
        assert expected.issubset(columns)
        assert all(columns[name]["nullable"] is False for name in expected)
        assert tuple(row) == ("none", "", "", "", "", "")
    finally:
        isolated_engine.dispose()


class VarcharInspector:
    @staticmethod
    def get_columns(_table_name: str) -> list[dict[str, Any]]:
        return [
            {"name": "stored_path", "type": String(1000)},
            {"name": "object_key", "type": String(2048)},
            {"name": "object_url", "type": String(1500)},
        ]


def test_mysql_varchar_expansion_only_widens_short_columns() -> None:
    connection = FakeMysqlConnection()

    with patch.object(database, "inspect", return_value=VarcharInspector()):
        database._widen_mysql_varchar_columns(
            connection,
            "bucket_files",
            {
                "stored_path": (4096, "NOT NULL"),
                "object_key": (2048, "NOT NULL DEFAULT ''"),
                "object_url": (4096, "NOT NULL DEFAULT ''"),
            },
        )

    executed = "\n".join(connection.driver_sql)
    assert "MODIFY COLUMN stored_path VARCHAR(4096) NOT NULL" in executed
    assert "MODIFY COLUMN object_url VARCHAR(4096) NOT NULL DEFAULT ''" in executed
    assert "MODIFY COLUMN object_key" not in executed


class FakeMysqlConnection:
    dialect = mysql.dialect()

    def __init__(self) -> None:
        self.driver_sql: list[str] = []
        self.core_sql: list[str] = []

    def exec_driver_sql(self, statement: str, *_args: Any, **_kwargs: Any) -> None:
        self.driver_sql.append(statement)

    def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> None:
        self.core_sql.append(str(statement.compile(dialect=mysql.dialect())))


class FakeEngine:
    def __init__(self, connection: FakeMysqlConnection) -> None:
        self.connection = connection
        self.dialect = connection.dialect

    @contextmanager
    def begin(self):
        yield self.connection


class ActionSafetyInspector:
    def __init__(self, status_length: int) -> None:
        self.status_length = status_length

    @staticmethod
    def has_table(name: str) -> bool:
        return name == "action_execution_logs"

    def get_columns(self, name: str) -> list[dict[str, Any]]:
        assert name == "action_execution_logs"
        return [
            {"name": "status", "type": String(self.status_length)},
            {"name": "mode", "type": String(20)},
            {"name": "idempotency_key", "type": String(120)},
            {"name": "connector_audit", "type": mysql.JSON()},
        ]

    @staticmethod
    def get_indexes(name: str) -> list[dict[str, str]]:
        assert name == "action_execution_logs"
        return [
            {"name": "ix_action_execution_logs_idempotency_key"},
            {"name": "uq_action_execution_logs_idempotency"},
        ]

    @staticmethod
    def get_unique_constraints(name: str) -> list[dict[str, str]]:
        assert name == "action_execution_logs"
        return [{"name": "uq_action_execution_logs_idempotency"}]


@pytest.mark.parametrize(
    ("installed_length", "should_alter"),
    [(20, True), (32, False), (64, False)],
)
def test_action_safety_migration_only_widens_short_mysql_status_columns(
    installed_length: int,
    should_alter: bool,
) -> None:
    connection = FakeMysqlConnection()
    inspector = ActionSafetyInspector(installed_length)

    with (
        patch.object(database, "engine", FakeEngine(connection)),
        patch.object(database, "inspect", return_value=inspector),
    ):
        database._migrate_action_safety()

    status_alters = [
        statement
        for statement in connection.driver_sql
        if "MODIFY COLUMN status" in statement
    ]
    assert bool(status_alters) is should_alter
    if should_alter:
        assert status_alters == [
            "ALTER TABLE action_execution_logs MODIFY COLUMN status "
            "VARCHAR(32) NOT NULL"
        ]


class DatetimePrecisionRows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def mappings(self) -> DatetimePrecisionRows:
        return self

    def __iter__(self):
        return iter(self.rows)


class DatetimePrecisionConnection:
    dialect = mysql.dialect()

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.queries: list[str] = []
        self.driver_sql: list[str] = []

    def execute(self, statement: Any) -> DatetimePrecisionRows:
        self.queries.append(str(statement.compile(dialect=self.dialect)))
        return DatetimePrecisionRows(self.rows)

    def exec_driver_sql(self, statement: str) -> None:
        self.driver_sql.append(statement)


def _datetime_column_row(
    table_name: str,
    column_name: str,
    *,
    precision: int,
    nullable: bool,
    default: object = None,
    extra: str = "",
    comment: str = "",
) -> dict[str, Any]:
    return {
        "table_name": table_name,
        "column_name": column_name,
        "data_type": "datetime",
        "datetime_precision": precision,
        "is_nullable": "YES" if nullable else "NO",
        "column_default": default,
        "extra": extra,
        "column_comment": comment,
    }


def test_mysql_datetime_precision_upgrade_preserves_column_attributes() -> None:
    connection = DatetimePrecisionConnection(
        [
            _datetime_column_row(
                "action_execution_logs",
                "created_at",
                precision=0,
                nullable=False,
            ),
            _datetime_column_row(
                "external_api_keys",
                "expires_at",
                precision=3,
                nullable=True,
            ),
            _datetime_column_row(
                "users",
                "updated_at",
                precision=0,
                nullable=False,
                default="CURRENT_TIMESTAMP",
                extra="DEFAULT_GENERATED on update CURRENT_TIMESTAMP(3)",
                comment="operator's clock",
            ),
            _datetime_column_row(
                "users",
                "created_at",
                precision=6,
                nullable=False,
            ),
            _datetime_column_row(
                "unmanaged_table",
                "created_at",
                precision=0,
                nullable=False,
            ),
        ]
    )

    database._widen_mysql_datetime_precision(connection)

    assert len(connection.queries) == 1
    assert connection.driver_sql == [
        "ALTER TABLE action_execution_logs MODIFY COLUMN created_at "
        "DATETIME(6) NOT NULL",
        "ALTER TABLE external_api_keys MODIFY COLUMN expires_at "
        "DATETIME(6) NULL DEFAULT NULL",
        "ALTER TABLE users MODIFY COLUMN updated_at DATETIME(6) NOT NULL "
        "DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) "
        "COMMENT 'operator''s clock'",
    ]


def test_mysql_datetime_precision_upgrade_matches_generated_default_precision() -> None:
    connection = DatetimePrecisionConnection(
        [
            _datetime_column_row(
                "scenario_model_draft_resources",
                "lineage_started_at",
                precision=0,
                nullable=False,
                default="CURRENT_TIMESTAMP",
                extra="DEFAULT_GENERATED",
            )
        ]
    )

    database._widen_mysql_datetime_precision(connection)

    assert connection.driver_sql == [
        "ALTER TABLE scenario_model_draft_resources "
        "MODIFY COLUMN lineage_started_at DATETIME(6) NOT NULL "
        "DEFAULT CURRENT_TIMESTAMP(6)"
    ]


class DraftResourceInspector:
    def __init__(
        self,
        *,
        lineage_nullable: bool,
        lineage_precision: int = 6,
        lineage_default: object = None,
    ) -> None:
        self.lineage_nullable = lineage_nullable
        self.lineage_precision = lineage_precision
        self.lineage_default = lineage_default

    @staticmethod
    def has_table(name: str) -> bool:
        return name == "scenario_model_draft_resources"

    def get_columns(self, name: str) -> list[dict[str, Any]]:
        assert name == "scenario_model_draft_resources"
        return [
            {
                "name": column.name,
                "type": (
                    mysql.DATETIME(fsp=self.lineage_precision)
                    if column.name == "lineage_started_at"
                    else column.type
                ),
                "nullable": (
                    self.lineage_nullable
                    if column.name == "lineage_started_at"
                    else column.nullable
                ),
                "default": (
                    self.lineage_default
                    if column.name == "lineage_started_at"
                    else None
                ),
            }
            for column in ScenarioModelDraftResource.__table__.columns
        ]

    @staticmethod
    def get_unique_constraints(name: str) -> list[dict[str, str]]:
        assert name == "scenario_model_draft_resources"
        return [{"name": "uq_scenario_model_draft_resource_identity"}]

    @staticmethod
    def get_indexes(name: str) -> list[dict[str, str]]:
        assert name == "scenario_model_draft_resources"
        return [
            {"name": index.name}
            for index in ScenarioModelDraftResource.__table__.indexes
        ]


@pytest.mark.parametrize(
    (
        "lineage_nullable",
        "lineage_precision",
        "lineage_default",
        "should_alter",
    ),
    [
        (False, 6, None, False),
        (True, 6, None, True),
        (False, 0, "CURRENT_TIMESTAMP", True),
    ],
)
def test_draft_resource_migration_normalizes_mysql_lineage_contract(
    lineage_nullable: bool,
    lineage_precision: int,
    lineage_default: object,
    should_alter: bool,
) -> None:
    connection = FakeMysqlConnection()
    inspector = DraftResourceInspector(
        lineage_nullable=lineage_nullable,
        lineage_precision=lineage_precision,
        lineage_default=lineage_default,
    )

    with (
        patch.object(database, "engine", FakeEngine(connection)),
        patch.object(database, "inspect", return_value=inspector),
    ):
        database._migrate_scenario_model_draft_resources()

    lineage_alters = [
        statement
        for statement in connection.driver_sql
        if "MODIFY lineage_started_at" in statement
    ]
    assert bool(lineage_alters) is should_alter
    if should_alter:
        assert lineage_alters == [
            "ALTER TABLE scenario_model_draft_resources "
            "MODIFY lineage_started_at DATETIME(6) NOT NULL"
        ]


def test_mysql_datetime_precision_upgrade_rejects_unknown_extra_attributes() -> None:
    connection = DatetimePrecisionConnection(
        [
            _datetime_column_row(
                "action_execution_logs",
                "created_at",
                precision=0,
                nullable=False,
                extra="STORAGE DISK",
            )
        ]
    )

    with pytest.raises(RuntimeError, match="EXTRA"):
        database._widen_mysql_datetime_precision(connection)
    assert connection.driver_sql == []


def test_datetime_precision_bootstrap_is_a_sqlite_noop() -> None:
    connection = DatetimePrecisionConnection([])
    connection.dialect = sqlite.dialect()

    database._widen_mysql_datetime_precision(connection)

    assert connection.queries == []
    assert connection.driver_sql == []


class FakeInspector:
    @staticmethod
    def has_table(name: str) -> bool:
        return name in {"assistant_attachments", "assistant_threads"}

    @staticmethod
    def get_columns(name: str) -> list[dict[str, str]]:
        if name == "assistant_attachments":
            return [
                {"name": "thread_id"},
                {"name": "consumed_at"},
                {"name": "expires_at"},
            ]
        return [{"name": "id"}]

    @staticmethod
    def get_indexes(_name: str) -> list[dict[str, str]]:
        return [
            {"name": "ix_assistant_attachments_thread_id"},
            {"name": "ix_assistant_attachments_expires_at"},
        ]


def test_mysql_attachment_migration_never_executes_sqlite_triggers() -> None:
    connection = FakeMysqlConnection()
    with (
        patch.object(database, "engine", FakeEngine(connection)),
        patch.object(database, "inspect", return_value=FakeInspector()),
    ):
        database._migrate_assistant_attachment_lifecycle()

    executed = "\n".join([*connection.driver_sql, *connection.core_sql])
    assert "CREATE TRIGGER" not in executed
    assert "RAISE(ABORT" not in executed
    assert "UPDATE assistant_attachments" in executed


class RecordingCursor:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.closed = False

    def execute(self, statement: str) -> None:
        self.statements.append(statement)

    def close(self) -> None:
        self.closed = True


class RecordingDbapiConnection:
    def __init__(self) -> None:
        self.cursor_instance = RecordingCursor()

    def cursor(self) -> RecordingCursor:
        return self.cursor_instance


def test_mysql_connect_hook_forces_innodb_for_every_session() -> None:
    connection = RecordingDbapiConnection()

    database._set_mysql_session_defaults(connection, None)

    assert connection.cursor_instance.statements == [
        "SET SESSION default_storage_engine=InnoDB"
    ]
    assert connection.cursor_instance.closed is True


class ScalarResult:
    def __init__(self, value: str) -> None:
        self.value = value

    def scalar_one(self) -> str:
        return self.value


class RowsResult:
    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self.rows = rows

    def all(self) -> list[tuple[str, str]]:
        return self.rows


class StorageEngineConnection:
    dialect = SimpleNamespace(name="mysql")

    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self.rows = rows
        self.calls = 0

    def execute(self, _statement: Any) -> ScalarResult | RowsResult:
        self.calls += 1
        if self.calls == 1:
            return ScalarResult("InnoDB")
        return RowsResult(self.rows)


def test_mysql_storage_engine_check_rejects_platform_myisam_tables() -> None:
    from app import models  # noqa: F401 - populate Base metadata.

    connection = StorageEngineConnection(
        [("business_scenarios", "MyISAM"), ("unrelated_table", "MyISAM")]
    )

    with pytest.raises(RuntimeError, match="business_scenarios"):
        database._verify_mysql_storage_engine(connection)


def test_mysql_storage_engine_check_accepts_innodb_platform_tables() -> None:
    from app import models  # noqa: F401 - populate Base metadata.

    connection = StorageEngineConnection(
        [("business_scenarios", "InnoDB"), ("unrelated_table", "MyISAM")]
    )

    database._verify_mysql_storage_engine(connection)


def test_mysql_utf8mb4_index_widths_fit_innodb_limit() -> None:
    from app import external_api_models, models  # noqa: F401

    indexed_widths: dict[str, int] = {}
    for table_definition in database.Base.metadata.tables.values():
        for definition in (*table_definition.indexes, *table_definition.constraints):
            columns = getattr(definition, "columns", ())
            width = sum(
                int(getattr(column.type, "length", 0) or 0) * 4
                for column in columns
            )
            if definition.name and width:
                indexed_widths[definition.name] = width

    assert indexed_widths
    assert max(indexed_widths.values()) <= 3072
