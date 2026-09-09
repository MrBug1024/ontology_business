"""Self-created PostgreSQL database; never writes fixtures to the configured database."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, create_engine, text
from sqlalchemy.engine import make_url


@contextmanager
def isolated_access_database(revision: str = "head"):
    from app.config import get_settings

    settings = get_settings()
    database_name = "ontology_access_verify_" + uuid4().hex[:12]
    if re.fullmatch(r"ontology_access_verify_[0-9a-f]{12}", database_name) is None:
        raise RuntimeError("Invalid isolated database name")
    admin_url = URL.create("postgresql+psycopg", username=settings.postgresql_admin_user.strip() or "postgres",
        password=settings.postgresql_admin_password or settings.postgresql_password,
        host=settings.postgresql_host, port=settings.postgresql_port, database="postgres")
    runtime_url = make_url(settings.database_url).set(database=database_name)
    control = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    target_admin = create_engine(admin_url.set(database=database_name))
    created = False
    old_url = os.environ.get("ALEMBIC_DATABASE_URL")
    try:
        with control.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
            created = True
        # Match deployment provisioning for the initial tables. Subsequent
        # migrations narrow their own table privileges (notably audit/guard).
        with target_admin.begin() as connection:
            role = connection.dialect.identifier_preparer.quote(settings.postgresql_user.strip())
            connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
            connection.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}")
        os.environ["ALEMBIC_DATABASE_URL"] = admin_url.set(database=database_name).render_as_string(hide_password=False)
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(config, revision)
        yield runtime_url, target_admin
    finally:
        if old_url is None:
            os.environ.pop("ALEMBIC_DATABASE_URL", None)
        else:
            os.environ["ALEMBIC_DATABASE_URL"] = old_url
        target_admin.dispose()
        if created:
            with control.connect() as connection:
                connection.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:name AND pid<>pg_backend_pid()"), {"name": database_name})
                connection.exec_driver_sql(f'DROP DATABASE "{database_name}"')
        control.dispose()
