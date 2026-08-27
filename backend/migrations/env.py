from __future__ import annotations

from logging.config import fileConfig
import os
import re

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import URL

from app import external_api_models, models  # noqa: F401
from app.config import get_settings
from app.database import Base


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
x_arguments = context.get_x_argument(as_dictionary=True)
configured_url = os.environ.get("ALEMBIC_DATABASE_URL", "").strip()
use_admin = (
    x_arguments.get("use_admin", "").strip() == "1"
    or os.environ.get("ALEMBIC_USE_ADMIN", "").strip() == "1"
)
if not configured_url and use_admin:
    configured_url = URL.create(
        "postgresql+psycopg",
        username=settings.postgresql_admin_user.strip() or "postgres",
        password=(
            settings.postgresql_admin_password
            or settings.postgresql_password
        ),
        host=settings.postgresql_host,
        port=settings.postgresql_port,
        database=settings.postgresql_database,
    ).render_as_string(hide_password=False)
if not configured_url:
    configured_url = settings.database_url
# ConfigParser treats percent signs in escaped credentials as interpolation.
config.set_main_option("sqlalchemy.url", configured_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={
            "application_name": "ontology-platform-migrator",
            "options": "-c timezone=UTC -c lock_timeout=30000",
        },
    )
    with connectable.connect() as connection:
        migration_role = (
            x_arguments.get("role", "").strip()
            or os.environ.get("ALEMBIC_ROLE", "").strip()
        )
        if migration_role:
            if re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", migration_role) is None:
                raise RuntimeError("ALEMBIC_ROLE is not a valid PostgreSQL role name")
            connection.exec_driver_sql(f'SET ROLE "{migration_role}"')
            # SET ROLE starts SQLAlchemy's implicit transaction. Commit that
            # session-state change before Alembic opens the migration-owned
            # transaction, otherwise connection close rolls the whole upgrade
            # back while still printing a misleading "Running upgrade" line.
            connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
