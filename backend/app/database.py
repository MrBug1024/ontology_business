"""PostgreSQL engine and session management."""
from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import DateTime as SQLAlchemyDateTime, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import ensure_runtime_directories, get_settings


class Base(DeclarativeBase):
    pass


def orm_datetime(*, timezone: bool = True):
    """Return the PostgreSQL timestamp type used by all ORM models."""
    return SQLAlchemyDateTime(timezone=timezone)


POSTGRESQL_SCHEMA_REVISION = "20260904_14"
# One PostgreSQL session-level advisory lock is held by the process elected to
# run durable background work.  The connection closing on a crash releases the
# lock automatically, so a replacement worker can take over without a stale
# lease or a shared in-memory leader flag.
BACKGROUND_WORKER_ADVISORY_LOCK_KEY = 6_418_602_014_221


class BackgroundWorkerLeaseLostError(RuntimeError):
    """The connection that held the advisory worker lease is no longer live."""


_settings = get_settings()
if not _settings.uses_postgresql_database:
    raise RuntimeError("平台数据库仅支持 PostgreSQL")

engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    connect_args={
        "application_name": "ontology-platform-api",
        "options": (
            "-c timezone=UTC "
            f"-c statement_timeout={_settings.database_statement_timeout_ms} "
            f"-c lock_timeout={_settings.database_lock_timeout_ms}"
        ),
    },
    pool_size=_settings.database_pool_size,
    max_overflow=_settings.database_max_overflow,
    pool_timeout=_settings.database_pool_timeout_seconds,
    pool_recycle=1800,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@dataclass
class BackgroundWorkerLease:
    """Own the database connection backing the background-worker leader lock."""

    connection: object | None = None
    backend_pid: int | None = None

    @property
    def acquired(self) -> bool:
        return self.connection is not None and self.backend_pid is not None

    def assert_held(self) -> None:
        """Probe the original PostgreSQL session before running durable work.

        A SQLAlchemy connection may reconnect after a database failover.  That
        replacement session does not own the original advisory lock, so merely
        executing ``SELECT 1`` would allow the old worker to continue.  The
        recorded backend PID makes a reconnect an explicit lease-loss event.
        """
        connection = self.connection
        expected_pid = self.backend_pid
        if connection is None or expected_pid is None:
            raise BackgroundWorkerLeaseLostError("后台 worker PostgreSQL 租约不可用")
        try:
            current_pid = int(connection.scalar(text("SELECT pg_backend_pid()")))
        except Exception as exc:  # noqa: BLE001 - connection may have failed over.
            self._discard_lost_connection(connection)
            raise BackgroundWorkerLeaseLostError(
                "后台 worker PostgreSQL 租约连接已失效"
            ) from exc
        if current_pid != expected_pid:
            self._discard_lost_connection(connection)
            raise BackgroundWorkerLeaseLostError(
                "后台 worker PostgreSQL 租约已在数据库重连后丢失"
            )

    def _discard_lost_connection(self, connection: object) -> None:
        self.connection = None
        self.backend_pid = None
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - the connection is already unhealthy.
            pass

    def release(self) -> None:
        connection = self.connection
        self.connection = None
        self.backend_pid = None
        if connection is None:
            return
        try:
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": BACKGROUND_WORKER_ADVISORY_LOCK_KEY},
            )
        except Exception:
            # A failed-over session has already released its advisory lock.
            pass
        finally:
            connection.close()


def acquire_background_worker_lease() -> BackgroundWorkerLease:
    """Acquire the cross-process worker lease without waiting for another API.

    A failed attempt closes its connection immediately.  Keeping the successful
    connection open is intentional: PostgreSQL advisory locks are scoped to the
    database session, not a transaction.
    """
    connection = engine.connect()
    try:
        acquired = bool(
            connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": BACKGROUND_WORKER_ADVISORY_LOCK_KEY},
            )
        )
        backend_pid = (
            int(connection.scalar(text("SELECT pg_backend_pid()")))
            if acquired
            else None
        )
    except Exception:
        connection.close()
        raise
    if not acquired:
        connection.close()
        return BackgroundWorkerLease()
    return BackgroundWorkerLease(connection=connection, backend_pid=backend_pid)


def get_db(request: Request) -> Generator[Session, None, None]:
    db = SessionLocal()
    tenant_id = getattr(request.state, "tenant_id", None)
    user_id = getattr(request.state, "user_id", None)
    if tenant_id:
        db.info["tenant_id"] = tenant_id
    if user_id:
        db.info["user_id"] = user_id
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Verify the configured PostgreSQL service and migrated schema."""
    from . import external_api_models, models  # noqa: F401

    ensure_runtime_directories(_settings)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - do not expose connection details.
        raise RuntimeError(
            "平台 PostgreSQL 连接失败，请检查数据库服务和账号权限"
        ) from exc

    _verify_postgresql_schema_revision()
    _verify_schema()


def _verify_postgresql_schema_revision() -> None:
    with engine.connect() as connection:
        inspector = inspect(connection)
        if not inspector.has_table("alembic_version"):
            raise RuntimeError(
                "PostgreSQL 平台库尚未执行版本化迁移；请先运行 Alembic upgrade head"
            )
        revisions = {
            str(value)
            for value in connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars()
        }
        if revisions != {POSTGRESQL_SCHEMA_REVISION}:
            installed = ", ".join(sorted(revisions)) or "<empty>"
            raise RuntimeError(
                "PostgreSQL schema 版本不匹配："
                f"当前 {installed}，应用需要 {POSTGRESQL_SCHEMA_REVISION}"
            )


def _verify_schema() -> None:
    """Fail startup if the installed PostgreSQL schema is incomplete."""
    with engine.connect() as connection:
        inspector = inspect(connection)
        missing_tables: list[str] = []
        missing_columns: dict[str, list[str]] = {}
        for table_name, model_table in Base.metadata.tables.items():
            physical_name = model_table.name
            if not inspector.has_table(physical_name, schema=model_table.schema):
                missing_tables.append(table_name)
                continue
            installed_columns = {
                column["name"]
                for column in inspector.get_columns(
                    physical_name,
                    schema=model_table.schema,
                )
            }
            missing = sorted(
                column.name
                for column in model_table.columns
                if column.name not in installed_columns
            )
            if missing:
                missing_columns[table_name] = missing

        if missing_tables or missing_columns:
            details = []
            if missing_tables:
                details.append(f"缺少表: {', '.join(sorted(missing_tables))}")
            details.extend(
                f"{table}: {', '.join(columns)}"
                for table, columns in sorted(missing_columns.items())
            )
            raise RuntimeError(
                "平台 PostgreSQL 结构不完整，启动已停止，请检查迁移执行结果："
                + "; ".join(details)
            )
