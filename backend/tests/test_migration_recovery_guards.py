from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from scripts import migrate_mysql_to_postgresql as migration
from scripts import verify_postgresql_runtime as runtime_verify


def _settings(tmp_path: Path) -> migration.MigrationSettings:
    env_file = tmp_path / ".env"
    env_file.write_text(
        """ANNUAL_MYSQL_HOST=mysql.internal
ANNUAL_MYSQL_PORT=3306
ANNUAL_MYSQL_DATABASE=ontology_business
ANNUAL_MYSQL_USER=ontology_app
ANNUAL_MYSQL_PASSWORD=mysql-secret
DATABASE_BACKEND=mysql
DATABASE_URL=mysql+pymysql://must-be-removed
POSTGRESQL_HOST=postgres.internal
POSTGRESQL_PORT=5432
POSTGRESQL_DATABASE=ontology_platform
POSTGRESQL_USER=postgres
POSTGRESQL_PASSWORD=admin-secret
POSTGRESQL_RUNTIME_PASSWORD=runtime-secret
MINIO_ALIYUN_ENDPOINT=minio.example.test
MINIO_ALIYUN_ACCESS_KEY_ID=minio-access
MINIO_ALIYUN_ACCESS_KEY_SECRET=minio-secret
MINIO_BUCKETNAME=ontology
""",
        encoding="utf-8",
    )
    return migration.MigrationSettings(
        env_file=env_file,
        manifest_path=tmp_path / "manifest.json",
        mysql_host="mysql.internal",
        mysql_port=3306,
        mysql_database="ontology_business",
        mysql_user="ontology_app",
        mysql_password="mysql-secret",
        postgresql_host="postgres.internal",
        postgresql_port=5432,
        postgresql_admin_database="postgres",
        postgresql_target_database="ontology_platform",
        postgresql_admin_user="postgres",
        postgresql_admin_password="admin-secret",
        postgresql_owner_role="ontology_owner",
        postgresql_runtime_role="ontology_app",
        postgresql_readonly_role="ontology_readonly",
        postgresql_runtime_password="runtime-secret",
        minio_endpoint="minio.example.test",
        minio_access_key="minio-access",
        minio_secret_key="minio-secret",
        minio_bucket="ontology",
        minio_prefix="ontology-business",
        minio_secure=True,
    )


def _manifest(*, verify_status: str = "running") -> dict[str, Any]:
    return {
        "run_id": "a" * 32,
        "plan_digest": "b" * 64,
        "created_at": "2026-08-27T00:00:00.000000Z",
        "source": {"source_fingerprint": "c" * 64},
        "confirmations": {
            "bootstrap": "bootstrap-token",
            "import": "import-token",
            "cutover": "cutover-token",
        },
        "phase_state": {
            "plan": {"status": "complete"},
            "bootstrap": {"status": "complete"},
            "archive": {"status": "complete"},
            "import": {"status": "running"},
            "verify": {"status": verify_status},
        },
        "checkpoints": {},
        "verification": {},
    }


class _DriverResult:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class _MySQLConnection:
    def __init__(self, *, read_only: str = "ON"):
        self.read_only = read_only
        self.statements: list[str] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def exec_driver_sql(self, statement: str):
        self.statements.append(statement)
        if statement.startswith("SHOW SESSION VARIABLES"):
            return _DriverResult((("transaction_read_only", self.read_only),))
        return _DriverResult()

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class _MySQLEngine:
    def __init__(self, connection: _MySQLConnection):
        self.connection = connection

    def connect(self):
        return self.connection


def test_mysql_snapshot_enforces_and_confirms_session_read_only() -> None:
    connection = _MySQLConnection()
    with migration._mysql_readonly_snapshot(_MySQLEngine(connection)) as yielded:
        assert yielded is connection
        assert connection.committed is True
        assert connection.statements[-1] == (
            "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
        )
    assert connection.rolled_back is True
    assert connection.closed is True
    assert "SET SESSION TRANSACTION READ ONLY" in connection.statements


def test_mysql_snapshot_fails_closed_when_read_only_cannot_be_confirmed() -> None:
    connection = _MySQLConnection(read_only="OFF")
    with pytest.raises(migration.MigrationError, match="未确认"):
        with migration._mysql_readonly_snapshot(_MySQLEngine(connection)):
            raise AssertionError("snapshot must not be yielded")
    assert connection.closed is True
    assert "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY" not in (
        connection.statements
    )


class _NoopConnection:
    def execute(self, _statement, _parameters=None):
        return None


class _FakeEngine:
    def __init__(self):
        self.connection = _NoopConnection()
        self.disposed = False

    @contextmanager
    def begin(self):
        yield self.connection

    @contextmanager
    def connect(self):
        yield self.connection

    def dispose(self):
        self.disposed = True


