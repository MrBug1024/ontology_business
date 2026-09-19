"""Opt-in PostgreSQL acceptance fixtures that never use the configured business DB.

Only a freshly generated database is migrated and removed. Credentials are read
through Settings and kept in URL objects; no connection string is printed.
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import URL, Engine, create_engine, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    BusinessScenario,
    Organization,
    OrganizationMember,
    OrganizationRole,
    Tenant,
    User,
)


_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_DATABASE_RE = re.compile(r"^ontology_acceptance_[0-9a-f]{16}$")
_ALEMBIC_ENV_KEYS = ("ALEMBIC_DATABASE_URL", "ALEMBIC_USE_ADMIN", "ALEMBIC_ROLE")


@dataclass
class IsolatedPostgreSQL:
    admin_engine: Engine
    runtime_engine: Engine
    head: str
    _admin_url: URL = field(repr=False)

    def migrate(self, revision: str, *, downgrade: bool = False) -> None:
        if not _DATABASE_RE.fullmatch(self._admin_url.database or ""):
            raise RuntimeError("Refusing migration outside the isolated acceptance database")
        previous = {key: os.environ.get(key) for key in _ALEMBIC_ENV_KEYS}
        try:
            os.environ["ALEMBIC_DATABASE_URL"] = self._admin_url.render_as_string(hide_password=False)
            os.environ.pop("ALEMBIC_USE_ADMIN", None)
            os.environ.pop("ALEMBIC_ROLE", None)
            config = Config(str(_BACKEND_ROOT / "alembic.ini"))
            (command.downgrade if downgrade else command.upgrade)(config, revision)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


@contextmanager
def isolated_database() -> Iterator[IsolatedPostgreSQL]:
    """Create, migrate, and reliably remove one uniquely named test database."""
    if os.environ.get("RUN_POSTGRESQL_INTEGRATION_TESTS") != "1":
        pytest.skip("Set RUN_POSTGRESQL_INTEGRATION_TESTS=1 for isolated PostgreSQL acceptance")
    settings = get_settings()
    database_name = "ontology_acceptance_" + uuid4().hex[:16]
    if not _DATABASE_RE.fullmatch(database_name) or database_name == settings.postgresql_database:
        raise RuntimeError("Invalid isolated acceptance database name")
    admin_url = URL.create(
        "postgresql+psycopg",
        username=settings.postgresql_admin_user.strip() or "postgres",
        password=settings.postgresql_admin_password or settings.postgresql_password,
        host=settings.postgresql_host,
        port=settings.postgresql_port,
        database=database_name,
    )
    runtime_url = admin_url.set(
        username=settings.postgresql_user,
        password=settings.postgresql_password,
    )
    engine_options = {
        "pool_pre_ping": True,
        "connect_args": {"connect_timeout": 5, "options": "-c statement_timeout=15000 -c lock_timeout=10000"},
    }
    # Database removal may wait for a PostgreSQL checkpoint when multiple
    # acceptance modules run concurrently; request SQL keeps the tighter bound.
    control = create_engine(admin_url.set(database="postgres"), isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 5, "options": "-c statement_timeout=60000 -c lock_timeout=10000"})
    admin_engine = create_engine(admin_url, **engine_options)
    runtime_engine = create_engine(runtime_url, **engine_options)
    head = ScriptDirectory.from_config(Config(str(_BACKEND_ROOT / "alembic.ini"))).get_current_head()
    if not head:
        raise RuntimeError("Acceptance requires one Alembic head")
    isolated = IsolatedPostgreSQL(admin_engine, runtime_engine, head, admin_url)
    created = False
    try:
        with control.connect() as connection:
            if connection.execute(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": database_name}).first():
                raise RuntimeError("Acceptance database unexpectedly already exists")
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
            created = True
        # The historical baseline creates tables but assumes deployment has
        # provisioned application DML. Provision that baseline only, then let
        # every later migration narrow grants and govern its newly added tables.
        # In particular the 35/36 grants are never supplied by this fixture.
        base = ScriptDirectory.from_config(Config(str(_BACKEND_ROOT / "alembic.ini"))).get_base()
        isolated.migrate(base)
        with admin_engine.begin() as connection:
            quoted_role = connection.dialect.identifier_preparer.quote(settings.postgresql_user)
            connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {quoted_role}")
            connection.exec_driver_sql(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {quoted_role}")
        isolated.migrate(head)
        yield isolated
    finally:
        runtime_engine.dispose()
        admin_engine.dispose()
        try:
            if created:
                with control.connect() as connection:
                    connection.execute(
                        text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :name AND pid <> pg_backend_pid()"),
                        {"name": database_name},
                    )
                    connection.exec_driver_sql(f'DROP DATABASE "{database_name}"')
        finally:
            control.dispose()


@pytest.fixture(scope="module")
def isolated_postgresql() -> Iterator[IsolatedPostgreSQL]:
    with isolated_database() as isolated:
        yield isolated


def seed_workspace(engine: Engine) -> dict[str, str]:
    """Persist synthetic active ownership and two scenarios in the isolated DB."""
    ids = {key: uuid4().hex for key in (
        "tenant_id", "user_id", "scenario_id", "other_scenario_id",
        "organization_id", "role_id", "member_id",
    )}
    with Session(engine) as db:
        db.add(Tenant(id=ids["tenant_id"], name="Acceptance workspace"))
        db.flush()
        db.add(User(
            id=ids["user_id"], tenant_id=ids["tenant_id"],
            email=f"{ids['user_id']}@acceptance.invalid", password_hash="unusable-test-password",
            status="active", email_verified_at=datetime.now(timezone.utc),
        ))
        db.add(Organization(id=ids["organization_id"], tenant_id=ids["tenant_id"], name="Acceptance organization"))
        db.flush()
        db.add(OrganizationRole(id=ids["role_id"], organization_id=ids["organization_id"], key="owner", name="Owner"))
        db.flush()
        db.add(OrganizationMember(
            id=ids["member_id"], organization_id=ids["organization_id"],
            user_id=ids["user_id"], role_id=ids["role_id"], status="active",
        ))
        db.add_all(BusinessScenario(
            id=ids[key], tenant_id=ids["tenant_id"], name=name,
        ) for key, name in (("scenario_id", "Acceptance A"), ("other_scenario_id", "Acceptance B")))
        db.commit()
    return ids


def tenant_session(engine: Engine, workspace: dict[str, str]) -> Session:
    return Session(engine, info={"tenant_id": workspace["tenant_id"], "user_id": workspace["user_id"]})
