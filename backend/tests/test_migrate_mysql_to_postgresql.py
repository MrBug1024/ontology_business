from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, JSON, MetaData, String, Table, create_engine, select

from scripts import migrate_mysql_to_postgresql as migration


def _settings(tmp_path: Path) -> migration.MigrationSettings:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "ANNUAL_MYSQL_HOST=mysql.internal",
                "ANNUAL_MYSQL_PORT=3306",
                "ANNUAL_MYSQL_DATABASE=ontology_business",
                "ANNUAL_MYSQL_USER=root",
                "ANNUAL_MYSQL_PASSWORD=mysql-secret",
                "POSTGRESQL_HOST=127.0.0.1",
                "POSTGRESQL_PORT=5432",
                "POSTGRESQL_DATABASE=",
                "POSTGRESQL_USER=postgres",
                "POSTGRESQL_PASSWORD=postgres-secret",
                "MINIO_ALIYUN_ENDPOINT=minio.example.test",
                "MINIO_ALIYUN_ACCESS_KEY_ID=minio-access",
                "MINIO_ALIYUN_ACCESS_KEY_SECRET=minio-secret",
                "MINIO_ALIYUN_FILE_PATH=ontology-business/",
                "MINIO_BUCKETNAME=ontology",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return migration.load_settings(
        backend_root=tmp_path,
        env_file=env_file,
        manifest_path=tmp_path / "manifest.json",
        environ={},
    )


def _manifest(*, completed: tuple[str, ...] = ("plan",)) -> dict:
    value = {
        "format_version": migration.MANIFEST_FORMAT_VERSION,
        "migration_name": migration.MIGRATION_NAME,
        "run_id": "a" * 32,
        "created_at": "2026-08-27T00:00:00.000000Z",
        "plan_digest": "b" * 64,
        "contract": {"scenario_ids": list(migration.TARGET_SCENARIO_IDS)},
        "connections": {},
        "source": {"source_fingerprint": "c" * 64},
        "target_probe": {},
        "confirmations": {
            phase: f"{phase}-token" for phase in migration.MUTATING_PHASES
        },
        "phase_state": {
            phase: {"status": "complete"} for phase in completed
        },
        "checkpoints": {},
        "archive": {"datasets": {}},
        "verification": {},
    }
    migration._set_manifest_digest(value)
    return value


def test_contract_is_exactly_two_scenarios_and_nineteen_base_relations() -> None:
    assert migration.TARGET_SCENARIO_IDS == (
        "56e2006148e8499e8599f5c7c8145e60",
        "cc5d3ff36d2a468596dfa9f8ef2995da",
    )
    assert sum(len(scenario.relations) for scenario in migration.SCENARIOS) == 19
    assert {scenario.id for scenario in migration.SCENARIOS} == set(
        migration.TARGET_SCENARIO_IDS
    )


def test_exact_scenario_validation_rejects_extra_or_missing_rows() -> None:
    migration._validate_exact_scenarios(
        [{"id": scenario_id} for scenario_id in migration.TARGET_SCENARIO_IDS]
    )
    with pytest.raises(migration.MigrationError, match="unexpected"):
        migration._validate_exact_scenarios(
            [
                *({"id": scenario_id} for scenario_id in migration.TARGET_SCENARIO_IDS),
                {"id": "f" * 32},
            ]
        )
    with pytest.raises(migration.MigrationError, match="missing"):
        migration._validate_exact_scenarios(
            [{"id": migration.BOOKKEEPING_SCENARIO_ID}]
        )