def test_completed_bootstrap_still_converges_to_current_alembic_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    manifest = _manifest()
    calls: list[str] = []
    result = {"alembic_heads": ["20260827_04"], "schema_fingerprint": "d" * 64}
    monkeypatch.setattr(
        migration,
        "_ensure_postgresql_roles_and_database",
        lambda _settings: calls.append("roles"),
    )
    monkeypatch.setattr(
        migration,
        "_initialize_target_schema",
        lambda _settings: calls.append("alembic") or result,
    )
    monkeypatch.setattr(
        migration,
        "_grant_runtime_privileges",
        lambda _settings: calls.append("grants"),
    )
    monkeypatch.setattr(migration, "_sync_migration_run", lambda *_a, **_k: None)

    actual = migration.bootstrap_target(
        settings,
        manifest,
        confirmation="bootstrap-token",
    )

    assert actual == result
    assert calls == ["roles", "alembic", "grants"]
    assert manifest["phase_state"]["bootstrap"]["status"] == "complete"
    assert any(
        key.startswith(
            migration._checkpoint_key(
                "bootstrap", migration.BOOTSTRAP_SCHEMA_CHECKPOINT_PREFIX
            )
        )
        for key in manifest["checkpoints"]
    )


def test_import_recovers_committed_target_checkpoint_without_mysql_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    manifest = _manifest()
    recovered = {
        "source_fingerprint": manifest["source"]["source_fingerprint"],
        "post_import_platform": {},
    }
    engine = _FakeEngine()
    monkeypatch.setattr(migration, "_postgres_engine", lambda _settings: engine)
    monkeypatch.setattr(
        migration,
        "_find_target_checkpoint",
        lambda *_args, **_kwargs: (migration.IMPORT_TARGET_CHECKPOINT, recovered),
    )
    monkeypatch.setattr(
        migration,
        "_mysql_engine",
        lambda _settings: (_ for _ in ()).throw(
            AssertionError("MySQL must not be reopened after target commit")
        ),
    )

    result = migration.import_to_postgresql(
        settings,
        manifest,
        confirmation="import-token",
        batch_size=100,
    )

    assert result == recovered
    assert manifest["phase_state"]["import"]["status"] == "complete"
    mirrored = manifest["checkpoints"][
        migration._checkpoint_key("import", migration.IMPORT_TARGET_CHECKPOINT)
    ]
    assert mirrored["authority"] == "postgresql"
    assert engine.disposed is True


def test_verify_recovers_committed_deterministic_checkpoint_without_rescan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    manifest = _manifest()
    imported = {
        "source_fingerprint": manifest["source"]["source_fingerprint"],
        "post_import_platform": {},
    }
    verified = {
        "checkpoint_contract_version": 2,
        "plan_digest": manifest["plan_digest"],
        "source_fingerprint": manifest["source"]["source_fingerprint"],
        "deep_object_verification": True,
    }
    monkeypatch.setattr(migration, "_postgres_engine", lambda _settings: _FakeEngine())
    monkeypatch.setattr(
        migration,
        "_find_target_checkpoint",
        lambda *_args, **_kwargs: (migration.IMPORT_TARGET_CHECKPOINT, imported),
    )
    monkeypatch.setattr(
        migration,
        "_read_target_checkpoint",
        lambda *_args, **_kwargs: verified,
    )
    monkeypatch.setattr(
        migration,
        "_source_inventory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("committed verify checkpoint must be recovered first")
        ),
    )

    result = migration.verify_migration(
        settings,
        manifest,
        batch_size=100,
        deep=True,
    )

    assert result == verified
    assert manifest["verification"] == verified
    assert manifest["phase_state"]["verify"]["status"] == "complete"
    assert "verified_at" not in verified


def test_cutover_failure_after_env_write_converges_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    manifest = _manifest(verify_status="complete")
    manifest["phase_state"]["import"] = {"status": "complete"}
    engine = _FakeEngine()
    remote: dict[tuple[str, str], dict[str, Any]] = {}
    imported = {"source_fingerprint": manifest["source"]["source_fingerprint"]}
    verified = {
        "source_fingerprint": manifest["source"]["source_fingerprint"],
        "deep_object_verification": True,
    }
    fail_final_once = {"value": True}

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(migration, "_postgres_engine", lambda _settings: engine)
    monkeypatch.setattr(migration, "_update_target_run_state", lambda *_a, **_k: None)

    def find_checkpoint(_connection, _manifest, *, stage, item_keys):
        if stage == "import":
            return item_keys[0], imported
        if stage == "verify":
            return item_keys[0], verified
        raise AssertionError(stage)

    def read_checkpoint(_connection, _manifest, *, stage, item_key):
        return remote.get((stage, item_key))

    def record_checkpoint(
        _connection, _manifest, *, stage, item_key, payload, row_count=None
    ):
        del row_count
        if (
            item_key == migration.CUTOVER_FINALIZED_CHECKPOINT
            and fail_final_once["value"]
        ):
            fail_final_once["value"] = False
            raise RuntimeError("simulated ledger outage after env replacement")
        remote[(stage, item_key)] = dict(payload)

    monkeypatch.setattr(migration, "_find_target_checkpoint", find_checkpoint)
    monkeypatch.setattr(migration, "_read_target_checkpoint", read_checkpoint)
    monkeypatch.setattr(migration, "_record_target_checkpoint", record_checkpoint)

    with pytest.raises(migration.MigrationError, match="可安全重跑"):
        migration.cutover_environment(
            settings,
            manifest,
            confirmation="cutover-token",
        )

    live_text = settings.env_file.read_text(encoding="utf-8")
    migration._assert_database_selector(live_text, backend="postgresql")
    assert "DATABASE_URL=" not in live_text
    backup = settings.env_file.with_name(
        f"{settings.env_file.name}.pre-mysql-rollback.{manifest['run_id']}.bak"
    )
    migration._assert_database_selector(
        backup.read_text(encoding="utf-8"), backend="mysql"
    )
    assert ("cutover", migration.CUTOVER_PREPARED_CHECKPOINT) in remote
    assert ("cutover", migration.CUTOVER_FINALIZED_CHECKPOINT) not in remote

    result = migration.cutover_environment(
        settings,
        manifest,
        confirmation="cutover-token",
    )

    assert result["state"] == "finalized"
    assert manifest["phase_state"]["cutover"]["status"] == "complete"
    assert ("cutover", migration.CUTOVER_FINALIZED_CHECKPOINT) in remote


