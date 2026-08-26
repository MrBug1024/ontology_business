from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, text

from app import database


def test_init_db_creates_nested_sqlite_database_and_preserves_existing_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime" / "nested" / "platform.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    isolated_engine = create_engine(database_url, pool_pre_ping=True)
    settings = SimpleNamespace(database_url=database_url, runtime_environment="dev")

    try:
        with (
            patch.object(database, "engine", isolated_engine),
            patch.object(database, "_settings", settings),
        ):
            database.init_db()
            with isolated_engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO tenants (id, name, created_at) "
                        "VALUES ('tenant-bootstrap', '已有租户', CURRENT_TIMESTAMP)"
                    )
                )

            # A second startup is the deployment scenario: it must be
            # idempotent and must not reset existing application data.
            database.init_db()

        assert database_path.is_file()
        assert database_path.parent.is_dir()
        with isolated_engine.connect() as connection:
            assert connection.execute(
                text("SELECT name FROM tenants WHERE id = 'tenant-bootstrap'")
            ).scalar_one() == "已有租户"
            assert connection.execute(text("SELECT COUNT(*) FROM tenants")).scalar_one() == 1
    finally:
        isolated_engine.dispose()
