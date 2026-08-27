# Platform database migrations

PostgreSQL schema changes are applied by Alembic before an application process
starts. The API runtime role only verifies `alembic_version` and never executes
DDL. Use `scripts/migrate_mysql_to_postgresql.py` for the initial bootstrap and
data migration; use `alembic upgrade head` for later versioned upgrades.