def test_settings_default_new_database_and_never_expose_credentials(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert settings.postgresql_target_database == "ontology_platform"
    assert settings.postgresql_owner_role == "ontology_owner"
    assert settings.postgresql_runtime_role == "ontology_app"
    assert settings.minio_secure is True
    rendered = migration._canonical_json(settings.public_summary())
    assert "mysql-secret" not in rendered
    assert "postgres-secret" not in rendered
    assert "minio-secret" not in rendered
    assert "minio-access" not in rendered


def test_settings_requires_explicit_http_for_insecure_minio(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    text = settings.env_file.read_text(encoding="utf-8").replace(
        "MINIO_ALIYUN_ENDPOINT=minio.example.test",
        "MINIO_ALIYUN_ENDPOINT=http://minio.example.test",
    )
    settings.env_file.write_text(text, encoding="utf-8")
    insecure = migration.load_settings(
        backend_root=tmp_path,
        env_file=settings.env_file,
        environ={},
    )
    assert insecure.minio_secure is False


def test_settings_reject_unsafe_postgresql_identifier(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    text = settings.env_file.read_text(encoding="utf-8").replace(
        "POSTGRESQL_DATABASE=", 'POSTGRESQL_DATABASE=bad";DROP DATABASE postgres;--'
    )
    settings.env_file.write_text(text, encoding="utf-8")
    with pytest.raises(migration.MigrationError, match="标识符"):
        migration.load_settings(
            backend_root=tmp_path,
            env_file=settings.env_file,
            environ={},
        )


def test_row_hasher_is_stable_for_json_decimal_and_utc() -> None:
    first = migration.RowHasher(("id", "amount", "at", "document"))
    second = migration.RowHasher(("id", "amount", "at", "document"))
    first.update(
        {
            "id": 1,
            "amount": Decimal("1.2300"),
            "at": datetime(2026, 8, 27, tzinfo=timezone.utc),
            "document": {"b": 2, "a": [1, True]},
        }
    )
    second.update(
        {
            "id": 1,
            "amount": Decimal("1.2300"),
            "at": datetime(2026, 8, 27),
            "document": {"a": [1, True], "b": 2},
        }
    )
    assert first.count == second.count == 1
    assert first.hexdigest == second.hexdigest


def test_sanitize_and_dataset_config_leave_no_connector_credentials() -> None:
    source = {
        "host": "db",
        "password": "bad",
        "nested": {"access_key_id": "bad", "safe": 1},
        "tokens": [{"api_token": "bad", "name": "kept"}],
    }
    sanitized = migration._sanitize_mapping(source)
    assert sanitized == {
        "host": "db",
        "nested": {"safe": 1},
    }
    connector = migration.dataset_connector_config(
        dataset_id="d" * 32,
        dataset_version_id="v" * 32,
        binding_id="b" * 32,
    )
    assert connector["adapter"] == "dataset"
    assert "host" not in connector
    assert migration._sanitize_mapping(connector) == connector


def test_manifest_digest_ignores_progress_but_detects_plan_tampering() -> None:
    manifest = _manifest()
    migration._verify_manifest_digest(manifest)
    manifest["phase_state"]["bootstrap"] = {"status": "running"}
    manifest["checkpoints"]["x"] = {"payload": {"anything": True}}
    migration._verify_manifest_digest(manifest)
    manifest["contract"]["scenario_ids"].append("f" * 32)
    with pytest.raises(migration.MigrationError, match="篡改"):
        migration._verify_manifest_digest(manifest)


def test_confirmation_and_prerequisite_checks_fail_before_mutation() -> None:
    manifest = _manifest()
    with pytest.raises(migration.MigrationError, match="确认令牌"):
        migration._require_confirmation(manifest, "bootstrap", "wrong")
    migration._require_confirmation(manifest, "bootstrap", "bootstrap-token")
    with pytest.raises(migration.MigrationError, match="bootstrap"):
        migration._require_prerequisites(manifest, "archive")


def test_checkpoint_is_idempotent_but_never_overwritten() -> None:
    manifest = _manifest()
    migration._put_checkpoint(
        manifest, stage="archive", item_key="one", payload={"sha": "a"}
    )
    migration._put_checkpoint(
        manifest, stage="archive", item_key="one", payload={"sha": "a"}
    )
    with pytest.raises(migration.MigrationError, match="拒绝覆盖"):
        migration._put_checkpoint(
            manifest, stage="archive", item_key="one", payload={"sha": "b"}
        )


def test_object_key_is_content_addressed_and_unicode_relation_is_safe(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    first = migration._object_key(
        settings,
        run_id="a" * 32,
        dataset_key="medical-insurance-audit",
        relation_key="规则表",
        content_sha256="1" * 64,
        suffix="parquet",
    )
    second = migration._object_key(
        settings,
        run_id="a" * 32,
        dataset_key="medical-insurance-audit",
        relation_key="规则表",
        content_sha256="2" * 64,
        suffix="parquet",
    )
    assert first != second
    assert first.endswith(f"{'1' * 64}.parquet")
    assert ".." not in first
    assert first.startswith("ontology-business/migrations/")


def test_common_copy_columns_reject_new_required_column_without_default() -> None:
    source_metadata = MetaData()
    target_metadata = MetaData()
    source = Table(
        "things",
        source_metadata,
        Column("id", String(32), primary_key=True),
    )
    target = Table(
        "things",
        target_metadata,
        Column("id", String(32), primary_key=True),
        Column("required_value", String(20), nullable=False),
    )
    with pytest.raises(migration.MigrationError, match="必填列"):
        migration._common_copy_columns(source, target)


def test_target_value_normalization_handles_mysql_boolean_json_and_utc() -> None:
    metadata = MetaData()
    table = Table(
        "values",
        metadata,
        Column("flag", Boolean),
        Column("document", JSON),
        Column("at", DateTime(timezone=True)),
        Column("number", Integer),
    )
    assert migration._normalize_target_value(0, table.c.flag) is False
    assert migration._normalize_target_value('{"b":2,"a":1}', table.c.document) == {
        "a": 1,
        "b": 2,
    }
    normalized = migration._normalize_target_value(
        datetime(2026, 8, 27), table.c.at
    )
    assert normalized.tzinfo == timezone.utc
    assert migration._normalize_target_value(3, table.c.number) == 3


def test_arrow_type_keeps_float_out_of_exact_decimal_branch() -> None:
    column = Column("amount", Float())
    arrow_type = migration._arrow_type(column)
    assert str(arrow_type) == "double"


def test_stream_rows_does_not_mutate_connection_execution_options() -> None:
    metadata = MetaData()
    table = Table("rows", metadata, Column("id", Integer, primary_key=True))
    engine = create_engine("sqlite:///:memory:")
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(table.insert(), [{"id": 1}, {"id": 2}])
            before = connection.get_execution_options()
            rows = list(
                migration._stream_rows(connection, table, batch_size=100)
            )
            assert connection.get_execution_options() == before
            assert rows == [{"id": 1}, {"id": 2}]
    finally:
        engine.dispose()


def test_normalize_orphans_only_applies_declared_nullable_set_null() -> None:
    metadata = MetaData()
    Table("parents", metadata, Column("id", Integer, primary_key=True))
    children = Table(
        "children",
        metadata,
        Column("id", Integer, primary_key=True),
        Column(
            "parent_id",
            Integer,
            ForeignKey("parents.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    engine = create_engine("sqlite:///:memory:")
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(children.insert(), {"id": 1, "parent_id": 999})
            normalized = migration._normalize_nullable_set_null_orphans(
                connection, metadata
            )
            assert normalized == [
                {
                    "table": "children",
                    "constraint": "<unnamed>",
                    "local_columns": ["parent_id"],
                    "referenced_table": "parents",
                    "referenced_columns": ["id"],
                    "count": 1,
                }
            ]
            assert connection.execute(
                select(children.c.parent_id).where(children.c.id == 1)
            ).scalar_one() is None
    finally:
        engine.dispose()


def test_self_referencing_fk_validation_distinguishes_valid_and_orphaned_rows() -> None:
    metadata = MetaData()
    nodes = Table(
        "nodes",
        metadata,
        Column("id", Integer, primary_key=True),
        Column(
            "parent_id",
            Integer,
            ForeignKey("nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    engine = create_engine("sqlite:///:memory:")
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                nodes.insert(),
                [
                    {"id": 1, "parent_id": None},
                    {"id": 2, "parent_id": 1},
                    {"id": 3, "parent_id": 999},
                ],
            )
            assert migration._foreign_key_violations(connection, metadata) == [
                {
                    "table": "nodes",
                    "constraint": "<unnamed>",
                    "local_columns": ["parent_id"],
                    "referenced_table": "nodes",
                    "referenced_columns": ["id"],
                    "count": 1,
                }
            ]
            normalized = migration._normalize_nullable_set_null_orphans(
                connection, metadata
            )
            assert normalized[0]["count"] == 1
            assert migration._foreign_key_violations(connection, metadata) == []
            assert connection.execute(
                select(nodes.c.parent_id).where(nodes.c.id == 2)
            ).scalar_one() == 1
            assert connection.execute(
                select(nodes.c.parent_id).where(nodes.c.id == 3)
            ).scalar_one() is None
    finally:
        engine.dispose()


def test_cutover_env_replaces_only_postgresql_runtime_identity() -> None:
    original = """# keep comment
ANNUAL_MYSQL_HOST=mysql.internal
ANNUAL_MYSQL_PASSWORD=rollback-secret
POSTGRESQL_DATABASE=
POSTGRESQL_USER=postgres
POSTGRESQL_PASSWORD=admin-secret
POSTGRESQL_RUNTIME_PASSWORD=runtime-secret
MINIO_BUCKETNAME=ontology
"""
    rendered = migration._render_cutover_env(
        original,
        database="ontology_platform",
        runtime_user="ontology_app",
        runtime_password="runtime-secret",
    )
    assert "ANNUAL_MYSQL_PASSWORD=rollback-secret" in rendered
    assert "POSTGRESQL_DATABASE=ontology_platform" in rendered
    assert "POSTGRESQL_USER=ontology_app" in rendered
    assert "POSTGRESQL_PASSWORD=runtime-secret" in rendered
    assert "POSTGRESQL_RUNTIME_PASSWORD=" not in rendered
    assert "admin-secret" not in rendered


def test_default_cli_phase_is_plan_and_plan_is_only_implicit_action() -> None:
    parser = migration._build_parser()
    assert parser.parse_args([]).phase == "plan"
    assert "plan" not in migration.MUTATING_PHASES
    assert set(migration.MUTATING_PHASES) == {
        "bootstrap",
        "archive",
        "import",
        "cutover",
    }


def test_script_contains_no_source_destructive_sql() -> None:
    source = Path(migration.__file__).read_text(encoding="utf-8").upper()
    assert "DROP DATABASE" not in source
    assert "DROP TABLE" not in source
    assert "TRUNCATE TABLE" not in source
    assert "DELETE FROM" not in source


def test_bootstrap_uses_dynamic_alembic_head_and_no_schema_bypass() -> None:
    source = Path(migration.__file__).read_text(encoding="utf-8")
    assert "script.get_heads()" in source
    assert 'command.upgrade(config, "head")' in source
    assert "metadata.create_all" not in source
    assert "MIGRATION_TRACKING_DDL" not in source


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _RoleConnection:
    def __init__(self, *, role_row=None, memberships=()):
        self.role_row = role_row
        self.memberships = tuple(memberships)
        self.statements = []

    def execute(self, statement, parameters):
        rendered = str(statement)
        self.statements.append((rendered, parameters))
        if "pg_auth_members" in rendered:
            return _Rows([(name,) for name in self.memberships])
        return _Rows([] if self.role_row is None else [self.role_row])


def _safe_role_row(*, can_login: bool) -> dict:
    return {
        "rolname": "role",
        "rolsuper": False,
        "rolcreaterole": False,
        "rolcreatedb": False,
        "rolcanlogin": can_login,
        "rolinherit": False,
        "rolbypassrls": False,
        "rolreplication": False,
    }


def test_role_row_reads_every_security_relevant_postgresql_flag() -> None:
    connection = _RoleConnection(
        role_row=("ontology_app", False, False, False, True, False, False, False)
    )
    role = migration._role_row(connection, "ontology_app")
    assert role == {
        "rolname": "ontology_app",
        "rolsuper": False,
        "rolcreaterole": False,
        "rolcreatedb": False,
        "rolcanlogin": True,
        "rolinherit": False,
        "rolbypassrls": False,
        "rolreplication": False,
    }
    query = connection.statements[0][0]
    assert "rolinherit" in query
    assert "rolbypassrls" in query
    assert "rolreplication" in query


@pytest.mark.parametrize(
    "unsafe_flag",
    (
        "rolsuper",
        "rolcreaterole",
        "rolcreatedb",
        "rolinherit",
        "rolbypassrls",
        "rolreplication",
    ),
)
def test_role_policy_rejects_each_privilege_escalation_flag(unsafe_flag: str) -> None:
    role = _safe_role_row(can_login=True)
    role[unsafe_flag] = True
    with pytest.raises(migration.MigrationError, match=unsafe_flag):
        migration._validate_role_policy(role, label="runtime", can_login=True)


def test_role_policy_enforces_expected_login_capability() -> None:
    with pytest.raises(migration.MigrationError, match="rolcanlogin"):
        migration._validate_role_policy(
            _safe_role_row(can_login=False), label="runtime", can_login=True
        )
    migration._validate_role_policy(
        _safe_role_row(can_login=False), label="owner", can_login=False
    )


def test_runtime_role_rejects_any_pg_auth_members_membership() -> None:
    connection = _RoleConnection(memberships=("pg_read_all_data", "operator"))
    assert migration._runtime_role_memberships(connection, "ontology_app") == (
        "pg_read_all_data",
        "operator",
    )
    with pytest.raises(migration.MigrationError, match="pg_read_all_data"):
        migration._reject_runtime_memberships(connection, "ontology_app")


def test_role_ddl_explicitly_disables_inheritance_rls_bypass_and_replication() -> None:
    source = Path(migration.__file__).read_text(encoding="utf-8")
    assert (
        '"ALTER ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "\n'
        '            "NOINHERIT NOBYPASSRLS NOREPLICATION"'
    ) in source
    assert source.count("NOINHERIT NOBYPASSRLS NOREPLICATION") >= 3