def test_cutover_and_rollback_renderers_bind_the_authoritative_selector() -> None:
    original = """DATABASE_BACKEND=mysql
DATABASE_URL=mysql://override
POSTGRESQL_USER=postgres
POSTGRESQL_PASSWORD=admin
POSTGRESQL_RUNTIME_PASSWORD=runtime
ANNUAL_MYSQL_HOST=mysql
ANNUAL_MYSQL_PORT=3306
ANNUAL_MYSQL_DATABASE=ontology_business
ANNUAL_MYSQL_USER=ontology_app
"""
    cutover = migration._render_cutover_env(
        original,
        database="ontology_platform",
        runtime_user="ontology_app",
        runtime_password="runtime",
        admin_user="postgres",
        admin_password="admin",
    )
    rollback = migration._render_mysql_rollback_env(cutover)
    migration._assert_database_selector(cutover, backend="postgresql")
    migration._assert_database_selector(rollback, backend="mysql")
    assert "DATABASE_URL=" not in cutover
    assert "DATABASE_URL=" not in rollback
    assert cutover.count("DATABASE_BACKEND=") == 1
    assert "POSTGRESQL_ADMIN_USER=postgres" in cutover
    assert "POSTGRESQL_ADMIN_PASSWORD=admin" in cutover


@pytest.mark.parametrize(
    ("mutator", "match"),
    (
        (lambda role: role.__setitem__("current_user", "postgres"), "mismatch"),
        (lambda role: role.__setitem__("rolsuper", True), "unsafe flags"),
    ),
)
def test_runtime_verifier_rejects_wrong_identity_or_dangerous_flags(
    mutator, match: str
) -> None:
    role = {
        "current_user": "ontology_app",
        "rolsuper": False,
        "rolcreaterole": False,
        "rolcreatedb": False,
        "rolcanlogin": True,
        "rolinherit": False,
        "rolbypassrls": False,
        "rolreplication": False,
    }
    mutator(role)
    with pytest.raises(RuntimeError, match=match):
        runtime_verify._validate_runtime_role_snapshot(
            role,
            expected_role="ontology_app",
            memberships=(),
            can_create_public=False,
        )


def test_runtime_verifier_rejects_membership_and_public_schema_create() -> None:
    role = {
        "current_user": "ontology_app",
        "rolsuper": False,
        "rolcreaterole": False,
        "rolcreatedb": False,
        "rolcanlogin": True,
        "rolinherit": False,
        "rolbypassrls": False,
        "rolreplication": False,
    }
    with pytest.raises(RuntimeError, match="memberships"):
        runtime_verify._validate_runtime_role_snapshot(
            role,
            expected_role="ontology_app",
            memberships=("pg_read_all_data",),
            can_create_public=False,
        )
    with pytest.raises(RuntimeError, match="CREATE"):
        runtime_verify._validate_runtime_role_snapshot(
            role,
            expected_role="ontology_app",
            memberships=(),
            can_create_public=True,
        )


def test_v2_checkpoint_payloads_do_not_hash_observation_timestamps() -> None:
    source = Path(migration.__file__).read_text(encoding="utf-8")
    verify_source = source[source.index("def verify_migration") : source.index("def _render_cutover_env")]
    cutover_source = source[source.index("def cutover_environment") : source.index("def _assert_settings_match_manifest")]
    assert '"verified_at"' not in verify_source
    assert '"cutover_at"' not in cutover_source
    assert migration.VERIFY_DEEP_CHECKPOINT.endswith("-v2")
    assert migration.CUTOVER_FINALIZED_CHECKPOINT.endswith("-v2")
