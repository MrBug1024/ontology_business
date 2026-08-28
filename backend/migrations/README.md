# Platform database migrations

PostgreSQL schema changes are applied by Alembic before an application process
starts. The API runtime role only verifies `alembic_version` and never executes
DDL. Run `alembic upgrade head` with the migration owner before starting the
application, then use the runtime verification script to confirm the deployed
revision and permissions.
