"""PostgreSQL engine and session management."""
from __future__ import annotations

from collections.abc import Generator

from fastapi import Request
from sqlalchemy import DateTime as SQLAlchemyDateTime, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import ensure_runtime_directories, get_settings


class Base(DeclarativeBase):
    pass


def orm_datetime(*, timezone: bool = True):
    """Return the PostgreSQL timestamp type used by all ORM models."""
    return SQLAlchemyDateTime(timezone=timezone)


POSTGRESQL_SCHEMA_REVISION = "20260828_06"

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
