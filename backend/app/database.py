"""SQLAlchemy engine / session management."""
from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
import uuid

from fastapi import Request
from sqlalchemy import (
    DateTime as SQLAlchemyDateTime,
    Index,
    MetaData,
    Table,
    column,
    create_engine,
    event,
    exists,
    inspect,
    literal,
    select,
    table,
    text,
    update,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import ensure_runtime_directories, get_settings


class Base(DeclarativeBase):
    pass


MYSQL_DATETIME_PRECISION = 6


def orm_datetime(*, timezone: bool = True):
    """Use microsecond-preserving DATETIME on MySQL and native DateTime elsewhere."""
    return SQLAlchemyDateTime(timezone=timezone).with_variant(
        mysql.DATETIME(fsp=MYSQL_DATETIME_PRECISION),
        "mysql",
    )


logger = logging.getLogger(__name__)
POSTGRESQL_SCHEMA_REVISION = "20260828_06"

_settings = get_settings()
engine_options: dict[str, object] = {"pool_pre_ping": True}
if _settings.uses_sqlite_database:
    engine_options["connect_args"] = {"check_same_thread": False}
elif _settings.uses_postgresql_database:
    engine_options.update(
        {
            "connect_args": {
                "application_name": "ontology-platform-api",
                "options": (
                    "-c timezone=UTC "
                    f"-c statement_timeout={_settings.database_statement_timeout_ms} "
                    f"-c lock_timeout={_settings.database_lock_timeout_ms}"
                ),
            },
            "pool_size": _settings.database_pool_size,
            "max_overflow": _settings.database_max_overflow,
            "pool_timeout": _settings.database_pool_timeout_seconds,
            "pool_recycle": 1800,
        }
    )
engine = create_engine(_settings.database_url, **engine_options)


def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _set_mysql_session_defaults(dbapi_connection, _connection_record) -> None:
    """Keep platform tables transactional even on MyISAM-default servers."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SET SESSION default_storage_engine=InnoDB")
    finally:
        cursor.close()


if engine.dialect.name == "sqlite":
    event.listen(engine, "connect", _enable_sqlite_foreign_keys)
elif engine.dialect.name == "mysql":
    event.listen(engine, "connect", _set_mysql_session_defaults)


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


def _migrate_data_sources_nullable_scenario() -> None:
    """SQLite 不支持 ALTER COLUMN：若 data_sources.scenario_id 仍为 NOT NULL，则重建表使其可空。"""
    if not _settings.database_url.startswith("sqlite"):
        return
    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            'SELECT "notnull" FROM pragma_table_info(\'data_sources\') WHERE name = \'scenario_id\''
        ).fetchone()
        if not row or not row[0]:
            return  # 已是可空（或表不存在），无需迁移

        # Reflect the exact installed schema.  Older deployments may already
        # contain tenancy, public-access, connector-revision, or future columns;
        # rebuilding from a hard-coded subset would irreversibly erase them.
        source_metadata = MetaData()
        source_table = Table("data_sources", source_metadata, autoload_with=conn)
        explicit_index_sql = [
            str(sql)
            for (sql,) in conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'data_sources' AND sql IS NOT NULL"
            ).fetchall()
        ]
        conn.commit()  # PRAGMA foreign_keys can only change outside a transaction.

        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
            conn.commit()
            raise RuntimeError("无法在 data_sources 重建前暂停 SQLite 外键检查")
        conn.commit()
        temporary_name = f"data_sources_migration_{uuid.uuid4().hex[:12]}"
        try:
            with conn.begin():
                target_metadata = MetaData()
                # Resolve reflected FK targets before cloning only the source
                # table under its temporary name.
                for table in source_metadata.tables.values():
                    if table is not source_table:
                        table.to_metadata(target_metadata)
                target_table = source_table.to_metadata(
                    target_metadata,
                    name=temporary_name,
                )
                target_table.c.scenario_id.nullable = True
                # Explicit indexes cannot coexist under their old global names
                # while the source table is still present.  Recreate their exact
                # SQLite DDL after the rename; UNIQUE table constraints remain.
                target_table.indexes.clear()
                target_table.create(conn)

                quote = conn.dialect.identifier_preparer.quote
                columns = ", ".join(quote(column.name) for column in source_table.columns)
                conn.exec_driver_sql(
                    f"INSERT INTO {quote(temporary_name)} ({columns}) "
                    f"SELECT {columns} FROM {quote('data_sources')}"
                )
                conn.exec_driver_sql(f"DROP TABLE {quote('data_sources')}")
                conn.exec_driver_sql(
                    f"ALTER TABLE {quote(temporary_name)} RENAME TO {quote('data_sources')}"
                )
                for ddl in explicit_index_sql:
                    conn.exec_driver_sql(ddl)
        finally:
            if conn.in_transaction():
                conn.rollback()
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            enabled = conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
            conn.commit()
            if enabled != 1:
                conn.invalidate()
                raise RuntimeError("data_sources 迁移后未能恢复 SQLite 外键检查")

        violations = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        conn.commit()
        if violations:
            raise RuntimeError("data_sources 迁移后检测到外键完整性错误")


def _migrate_workflows_dag() -> None:
    """为既有工作流补 nodes/edges 列。"""
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("ontology_workflows"):
            return
        cols = {column["name"] for column in inspector.get_columns("ontology_workflows")}
        if "nodes" not in cols:
            conn.exec_driver_sql("ALTER TABLE ontology_workflows ADD COLUMN nodes JSON")
        if "edges" not in cols:
            conn.exec_driver_sql("ALTER TABLE ontology_workflows ADD COLUMN edges JSON")
        # MySQL versions before 8.0.13 reject defaults on JSON columns.  ORM
        # writes supply application defaults; legacy rows are repaired here.
        conn.exec_driver_sql(
            "UPDATE ontology_workflows SET nodes = '[]' WHERE nodes IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE ontology_workflows SET edges = '[]' WHERE edges IS NULL"
        )


def _migrate_data_mapping_status() -> None:
    """为已有数据映射补充检查、刷新和错误状态字段。"""
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("data_mappings"):
            return
        existing = {column["name"] for column in inspector.get_columns("data_mappings")}
        if not existing:
            return
        columns = {
            "data_source_binding_key": "VARCHAR(180) DEFAULT ''",
            "data_source_binding_ref": "JSON",
            "status": "VARCHAR(20) DEFAULT 'unknown'",
            "last_error": "TEXT",
            "last_checked_at": "TIMESTAMP",
            "last_refreshed_at": "TIMESTAMP",
            "last_row_count": "INTEGER DEFAULT 0",
            "last_imported_count": "INTEGER DEFAULT 0",
            "environment_status": "JSON",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE data_mappings ADD COLUMN {name} {definition}")
        conn.exec_driver_sql(
            "UPDATE data_mappings SET data_source_binding_ref = '{}' "
            "WHERE data_source_binding_ref IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE data_mappings SET environment_status = '{}' "
            "WHERE environment_status IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE data_mappings SET last_error = '' WHERE last_error IS NULL"
        )


def _migrate_data_mapping_runtime_bindings() -> None:
    """为既有映射补充跨环境运行时绑定字段。

    ``create_all`` 不会为既有表添加列。这里不依赖 SQLite 的 ``PRAGMA``，使
    已部署的 PostgreSQL/MySQL 数据库也能安全升级；列允许暂时为 NULL，以便
    老映射继续使用开发环境的直接数据源兼容路径。
    """
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("data_mappings"):
            return
        existing = {column["name"] for column in inspector.get_columns("data_mappings")}
        columns = {
            "data_source_binding_key": "VARCHAR(180)",
            "data_source_binding_ref": "JSON",
            "environment_status": "JSON",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE data_mappings ADD COLUMN {name} {definition}")


def _migrate_mapping_refresh_provenance() -> None:
    """Fail closed when pre-freeze mapping refresh jobs are encountered.

    Historic jobs only carried a mutable mapping id/fingerprint.  They cannot
    prove which mapping definition or environment release authorised their
    external read, so active records are cancelled during upgrade and must be
    re-enqueued.  Terminal history remains readable.
    """
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("data_mapping_refresh_jobs"):
            return
        existing = {
            column["name"]
            for column in inspector.get_columns("data_mapping_refresh_jobs")
        }
        is_legacy = "mapping_snapshot" not in existing
        lacks_relation_fingerprint = "relation_mapping_fingerprint" not in existing
        columns = {
            "mapping_snapshot": "JSON",
            "definition_snapshot_id": "VARCHAR(32)",
            "release_id": "VARCHAR(32)",
            "definition_hash": "VARCHAR(64)",
            "definition_source": "VARCHAR(20)",
            "relation_mapping_fingerprint": "VARCHAR(64)",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.exec_driver_sql(
                    f"ALTER TABLE data_mapping_refresh_jobs ADD COLUMN {name} {definition}"
                )
        conn.exec_driver_sql(
            "UPDATE data_mapping_refresh_jobs SET mapping_snapshot = '{}' "
            "WHERE mapping_snapshot IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE data_mapping_refresh_jobs SET definition_hash = '' "
            "WHERE definition_hash IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE data_mapping_refresh_jobs SET definition_source = 'live' "
            "WHERE definition_source IS NULL OR TRIM(definition_source) = ''"
        )
        conn.exec_driver_sql(
            "UPDATE data_mapping_refresh_jobs SET relation_mapping_fingerprint = '' "
            "WHERE relation_mapping_fingerprint IS NULL"
        )
        if is_legacy:
            terminal_statuses = "'succeeded', 'failed', 'timed_out', 'cancelled'"
            conn.exec_driver_sql(
                "UPDATE data_mapping_refresh_jobs "
                "SET status = 'cancelled', "
                "error = '映射刷新定义快照缺失，部署升级后已安全取消，请重新提交', "
                "active_key = NULL, next_retry_at = NULL, "
                "completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP) "
                f"WHERE status NOT IN ({terminal_statuses})"
            )
            # A historic terminal row is safe to display but it does not have
            # a reproducible mapping body.  Do not label it as a new live job
            # and accidentally imply durable provenance to operators.
            conn.exec_driver_sql(
                "UPDATE data_mapping_refresh_jobs SET definition_source = 'legacy'"
            )
        elif lacks_relation_fingerprint:
            terminal_statuses = "'succeeded', 'failed', 'timed_out', 'cancelled'"
            conn.exec_driver_sql(
                "UPDATE data_mapping_refresh_jobs "
                "SET status = 'cancelled', "
                "error = '关系映射定义指纹缺失，部署升级后已安全取消，请重新提交', "
                "active_key = NULL, next_retry_at = NULL, "
                "completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP) "
                f"WHERE status NOT IN ({terminal_statuses})"
            )
        # Intermediate deployments could already have the relation fingerprint
        # while still leaving dev's full-definition hash empty. Such jobs cannot
        # prove which endpoint property contract they were queued against.
        terminal_statuses = "'succeeded', 'failed', 'timed_out', 'cancelled'"
        conn.exec_driver_sql(
            "UPDATE data_mapping_refresh_jobs "
            "SET status = 'cancelled', "
            "error = '开发环境定义指纹缺失，部署升级后已安全取消，请重新提交', "
            "active_key = NULL, next_retry_at = NULL, "
            "completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP) "
            f"WHERE status NOT IN ({terminal_statuses}) "
            "AND definition_source = 'live' "
            "AND (definition_hash IS NULL OR TRIM(definition_hash) = '')"
        )
        refreshed = inspect(conn)
        existing_indexes = {
            index["name"]
            for index in refreshed.get_indexes("data_mapping_refresh_jobs")
        }
        for name, columns in {
            "ix_mapping_refresh_jobs_release": "release_id, definition_snapshot_id",
            "ix_mapping_refresh_jobs_definition_snapshot_id": "definition_snapshot_id",
        }.items():
            if name not in existing_indexes:
                conn.exec_driver_sql(
                    f"CREATE INDEX {name} ON data_mapping_refresh_jobs ({columns})"
                )


def _migrate_action_safety() -> None:
    """为已有 Action 和执行日志补充确认、幂等和执行模式字段。"""
    with engine.begin() as conn:
        inspector = inspect(conn)
        action_columns = (
            {column["name"] for column in inspector.get_columns("ontology_actions")}
            if inspector.has_table("ontology_actions")
            else set()
        )
        if action_columns:
            for name, definition in {
                "requires_confirmation": "BOOLEAN DEFAULT 1",
                "idempotency_required": "BOOLEAN DEFAULT 1",
                "permission_scope": "VARCHAR(30) DEFAULT 'scenario'",
            }.items():
                if name not in action_columns:
                    conn.exec_driver_sql(
                        f"ALTER TABLE ontology_actions ADD COLUMN {name} {definition}"
                    )

        log_columns = (
            {column["name"] for column in inspector.get_columns("action_execution_logs")}
            if inspector.has_table("action_execution_logs")
            else set()
        )
        if log_columns:
            for name, definition in {
                "mode": "VARCHAR(20) DEFAULT 'execute'",
                "idempotency_key": "VARCHAR(120)",
                "connector_audit": "JSON",
            }.items():
                if name not in log_columns:
                    conn.exec_driver_sql(
                        f"ALTER TABLE action_execution_logs ADD COLUMN {name} {definition}"
                    )
            _widen_mysql_varchar_columns(
                conn,
                "action_execution_logs",
                {"status": (32, "NOT NULL")},
            )
            conn.exec_driver_sql(
                "UPDATE action_execution_logs SET connector_audit = '[]' "
                "WHERE connector_audit IS NULL"
            )
            existing_indexes = {
                index["name"] for index in inspect(conn).get_indexes("action_execution_logs")
            }
            if "ix_action_execution_logs_idempotency_key" not in existing_indexes:
                try:
                    conn.exec_driver_sql(
                        "CREATE INDEX ix_action_execution_logs_idempotency_key "
                        "ON action_execution_logs (idempotency_key)"
                    )
                except Exception:  # noqa: BLE001 - tolerate startup races.
                    pass
            unique_names = {
                constraint["name"]
                for constraint in inspect(conn).get_unique_constraints("action_execution_logs")
            } | existing_indexes
            if "uq_action_execution_logs_idempotency" not in unique_names:
                try:
                    conn.exec_driver_sql(
                        "CREATE UNIQUE INDEX uq_action_execution_logs_idempotency "
                        "ON action_execution_logs (scenario_id, target_type, target_id, idempotency_key)"
                    )
                except Exception:  # noqa: BLE001 - preserve legacy duplicate audit rows.
                    logger.warning(
                        "action_execution_logs 存在历史幂等键冲突，保留原记录并跳过唯一索引"
                    )


def _migrate_workflow_lifecycle() -> None:
    """为已有工作流补充草稿/启用/停用生命周期状态。"""
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("ontology_workflows"):
            return
        existing = {column["name"] for column in inspector.get_columns("ontology_workflows")}
        if not existing or "status" in existing:
            return
        # 旧版本只有 enabled 字段，迁移为可执行的 active，避免升级后已有流程突然无法运行。
        conn.exec_driver_sql(
            "ALTER TABLE ontology_workflows ADD COLUMN status VARCHAR(20) DEFAULT 'active'"
        )


def _migrate_tenancy() -> None:
    """为已有平台表补充租户列；旧数据在首个用户注册时认领。"""
    columns_by_table = {
        "business_scenarios": {
            "tenant_id": "VARCHAR(32)",
            "is_public": "BOOLEAN DEFAULT 0",
        },
        "data_sources": {
            "tenant_id": "VARCHAR(32)",
            "is_public": "BOOLEAN DEFAULT 0",
        },
        "llm_configs": {
            "tenant_id": "VARCHAR(32)",
            "is_public": "BOOLEAN DEFAULT 0",
        },
        "skills": {
            "tenant_id": "VARCHAR(32)",
            "is_public": "BOOLEAN DEFAULT 0",
        },
        "mcp_configs": {
            "tenant_id": "VARCHAR(32)",
            "is_public": "BOOLEAN DEFAULT 0",
        },
        "agents": {
            "tenant_id": "VARCHAR(32)",
        },
    }
    with engine.begin() as conn:
        inspector = inspect(conn)
        for table, columns in columns_by_table.items():
            if not inspector.has_table(table):
                continue
            existing = {column["name"] for column in inspector.get_columns(table)}
            for name, definition in columns.items():
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _migrate_assistant_scopes() -> None:
    """为已有助手会话补充上下文范围；旧消息可用最近一条消息的路径回填。"""
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("assistant_threads"):
            return
        existing = {column["name"] for column in inspector.get_columns("assistant_threads")}
        if "scope_key" not in existing:
            conn.exec_driver_sql("ALTER TABLE assistant_threads ADD COLUMN scope_key VARCHAR(700) DEFAULT 'global'")
        if conn.dialect.name == "sqlite":
            # 仅在 SQLite 上使用 json_extract 回填旧版本的 global 值；新会话会在
            # 创建时写入准确范围。其他数据库在这里保留安全的 global 默认值。
            conn.exec_driver_sql(
                """
                UPDATE assistant_threads
                SET scope_key = CASE
                    WHEN scenario_id IS NOT NULL THEN 'scenario:' || scenario_id || '|path:' || COALESCE(
                        (SELECT json_extract(m.context, '$.path')
                         FROM assistant_messages m
                         WHERE m.thread_id = assistant_threads.id
                         ORDER BY m.created_at DESC LIMIT 1), '/')
                    ELSE 'global|path:' || COALESCE(
                        (SELECT json_extract(m.context, '$.path')
                         FROM assistant_messages m
                         WHERE m.thread_id = assistant_threads.id
                         ORDER BY m.created_at DESC LIMIT 1), '/')
                END
                WHERE scope_key IS NULL OR scope_key = 'global'
                """
            )
        else:
            conn.exec_driver_sql(
                "UPDATE assistant_threads SET scope_key = 'global' "
                "WHERE scope_key IS NULL OR scope_key = ''"
            )
        if inspector.has_table("assistant_messages"):
            message_columns = {
                column["name"] for column in inspector.get_columns("assistant_messages")
            }
        else:
            message_columns = set()
        if message_columns and "thinking" not in message_columns:
            conn.exec_driver_sql("ALTER TABLE assistant_messages ADD COLUMN thinking JSON")
        if message_columns:
            conn.exec_driver_sql(
                "UPDATE assistant_messages SET thinking = '[]' WHERE thinking IS NULL"
            )


def _migrate_assistant_thread_ownership() -> None:
    """为既有全局助手会话补充创建者，避免同租户横向读取历史上下文。

    旧会话优先从已有审计记录回填创建用户；无法可靠归属的旧会话保留为 NULL，
    路由会将其视作不可访问而非错误地共享给同租户所有用户。
    """
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("assistant_threads"):
            return
        columns = {column["name"] for column in inspector.get_columns("assistant_threads")}
        if "created_by_user_id" not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE assistant_threads ADD COLUMN created_by_user_id VARCHAR(32)"
            )
        if inspector.has_table("assistant_audit_logs"):
            rows = conn.exec_driver_sql(
                "SELECT id FROM assistant_threads WHERE created_by_user_id IS NULL"
            ).fetchall()
            for (thread_id,) in rows:
                owner = conn.execute(
                    text(
                        "SELECT user_id FROM assistant_audit_logs "
                        "WHERE thread_id = :thread_id ORDER BY created_at ASC LIMIT 1"
                    ),
                    {"thread_id": thread_id},
                ).fetchone()
                if owner and owner[0]:
                    conn.execute(
                        text(
                            "UPDATE assistant_threads SET created_by_user_id = :user_id "
                            "WHERE id = :thread_id"
                        ),
                        {"user_id": owner[0], "thread_id": thread_id},
                    )

        if not inspector.has_table("assistant_attachments"):
            return
        attachment_columns = {
            column["name"] for column in inspector.get_columns("assistant_attachments")
        }
        if "created_by_user_id" not in attachment_columns:
            conn.exec_driver_sql(
                "ALTER TABLE assistant_attachments ADD COLUMN created_by_user_id VARCHAR(32)"
            )


def _migrate_conversation_ownership() -> None:
    """Add private Agent-conversation ownership without guessing legacy rows.

    A transcript predating this field has no trustworthy creator signal.  It
    deliberately remains NULL and therefore inaccessible through the API until
    a supported migration/explicit sharing model exists.
    """
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("conversations"):
            return
        columns = {column["name"] for column in inspector.get_columns("conversations")}
        if "created_by_user_id" not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE conversations ADD COLUMN created_by_user_id VARCHAR(32)"
            )
        indexes = {index["name"] for index in inspector.get_indexes("conversations")}
        if "ix_conversations_created_by_user_id" not in indexes:
            conn.exec_driver_sql(
                "CREATE INDEX ix_conversations_created_by_user_id "
                "ON conversations (created_by_user_id)"
            )


def _migrate_document_index() -> None:
    """为既有文件桶补充 P1 文档索引元数据。

    ``document_chunks`` 是新表，会由 ``create_all`` 创建；这里为旧版
    ``bucket_files`` 补列。项目支持 SQLite、PostgreSQL 与 MySQL，所以使用
    三者都支持的基础 ALTER TABLE，而不是只在本地 SQLite 上悄悄成功。
    """
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("bucket_files"):
            return
        existing = {column["name"] for column in inspector.get_columns("bucket_files")}
        columns = {
            "index_status": "VARCHAR(20)",
            "index_error": "TEXT",
            "index_version": "VARCHAR(80)",
            "indexed_content_hash": "VARCHAR(64)",
            "indexed_at": "TIMESTAMP",
            "chunk_count": "INTEGER",
            "content_sha256": "VARCHAR(64)",
            "origin_template_file_id": "VARCHAR(32)",
            "origin_template_sha256": "VARCHAR(64)",
            "origin_template_id": "VARCHAR(32)",
            "origin_template_version_id": "VARCHAR(32)",
            "generated_by_action_log_id": "VARCHAR(32)",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE bucket_files ADD COLUMN {name} {definition}")
        # 旧行可能是 NULL；回填为 API/索引状态机所需的安全默认值。
        conn.exec_driver_sql(
            "UPDATE bucket_files SET index_status = 'pending' "
            "WHERE index_status IS NULL OR index_status = ''"
        )
        conn.exec_driver_sql("UPDATE bucket_files SET index_error = '' WHERE index_error IS NULL")
        conn.exec_driver_sql("UPDATE bucket_files SET index_version = '' WHERE index_version IS NULL")
        conn.exec_driver_sql(
            "UPDATE bucket_files SET indexed_content_hash = '' WHERE indexed_content_hash IS NULL"
        )
        conn.exec_driver_sql("UPDATE bucket_files SET content_sha256 = '' WHERE content_sha256 IS NULL")
        conn.exec_driver_sql(
            "UPDATE bucket_files SET origin_template_sha256 = '' "
            "WHERE origin_template_sha256 IS NULL"
        )
        conn.exec_driver_sql("UPDATE bucket_files SET chunk_count = 0 WHERE chunk_count IS NULL")
        indexes = {index["name"] for index in inspect(conn).get_indexes("bucket_files")}
        if "uq_bucket_files_generated_action_log" not in indexes:
            # Multiple API workers may run startup migrations concurrently.
            # A duplicate-index error means another worker completed this
            # exact idempotent step first and must not abort application boot.
            try:
                conn.exec_driver_sql(
                    "CREATE UNIQUE INDEX uq_bucket_files_generated_action_log "
                    "ON bucket_files (generated_by_action_log_id)"
                )
            except Exception:  # noqa: BLE001
                pass


def _widen_mysql_varchar_columns(
    conn,
    table_name: str,
    definitions: dict[str, tuple[int, str]],
) -> None:
    """Idempotently expand managed VARCHAR columns without narrowing data."""
    if conn.dialect.name != "mysql":
        return
    installed = {
        column_definition["name"]: column_definition
        for column_definition in inspect(conn).get_columns(table_name)
    }
    quote = conn.dialect.identifier_preparer.quote
    for column_name, (target_length, suffix) in definitions.items():
        column_definition = installed.get(column_name)
        if column_definition is None:
            continue
        current_length = getattr(column_definition["type"], "length", None)
        if current_length is not None and int(current_length) >= target_length:
            continue
        conn.exec_driver_sql(
            f"ALTER TABLE {quote(table_name)} MODIFY COLUMN {quote(column_name)} "
            f"VARCHAR({target_length}) {suffix}"
        )


_CURRENT_TIMESTAMP_EXPRESSION = re.compile(
    r"^current_timestamp(?:\(([0-6]?)\))?$",
    re.IGNORECASE,
)
_ON_UPDATE_CURRENT_TIMESTAMP = re.compile(
    r"\bon\s+update\s+(current_timestamp(?:\([0-6]?\))?)(?=$|\s)",
    re.IGNORECASE,
)


def _mysql_datetime_expression(value: object) -> str | None:
    match = _CURRENT_TIMESTAMP_EXPRESSION.fullmatch(str(value or "").strip())
    if match is None:
        return None
    # MySQL requires CURRENT_TIMESTAMP/ON UPDATE precision to match the
    # fractional precision of the DATETIME column being rebuilt below.
    return f"CURRENT_TIMESTAMP({MYSQL_DATETIME_PRECISION})"


def _mysql_literal(conn, value: object) -> str:
    return str(
        literal(value).compile(
            dialect=conn.dialect,
            compile_kwargs={"literal_binds": True},
        )
    )


def _mysql_datetime_column_definition(conn, row: dict[str, object]) -> str:
    """Rebuild one DATETIME column without dropping its installed attributes."""
    quote = conn.dialect.identifier_preparer.quote
    column_name = str(row["column_name"])
    nullable = str(row.get("is_nullable") or "").upper() == "YES"
    default = row.get("column_default")
    extra = str(row.get("extra") or "").strip()
    generated_default = bool(
        re.search(r"\bDEFAULT_GENERATED\b", extra, re.IGNORECASE)
    )
    remaining_extra = re.sub(
        r"\bDEFAULT_GENERATED\b",
        " ",
        extra,
        flags=re.IGNORECASE,
    )

    on_update_matches = _ON_UPDATE_CURRENT_TIMESTAMP.findall(remaining_extra)
    if len(on_update_matches) > 1:
        raise RuntimeError(
            f"MySQL 时间列 {row['table_name']}.{column_name} 包含重复 ON UPDATE 定义"
        )
    remaining_extra = _ON_UPDATE_CURRENT_TIMESTAMP.sub(" ", remaining_extra)
    invisible = bool(re.search(r"\bINVISIBLE\b", remaining_extra, re.IGNORECASE))
    remaining_extra = re.sub(
        r"\bINVISIBLE\b",
        " ",
        remaining_extra,
        flags=re.IGNORECASE,
    )
    if remaining_extra.strip():
        raise RuntimeError(
            f"MySQL 时间列 {row['table_name']}.{column_name} 包含不支持的 EXTRA 属性"
        )

    parts = [
        f"{quote(column_name)} DATETIME({MYSQL_DATETIME_PRECISION})",
        "NULL" if nullable else "NOT NULL",
    ]
    if default is None:
        if nullable:
            parts.append("DEFAULT NULL")
    else:
        default_expression = _mysql_datetime_expression(default)
        if generated_default and default_expression is None:
            raise RuntimeError(
                f"MySQL 时间列 {row['table_name']}.{column_name} 使用了不支持的生成默认值"
            )
        parts.append(
            "DEFAULT "
            + (
                default_expression
                if default_expression is not None
                else _mysql_literal(conn, default)
            )
        )
    if on_update_matches:
        expression = _mysql_datetime_expression(on_update_matches[0])
        if expression is None:  # pragma: no cover - constrained by the regex.
            raise RuntimeError("MySQL 时间列 ON UPDATE 表达式无效")
        parts.append(f"ON UPDATE {expression}")
    if invisible:
        parts.append("INVISIBLE")
    comment = str(row.get("column_comment") or "")
    if comment:
        parts.append(f"COMMENT {_mysql_literal(conn, comment)}")
    return " ".join(parts)


def _widen_mysql_datetime_precision(conn) -> None:
    """Expand every installed ORM DATETIME to microseconds without narrowing."""
    if conn.dialect.name != "mysql":
        return

    managed_columns = {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if str(column.type.compile(dialect=conn.dialect)).upper()
        == f"DATETIME({MYSQL_DATETIME_PRECISION})"
    }
    installed = conn.execute(
        text(
            "SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name, "
            "DATA_TYPE AS data_type, DATETIME_PRECISION AS datetime_precision, "
            "IS_NULLABLE AS is_nullable, COLUMN_DEFAULT AS column_default, "
            "EXTRA AS extra, COLUMN_COMMENT AS column_comment "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE()"
        )
    ).mappings()
    modifications: dict[str, list[str]] = {}
    for raw_row in installed:
        row = dict(raw_row)
        table_name = str(row.get("table_name") or "")
        column_name = str(row.get("column_name") or "")
        if (table_name, column_name) not in managed_columns:
            continue
        if str(row.get("data_type") or "").casefold() != "datetime":
            raise RuntimeError(
                f"MySQL ORM 时间列 {table_name}.{column_name} 不是 DATETIME，已中止精度升级"
            )
        precision = int(row.get("datetime_precision") or 0)
        if precision >= MYSQL_DATETIME_PRECISION:
            continue
        modifications.setdefault(table_name, []).append(
            _mysql_datetime_column_definition(conn, row)
        )

    quote = conn.dialect.identifier_preparer.quote
    for table_name in sorted(modifications):
        clauses = ", ".join(
            f"MODIFY COLUMN {definition}"
            for definition in modifications[table_name]
        )
        conn.exec_driver_sql(f"ALTER TABLE {quote(table_name)} {clauses}")


def _migrate_mysql_datetime_precision() -> None:
    if engine.dialect.name != "mysql":
        return
    with engine.begin() as conn:
        _widen_mysql_datetime_precision(conn)


def _migrate_bucket_storage_metadata() -> None:
    """Add durable object-storage identity while preserving legacy local rows."""
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("bucket_files"):
            return
        existing = {column["name"] for column in inspector.get_columns("bucket_files")}
        columns = {
            "storage_provider": "VARCHAR(20) NOT NULL DEFAULT 'local'",
            "bucket_name": "VARCHAR(255) NOT NULL DEFAULT ''",
            "object_key": "VARCHAR(2048) NOT NULL DEFAULT ''",
            "object_version_id": "VARCHAR(255) NOT NULL DEFAULT ''",
            "etag": "VARCHAR(128) NOT NULL DEFAULT ''",
            "object_url": "VARCHAR(4096) NOT NULL DEFAULT ''",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.exec_driver_sql(
                    f"ALTER TABLE bucket_files ADD COLUMN {name} {definition}"
                )
        _widen_mysql_varchar_columns(
            conn,
            "bucket_files",
            {
                "stored_path": (4096, "NOT NULL"),
                "object_key": (2048, "NOT NULL DEFAULT ''"),
                "object_url": (4096, "NOT NULL DEFAULT ''"),
            },
        )
        conn.exec_driver_sql(
            "UPDATE bucket_files SET storage_provider = 'local' "
            "WHERE storage_provider IS NULL OR TRIM(storage_provider) = ''"
        )
        for name in (
            "bucket_name",
            "object_key",
            "object_version_id",
            "etag",
            "object_url",
        ):
            conn.exec_driver_sql(
                f"UPDATE bucket_files SET {name} = '' WHERE {name} IS NULL"
            )


def _migrate_document_index_active_key() -> None:
    """给既有索引队列补充跨请求去重键。"""
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("document_index_jobs"):
            return
        columns = {column["name"] for column in inspector.get_columns("document_index_jobs")}
        if "active_key" not in columns:
            conn.exec_driver_sql("ALTER TABLE document_index_jobs ADD COLUMN active_key VARCHAR(32)")
        conn.exec_driver_sql(
            "UPDATE document_index_jobs SET active_key = bucket_file_id "
            "WHERE status IN ('queued', 'running', 'retry_waiting') "
            "AND (active_key IS NULL OR active_key = '')"
        )
        conn.exec_driver_sql(
            "UPDATE document_index_jobs SET active_key = NULL "
            "WHERE status NOT IN ('queued', 'running', 'retry_waiting') "
            "AND (active_key IS NULL OR active_key = '')"
        )
        # 旧库理论上可能已经有重复活跃行。保留最新的一条并让其余行进入失败终态，
        # 避免创建唯一索引时静默失败或继续并发改写同一份文档。
        duplicates = conn.exec_driver_sql(
            "SELECT tenant_id, active_key FROM document_index_jobs "
            "WHERE active_key IS NOT NULL AND active_key <> '' "
            "GROUP BY tenant_id, active_key HAVING COUNT(*) > 1"
        ).fetchall()
        for tenant_id, active_key in duplicates:
            rows = conn.execute(
                text(
                    "SELECT id FROM document_index_jobs WHERE tenant_id = :tenant_id "
                    "AND active_key = :active_key ORDER BY created_at DESC, id DESC"
                ),
                {"tenant_id": tenant_id, "active_key": active_key},
            ).fetchall()
            for (job_id,) in rows[1:]:
                conn.execute(
                    text(
                        "UPDATE document_index_jobs SET status = 'failed', active_key = NULL, "
                        "error = '已由较新的同文件索引任务替代' WHERE id = :job_id"
                    ),
                    {"job_id": job_id},
                )
        # 兼容 SQLite / PostgreSQL / MySQL 的基础建索引语法；重复索引错误仅代表
        # 已完成升级，不能阻断启动。
        try:
            conn.exec_driver_sql(
                "CREATE UNIQUE INDEX uq_document_index_jobs_active_key "
                "ON document_index_jobs (tenant_id, active_key)"
            )
        except Exception:  # noqa: BLE001
            pass


def _migrate_llm_management() -> None:
    """为已有 LLM 配置补齐 P1 能力/路由/计费字段。

    新 trace 与评测表由 ``create_all`` 创建；这里处理已经存在的
    ``llm_configs``，并使用 SQLite、PostgreSQL、MySQL 都支持的基础 ALTER。
    """
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("llm_configs"):
            return
        existing = {column["name"] for column in inspector.get_columns("llm_configs")}
        columns = {
            "capabilities": "JSON",
            "enabled": "BOOLEAN",
            "routing_priority": "INTEGER",
            "input_cost_per_million": "FLOAT",
            "output_cost_per_million": "FLOAT",
            "budget_limit": "FLOAT",
            "cost_currency": "VARCHAR(12)",
            "updated_at": "TIMESTAMP",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE llm_configs ADD COLUMN {name} {definition}")
        # 不用 dialect 专属 JSON 函数，直接写入合法 JSON 文本，三种目标数据库均可接受。
        conn.exec_driver_sql(
            "UPDATE llm_configs SET capabilities = '[\"chat\", \"tool\"]' "
            "WHERE capabilities IS NULL"
        )
        conn.exec_driver_sql("UPDATE llm_configs SET enabled = TRUE WHERE enabled IS NULL")
        conn.exec_driver_sql(
            "UPDATE llm_configs SET routing_priority = 100 WHERE routing_priority IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE llm_configs SET input_cost_per_million = 0 WHERE input_cost_per_million IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE llm_configs SET output_cost_per_million = 0 WHERE output_cost_per_million IS NULL"
        )
        conn.exec_driver_sql("UPDATE llm_configs SET budget_limit = 0 WHERE budget_limit IS NULL")
        conn.exec_driver_sql("UPDATE llm_configs SET cost_currency = 'USD' WHERE cost_currency IS NULL")
        conn.exec_driver_sql(
            "UPDATE llm_configs SET updated_at = COALESCE(updated_at, created_at, CURRENT_TIMESTAMP) "
            "WHERE updated_at IS NULL"
        )


def _migrate_llm_trace_context() -> None:
    """为既有模型调用追踪补齐最小回链字段，不保存 prompt 或业务原文。"""
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("llm_invocation_traces"):
            return
        existing = {column["name"] for column in inspector.get_columns("llm_invocation_traces")}
        columns = {
            "correlation_id": "VARCHAR(64)",
            "agent_id": "VARCHAR(32)",
            "conversation_id": "VARCHAR(32)",
            "scenario_id": "VARCHAR(32)",
            "user_id": "VARCHAR(32)",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE llm_invocation_traces ADD COLUMN {name} {definition}")
        conn.exec_driver_sql(
            "UPDATE llm_invocation_traces SET correlation_id = '' WHERE correlation_id IS NULL"
        )


def _migrate_message_citations() -> None:
    """为既有 Agent 对话消息补充结构化 RAG 引用列。"""
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("messages"):
            return
        existing = {column["name"] for column in inspector.get_columns("messages")}
        if "citations" not in existing:
            # SQLite、PostgreSQL 和 MySQL 都接受 JSON 类型声明；避免仅本地可用的迁移。
            conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN citations JSON")
        if "stream_finalized" not in existing:
            boolean_true = "1" if conn.dialect.name == "sqlite" else "TRUE"
            conn.exec_driver_sql(
                f"ALTER TABLE messages ADD COLUMN stream_finalized BOOLEAN DEFAULT {boolean_true}"
            )
        conn.exec_driver_sql("UPDATE messages SET citations = '[]' WHERE citations IS NULL")
        # Historical messages predate streaming confirmation and are already
        # immutable transcripts, so they are safe to treat as finalized.
        boolean_true = "1" if conn.dialect.name == "sqlite" else "TRUE"
        conn.exec_driver_sql(
            f"UPDATE messages SET stream_finalized = {boolean_true} WHERE stream_finalized IS NULL"
        )


def _migrate_permission_controls() -> None:
    """为既有本体资源补充 P1 细粒度权限控制列。

    组织、角色、成员和授权是新表，已由 ``create_all`` 创建；这里仅处理已经存在
    的属性、对象、Action 和工作流表，且使用 SQLite / PostgreSQL / MySQL 共通的
    基础 ``ALTER TABLE ADD COLUMN`` 语法。
    """
    columns_by_table = {
        "ontology_properties": {
            "is_sensitive": "BOOLEAN DEFAULT 0",
        },
        "ontology_instances": {
            "access_scope": "VARCHAR(20) DEFAULT 'tenant'",
            "source_metadata": "JSON",
        },
        "ontology_actions": {
            "access_scope": "VARCHAR(20) DEFAULT 'tenant'",
        },
        "ontology_workflows": {
            "access_scope": "VARCHAR(20) DEFAULT 'tenant'",
        },
    }
    with engine.begin() as conn:
        inspector = inspect(conn)
        for table, columns in columns_by_table.items():
            if not inspector.has_table(table):
                continue
            existing = {column["name"] for column in inspector.get_columns(table)}
            for name, definition in columns.items():
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            if table == "ontology_properties":
                conn.exec_driver_sql(
                    "UPDATE ontology_properties SET is_sensitive = 0 WHERE is_sensitive IS NULL"
                )
            else:
                conn.exec_driver_sql(
                    f"UPDATE {table} SET access_scope = 'tenant' "
                    "WHERE access_scope IS NULL OR access_scope = ''"
                )
                if table == "ontology_instances":
                    conn.exec_driver_sql(
                        "UPDATE ontology_instances SET source_metadata = '{}' "
                        "WHERE source_metadata IS NULL"
                    )


def _migrate_release_governance() -> None:
    """初始化 P2 发布治理表的安全默认状态。

    分支、快照、提案、评审、发布与回滚均为新表，已由 ``create_all`` 创建；这里保留
    轻量回填以支持早期预览库中可能存在的 NULL 状态列，并避免升级后把未定义状态当成
    可合并/可发布对象。
    """
    with engine.begin() as conn:
        inspector = inspect(conn)
        if inspector.has_table("ontology_branches"):
            conn.exec_driver_sql(
                "UPDATE ontology_branches SET status = 'active' "
                "WHERE status IS NULL OR status = ''"
            )
        if inspector.has_table("ontology_proposals"):
            conn.exec_driver_sql(
                "UPDATE ontology_proposals SET status = 'submitted' "
                "WHERE status IS NULL OR status = ''"
            )
        if inspector.has_table("ontology_releases"):
            conn.exec_driver_sql(
                "UPDATE ontology_releases SET status = 'released' "
                "WHERE status IS NULL OR status = ''"
            )


def _migrate_connector_governance() -> None:
    """Add connector audit and immutable revision fields to existing P2 tables.

    ``connector_bindings`` itself is a new table and is created by metadata;
    SQLite/PostgreSQL/MySQL do not add columns to an existing release table via
    ``create_all`` though, so preserve old release/rollback records explicitly.
    Connector target revisions are opaque integers: unlike a configuration hash
    they do not expose endpoint or credential material in audit JSON.
    """
    with engine.begin() as conn:
        inspector = inspect(conn)
        for table in ("ontology_releases", "ontology_rollbacks"):
            if not inspector.has_table(table):
                continue
            existing = {column["name"] for column in inspector.get_columns(table)}
            if "connector_audit" not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN connector_audit JSON")
            conn.exec_driver_sql(
                f"UPDATE {table} SET connector_audit = '[]' "
                "WHERE connector_audit IS NULL"
            )
        for table in ("data_sources", "mcp_configs", "llm_configs"):
            if not inspector.has_table(table):
                continue
            existing = {column["name"] for column in inspector.get_columns(table)}
            if "connector_revision" not in existing:
                conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN connector_revision INTEGER NOT NULL DEFAULT 1"
                )
            conn.exec_driver_sql(
                f"UPDATE {table} SET connector_revision = 1 "
                "WHERE connector_revision IS NULL OR connector_revision < 1"
            )


def _migrate_mcp_name_identity() -> None:
    """Install one Unicode-normalized MCP name identity per tenant.

    Application-side conflict checks are useful for friendly errors but cannot
    close a concurrent check-then-insert race.  Existing duplicates are never
    renamed or deleted automatically: startup fails with the exact row ids so
    an operator can resolve the ambiguity deliberately.
    """
    from .models import normalize_mcp_name_key

    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("mcp_configs"):
            return
        existing = {
            column["name"] for column in inspector.get_columns("mcp_configs")
        }
        if "name_key" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE mcp_configs ADD COLUMN name_key VARCHAR(600)"
            )

        rows = conn.execute(text(
            "SELECT id, tenant_id, name FROM mcp_configs ORDER BY tenant_id, id"
        )).mappings().all()
        identities: dict[tuple[str, str], list[str]] = {}
        updates: list[dict[str, str]] = []
        for row in rows:
            key = normalize_mcp_name_key(str(row["name"] or ""))
            tenant_id = str(row["tenant_id"] or "")
            if tenant_id:
                identities.setdefault((tenant_id, key), []).append(str(row["id"]))
            updates.append({"id": str(row["id"]), "name_key": key})
        duplicates = [
            (tenant_id, key, ids)
            for (tenant_id, key), ids in identities.items()
            if len(ids) > 1
        ]
        if duplicates:
            tenant_id, key, ids = duplicates[0]
            raise RuntimeError(
                "MCP 名称规范化后存在租户内重复，无法安全建立唯一约束："
                f"tenant={tenant_id}, name_key={key!r}, ids={','.join(ids)}"
            )
        if updates:
            conn.execute(
                text("UPDATE mcp_configs SET name_key=:name_key WHERE id=:id"),
                updates,
            )

        refreshed = inspect(conn)
        identity_names = {
            item.get("name") for item in refreshed.get_unique_constraints("mcp_configs")
        } | {
            item.get("name") for item in refreshed.get_indexes("mcp_configs")
            if item.get("unique")
        }
        if "uq_mcp_configs_tenant_name_key" not in identity_names:
            metadata = MetaData()
            table = Table("mcp_configs", metadata, autoload_with=conn)
            Index(
                "uq_mcp_configs_tenant_name_key",
                table.c.tenant_id,
                table.c.name_key,
                unique=True,
            ).create(conn)


def _migrate_workflow_run_environment() -> None:
    """Safely isolate old workflow runs that lack an environment snapshot.

    A pre-P2 queue record has no reliable deployment-environment provenance.
    It must not silently become a ``dev`` run after an upgrade: doing so could
    re-enable the legacy direct-ID connector path in a staging/prod worker.
    Terminal history is labelled with this deployment for display only; active
    legacy runs are cancelled and must be submitted again by an operator.
    """
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("workflow_runs"):
            return
        existing = {column["name"] for column in inspector.get_columns("workflow_runs")}
        if "environment" not in existing:
            # No SQL default here: existing records must remain distinguishable
            # as unknown until they are quarantined below.
            conn.exec_driver_sql("ALTER TABLE workflow_runs ADD COLUMN environment VARCHAR(20)")
        deployment_environment = str(_settings.runtime_environment)
        unknown_environment = "environment IS NULL OR TRIM(environment) = ''"
        terminal_statuses = "'succeeded', 'failed', 'timed_out', 'rejected', 'cancelled'"
        conn.exec_driver_sql(
            f"UPDATE workflow_runs SET environment = '{deployment_environment}' "
            f"WHERE ({unknown_environment}) AND status IN ({terminal_statuses})"
        )
        conn.exec_driver_sql(
            f"UPDATE workflow_runs SET environment = '{deployment_environment}', "
            "status = 'cancelled', "
            "error = '运行环境快照缺失，部署升级后已安全取消，请重新提交', "
            "completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP), "
            "next_retry_at = NULL "
            f"WHERE ({unknown_environment}) AND status NOT IN ({terminal_statuses})"
        )


def _migrate_workflow_run_execution_key() -> None:
    """Give pre-existing runs a stable idempotency lineage.

    A queue retry must not mint a new Action idempotency key.  Existing runs did
    not persist such a lineage, so their immutable run ID is the safest compatible
    backfill.  New rows receive the same value immediately after their ID exists.
    """
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("workflow_runs"):
            return
        existing = {column["name"] for column in inspector.get_columns("workflow_runs")}
        if "execution_key" not in existing:
            conn.exec_driver_sql("ALTER TABLE workflow_runs ADD COLUMN execution_key VARCHAR(64)")
        conn.exec_driver_sql(
            "UPDATE workflow_runs SET execution_key = id "
            "WHERE execution_key IS NULL OR TRIM(execution_key) = ''"
        )


def _migrate_runtime_definition_pins() -> None:
    """Add immutable runtime-definition provenance to existing P1 records.

    New releases execute from a snapshot in staging/prod.  Historic non-dev
    queue entries lack that essential provenance, so active ones are cancelled
    rather than silently falling forward to today's active release.  Dev keeps
    its authoring/live compatibility path; completed historic rows remain
    readable and are explicitly labelled ``live``.
    """
    columns_by_table = {
        "workflow_runs": {
            "definition_snapshot_id": "VARCHAR(32)",
            "release_id": "VARCHAR(32)",
            "definition_hash": "VARCHAR(64)",
            "definition_source": "VARCHAR(20)",
        },
        "event_envelopes": {
            "environment": "VARCHAR(20)",
            "definition_snapshot_id": "VARCHAR(32)",
            "release_id": "VARCHAR(32)",
            "definition_hash": "VARCHAR(64)",
            "definition_source": "VARCHAR(20)",
        },
        "action_execution_logs": {
            "environment": "VARCHAR(20)",
            "definition_snapshot_id": "VARCHAR(32)",
            "release_id": "VARCHAR(32)",
            "definition_hash": "VARCHAR(64)",
            "definition_source": "VARCHAR(20)",
        },
    }
    with engine.begin() as conn:
        inspector = inspect(conn)
        for table, columns in columns_by_table.items():
            if not inspector.has_table(table):
                continue
            existing = {column["name"] for column in inspector.get_columns(table)}
            for name, definition in columns.items():
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

        deployment_environment = str(_settings.runtime_environment)
        if inspector.has_table("workflow_runs"):
            conn.exec_driver_sql(
                "UPDATE workflow_runs SET definition_hash = '' "
                "WHERE definition_hash IS NULL"
            )
            conn.exec_driver_sql(
                "UPDATE workflow_runs SET definition_source = 'live' "
                "WHERE definition_source IS NULL OR TRIM(definition_source) = ''"
            )
            terminal_statuses = "'succeeded', 'failed', 'timed_out', 'rejected', 'cancelled'"
            conn.exec_driver_sql(
                "UPDATE workflow_runs SET status = 'cancelled', "
                "error = '运行定义快照缺失，部署升级后已安全取消，请重新提交', "
                "completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP), "
                "next_retry_at = NULL "
                "WHERE environment IN ('staging', 'prod') "
                "AND (definition_snapshot_id IS NULL OR release_id IS NULL) "
                f"AND status NOT IN ({terminal_statuses})"
            )
        for table in ("event_envelopes", "action_execution_logs"):
            if not inspector.has_table(table):
                continue
            conn.exec_driver_sql(
                f"UPDATE {table} SET environment = '{deployment_environment}' "
                "WHERE environment IS NULL OR TRIM(environment) = ''"
            )
            conn.exec_driver_sql(
                f"UPDATE {table} SET definition_hash = '' WHERE definition_hash IS NULL"
            )
            conn.exec_driver_sql(
                f"UPDATE {table} SET definition_source = 'live' "
                "WHERE definition_source IS NULL OR TRIM(definition_source) = ''"
            )

        index_specs = {
            "workflow_runs": ("ix_workflow_runs_release", "release_id, definition_snapshot_id"),
            "event_envelopes": (
                "ix_event_envelopes_definition_snapshot_id",
                "definition_snapshot_id",
            ),
            "action_execution_logs": (
                "ix_action_execution_logs_definition_snapshot_id",
                "definition_snapshot_id",
            ),
        }
        refreshed = inspect(conn)
        for table, (name, columns) in index_specs.items():
            if not refreshed.has_table(table):
                continue
            existing_indexes = {index["name"] for index in refreshed.get_indexes(table)}
            if name not in existing_indexes:
                conn.exec_driver_sql(f"CREATE INDEX {name} ON {table} ({columns})")


def _migrate_action_decision_chain() -> None:
    """Add verifiable user/Agent/model/data/permission provenance to Action logs.

    Existing rows cannot prove those identities, so the migration intentionally
    leaves all identity columns NULL and labels their actor as ``unknown``.
    This is preferable to fabricating the scenario owner or a system actor.
    """
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("action_execution_logs"):
            return
        existing = {
            column["name"] for column in inspector.get_columns("action_execution_logs")
        }
        columns = {
            "actor_type": "VARCHAR(20)",
            "actor_user_id": "VARCHAR(32)",
            "agent_id": "VARCHAR(32)",
            "llm_config_id": "VARCHAR(32)",
            "model_name": "VARCHAR(240)",
            "permission_decision": "JSON",
            "data_context": "JSON",
            "correlation_id": "VARCHAR(64)",
            "parent_action_log_id": "VARCHAR(32)",
            "agent_message_id": "VARCHAR(32)",
            "assistant_message_id": "VARCHAR(32)",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.exec_driver_sql(
                    f"ALTER TABLE action_execution_logs ADD COLUMN {name} {definition}"
                )
        conn.exec_driver_sql(
            "UPDATE action_execution_logs SET actor_type = 'unknown' "
            "WHERE actor_type IS NULL OR TRIM(actor_type) = ''"
        )
        conn.exec_driver_sql(
            "UPDATE action_execution_logs SET model_name = '' WHERE model_name IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE action_execution_logs SET correlation_id = '' WHERE correlation_id IS NULL"
        )
        for column in ("permission_decision", "data_context"):
            conn.exec_driver_sql(
                f"UPDATE action_execution_logs SET {column} = '{{}}' WHERE {column} IS NULL"
            )

        refreshed = inspect(conn)
        existing_indexes = {
            index["name"] for index in refreshed.get_indexes("action_execution_logs")
        }
        for name, column in (
            ("ix_action_execution_logs_actor_user_id", "actor_user_id"),
            ("ix_action_execution_logs_agent_id", "agent_id"),
            ("ix_action_execution_logs_llm_config_id", "llm_config_id"),
            ("ix_action_execution_logs_correlation_id", "correlation_id"),
            ("ix_action_execution_logs_parent_action_log_id", "parent_action_log_id"),
            ("ix_action_execution_logs_agent_message_id", "agent_message_id"),
            ("ix_action_execution_logs_assistant_message_id", "assistant_message_id"),
        ):
            if name not in existing_indexes:
                try:
                    conn.exec_driver_sql(
                        f"CREATE INDEX {name} ON action_execution_logs ({column})"
                    )
                except Exception:  # noqa: BLE001 - tolerate startup index races.
                    pass
        existing_indexes = {
            index["name"] for index in inspect(conn).get_indexes("action_execution_logs")
        }
        if "uq_action_execution_logs_parent_preview" not in existing_indexes:
            try:
                conn.exec_driver_sql(
                    "CREATE UNIQUE INDEX uq_action_execution_logs_parent_preview "
                    "ON action_execution_logs (parent_action_log_id)"
                )
            except Exception:  # noqa: BLE001 - startup race or invalid legacy duplicates.
                pass

        # SQLite cannot attach REFERENCES constraints with ALTER COLUMN.  For
        # upgraded databases, clean unverifiable orphan ids and install
        # equivalent fail-closed/set-null triggers.  Fresh databases already
        # have native FKs; the triggers are idempotent and reinforce the same
        # behavior rather than changing it.
        if conn.dialect.name == "sqlite":
            refreshed = inspect(conn)
            references = (
                ("actor_user_id", "users", "id"),
                ("agent_id", "agents", "id"),
                ("llm_config_id", "llm_configs", "id"),
                ("parent_action_log_id", "action_execution_logs", "id"),
                ("agent_message_id", "messages", "id"),
                ("assistant_message_id", "assistant_messages", "id"),
            )
            for column, parent_table, parent_key in references:
                if not refreshed.has_table(parent_table):
                    continue
                conn.exec_driver_sql(
                    f"UPDATE action_execution_logs SET {column} = NULL "
                    f"WHERE {column} IS NOT NULL AND NOT EXISTS ("
                    f"SELECT 1 FROM {parent_table} p WHERE p.{parent_key} = action_execution_logs.{column})"
                )
                trigger_base = f"trg_action_logs_{column}"
                conn.exec_driver_sql(
                    f"CREATE TRIGGER IF NOT EXISTS {trigger_base}_insert "
                    "BEFORE INSERT ON action_execution_logs "
                    f"WHEN NEW.{column} IS NOT NULL AND NOT EXISTS ("
                    f"SELECT 1 FROM {parent_table} p WHERE p.{parent_key} = NEW.{column}) "
                    "BEGIN SELECT RAISE(ABORT, 'invalid action audit reference'); END"
                )
                conn.exec_driver_sql(
                    f"CREATE TRIGGER IF NOT EXISTS {trigger_base}_update "
                    f"BEFORE UPDATE OF {column} ON action_execution_logs "
                    f"WHEN NEW.{column} IS NOT NULL AND NOT EXISTS ("
                    f"SELECT 1 FROM {parent_table} p WHERE p.{parent_key} = NEW.{column}) "
                    "BEGIN SELECT RAISE(ABORT, 'invalid action audit reference'); END"
                )
                conn.exec_driver_sql(
                    f"CREATE TRIGGER IF NOT EXISTS {trigger_base}_delete "
                    f"AFTER DELETE ON {parent_table} BEGIN "
                    f"UPDATE action_execution_logs SET {column} = NULL "
                    f"WHERE {column} = OLD.{parent_key}; END"
                )


def _relation_duplicate_groups_statement():
    return text(
        "SELECT COUNT(*) FROM ("
        "SELECT relation_id, source_instance_id, target_instance_id "
        "FROM relation_instances "
        "GROUP BY relation_id, source_instance_id, target_instance_id "
        "HAVING COUNT(*) > 1"
        ") AS duplicate_edges"
    )


def _promote_legacy_title_keys(conn) -> None:
    candidate_ids = conn.execute(
        text(
            "SELECT MIN(candidate.id) FROM ontology_properties candidate "
            "WHERE candidate.is_key = :true_value AND NOT EXISTS ("
            "SELECT 1 FROM ontology_properties titled "
            "WHERE titled.entity_id = candidate.entity_id "
            "AND titled.is_title = :true_value"
            ") GROUP BY candidate.entity_id"
        ),
        {"true_value": True},
    ).scalars().all()
    if not candidate_ids:
        return
    properties = table(
        "ontology_properties",
        column("id"),
        column("is_title"),
    )
    conn.execute(
        update(properties)
        .where(properties.c.id.in_(candidate_ids))
        .values(is_title=True)
    )


def _migrate_ontology_runtime_metadata() -> None:
    """Add the P0 namespace/constraint/state/validity/quality/mapping metadata."""
    columns_by_table = {
        "business_scenarios": {
            "namespace": "VARCHAR(180)",
        },
        "ontology_entities": {
            "namespace": "VARCHAR(180)",
            "state_property": "VARCHAR(200)",
        },
        "ontology_properties": {
            "constraints": "JSON",
            "is_title": "BOOLEAN",
        },
        "ontology_relations": {
            "namespace": "VARCHAR(180)",
            "constraints": "JSON",
        },
        "ontology_instances": {
            "state": "VARCHAR(120)",
            "valid_from": "DATETIME",
            "valid_to": "DATETIME",
            "quality": "JSON",
        },
        "relation_instances": {
            "source": "VARCHAR(20)",
            "source_ref": "VARCHAR(500)",
            "source_metadata": "JSON",
        },
        "data_mappings": {
            "transform_rules": "JSON",
        },
    }
    with engine.begin() as conn:
        inspector = inspect(conn)
        for table, columns in columns_by_table.items():
            if not inspector.has_table(table):
                continue
            existing = {column["name"] for column in inspector.get_columns(table)}
            for name, definition in columns.items():
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        for table, column, value in (
            ("business_scenarios", "namespace", "default"),
            ("ontology_entities", "namespace", "default"),
            ("ontology_entities", "state_property", ""),
            ("ontology_relations", "namespace", "default"),
            ("ontology_instances", "state", ""),
            ("relation_instances", "source", "manual"),
            ("relation_instances", "source_ref", ""),
        ):
            if inspector.has_table(table):
                conn.execute(
                    text(
                        f"UPDATE {table} SET {column} = :value "
                        f"WHERE {column} IS NULL OR TRIM({column}) = ''"
                    ),
                    {"value": value},
                )
        for table, column in (
            ("ontology_properties", "constraints"),
            ("ontology_relations", "constraints"),
            ("ontology_instances", "quality"),
            ("relation_instances", "source_metadata"),
            ("data_mappings", "transform_rules"),
        ):
            if inspector.has_table(table):
                conn.exec_driver_sql(
                    f"UPDATE {table} SET {column} = '{{}}' WHERE {column} IS NULL"
                )
        if inspector.has_table("ontology_properties"):
            property_columns = {
                column["name"] for column in inspect(conn).get_columns("ontology_properties")
            }
            if "is_title" in property_columns:
                conn.execute(
                    text(
                        "UPDATE ontology_properties SET is_title = :false_value "
                        "WHERE is_title IS NULL"
                    ),
                    {"false_value": False},
                )
            if {"entity_id", "is_key", "is_title"}.issubset(property_columns):
                # Legacy object types used their primary key as the display
                # label. Preserve that deterministic behaviour while making
                # title-key semantics explicit and independently editable.
                _promote_legacy_title_keys(conn)
        if inspector.has_table("ontology_instances"):
            indexes = {
                index["name"] for index in inspect(conn).get_indexes("ontology_instances")
            }
            if "ix_ontology_instances_state" not in indexes:
                try:
                    conn.exec_driver_sql(
                        "CREATE INDEX ix_ontology_instances_state ON ontology_instances (state)"
                    )
                except Exception:  # noqa: BLE001 - tolerate startup races.
                    pass
        if inspector.has_table("relation_instances"):
            refreshed = inspect(conn)
            relation_columns = {
                column["name"] for column in refreshed.get_columns("relation_instances")
            }
            edge_columns = {
                "id", "relation_id", "source_instance_id", "target_instance_id"
            }
            if edge_columns.issubset(relation_columns):
                duplicate_count = conn.execute(
                    _relation_duplicate_groups_statement()
                ).scalar_one()
                unique_names = {
                    item.get("name")
                    for item in refreshed.get_unique_constraints("relation_instances")
                }
                index_names = {
                    item.get("name") for item in refreshed.get_indexes("relation_instances")
                }
                if duplicate_count:
                    # Never delete historical edges during an automatic boot.
                    # The unique guard can be added after an operator resolves
                    # the pre-existing ambiguity deliberately.
                    logger.warning(
                        "relation_instances 存在 %s 组重复边，保留全部记录并跳过唯一索引；"
                        "请人工处理后重新启动",
                        duplicate_count,
                    )
                elif "uq_relation_instances_edge" not in unique_names | index_names:
                    # Concurrency correctness depends on this database guard.
                    # A failed DDL must stop startup instead of silently
                    # downgrading relation-instance idempotency to best effort.
                    conn.exec_driver_sql(
                        "CREATE UNIQUE INDEX uq_relation_instances_edge ON "
                        "relation_instances (relation_id, source_instance_id, target_instance_id)"
                    )


def _migrate_assistant_attachment_lifecycle() -> None:
    """Bind temporary uploads to one thread and give legacy rows a short TTL."""
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("assistant_attachments"):
            return
        existing = {
            column["name"] for column in inspector.get_columns("assistant_attachments")
        }
        for name, definition in (
            ("thread_id", "VARCHAR(32)"),
            ("consumed_at", "DATETIME"),
            ("expires_at", "DATETIME"),
            ("storage_provider", "VARCHAR(20) NOT NULL DEFAULT 'none'"),
            ("bucket_name", "VARCHAR(255) NOT NULL DEFAULT ''"),
            ("object_key", "VARCHAR(2048) NOT NULL DEFAULT ''"),
            ("object_version_id", "VARCHAR(255) NOT NULL DEFAULT ''"),
            ("etag", "VARCHAR(128) NOT NULL DEFAULT ''"),
            ("object_url", "VARCHAR(4096) NOT NULL DEFAULT ''"),
        ):
            if name not in existing:
                conn.exec_driver_sql(
                    f"ALTER TABLE assistant_attachments ADD COLUMN {name} {definition}"
                )
        _widen_mysql_varchar_columns(
            conn,
            "assistant_attachments",
            {
                "object_key": (2048, "NOT NULL DEFAULT ''"),
                "object_url": (4096, "NOT NULL DEFAULT ''"),
            },
        )
        conn.exec_driver_sql(
            "UPDATE assistant_attachments SET storage_provider = 'none' "
            "WHERE storage_provider IS NULL OR TRIM(storage_provider) = ''"
        )
        for name in (
            "bucket_name",
            "object_key",
            "object_version_id",
            "etag",
            "object_url",
        ):
            conn.exec_driver_sql(
                f"UPDATE assistant_attachments SET {name} = '' WHERE {name} IS NULL"
            )
        expiry = datetime.now(timezone.utc)
        conn.execute(
            text(
                "UPDATE assistant_attachments SET expires_at = :expiry "
                "WHERE expires_at IS NULL"
            ),
            {"expiry": expiry},
        )
        if inspector.has_table("assistant_threads"):
            # SQLite cannot add an FK with ALTER TABLE.  Preserve unowned
            # legacy context by detaching it from the missing thread, then
            # install equivalent fail-closed/cascade triggers for upgraded
            # databases.  Fresh databases already have the real FK; the
            # idempotent triggers are harmless there.
            conn.execute(
                _nullable_orphan_repair_statement(
                    "assistant_attachments",
                    "thread_id",
                    "assistant_threads",
                )
            )
            if conn.dialect.name == "sqlite":
                conn.exec_driver_sql(
                    "CREATE TRIGGER IF NOT EXISTS trg_assistant_attachment_thread_insert "
                    "BEFORE INSERT ON assistant_attachments "
                    "WHEN NEW.thread_id IS NOT NULL AND NOT EXISTS ("
                    "SELECT 1 FROM assistant_threads WHERE id = NEW.thread_id"
                    ") BEGIN SELECT RAISE(ABORT, 'invalid assistant attachment thread'); END"
                )
                conn.exec_driver_sql(
                    "CREATE TRIGGER IF NOT EXISTS trg_assistant_attachment_thread_update "
                    "BEFORE UPDATE OF thread_id ON assistant_attachments "
                    "WHEN NEW.thread_id IS NOT NULL AND NOT EXISTS ("
                    "SELECT 1 FROM assistant_threads WHERE id = NEW.thread_id"
                    ") BEGIN SELECT RAISE(ABORT, 'invalid assistant attachment thread'); END"
                )
                conn.exec_driver_sql(
                    "CREATE TRIGGER IF NOT EXISTS trg_assistant_attachment_thread_delete "
                    "AFTER DELETE ON assistant_threads BEGIN "
                    "DELETE FROM assistant_attachments WHERE thread_id = OLD.id; END"
                )
        indexes = {
            index["name"] for index in inspect(conn).get_indexes("assistant_attachments")
        }
        for name, column in (
            ("ix_assistant_attachments_thread_id", "thread_id"),
            ("ix_assistant_attachments_expires_at", "expires_at"),
        ):
            if name not in indexes:
                try:
                    conn.exec_driver_sql(
                        f"CREATE INDEX {name} ON assistant_attachments ({column})"
                    )
                except Exception:  # noqa: BLE001
                    pass


def _migrate_assistant_compilation_jobs() -> None:
    """Install the durable compilation ledger and attachment byte hashes.

    ``create_all`` creates the new job table on fresh and upgraded installs.
    This migration is intentionally idempotent so partially upgraded SQLite
    deployments also receive the content-hash column and database uniqueness
    guard before an assistant request can reach a provider.
    """
    with engine.begin() as conn:
        inspector = inspect(conn)
        if inspector.has_table("assistant_attachments"):
            attachment_columns = {
                column["name"]
                for column in inspector.get_columns("assistant_attachments")
            }
            if "content_hash" not in attachment_columns:
                conn.exec_driver_sql(
                    "ALTER TABLE assistant_attachments "
                    "ADD COLUMN content_hash VARCHAR(64)"
                )
            rows = conn.exec_driver_sql(
                "SELECT id, parsed_text FROM assistant_attachments "
                "WHERE content_hash IS NULL OR content_hash = ''"
            ).fetchall()
            for attachment_id, parsed_text in rows:
                legacy_hash = hashlib.sha256(
                    str(parsed_text or "").encode("utf-8")
                ).hexdigest()
                conn.execute(
                    text(
                        "UPDATE assistant_attachments SET content_hash = :hash "
                        "WHERE id = :id"
                    ),
                    {"hash": legacy_hash, "id": attachment_id},
                )
            attachment_indexes = {
                index["name"]
                for index in inspect(conn).get_indexes("assistant_attachments")
            }
            if "ix_assistant_attachments_content_hash" not in attachment_indexes:
                conn.exec_driver_sql(
                    "CREATE INDEX ix_assistant_attachments_content_hash "
                    "ON assistant_attachments (content_hash)"
                )

        if not inspector.has_table("assistant_compilation_jobs"):
            # ``Base.metadata.create_all`` should have created it.  Failing
            # startup is safer than silently running without single-flight.
            raise RuntimeError("assistant_compilation_jobs 表未创建")
        job_columns = {
            column["name"]
            for column in inspect(conn).get_columns(
                "assistant_compilation_jobs"
            )
        }
        # Support an interrupted/intermediate deployment of this feature.  New
        # rows are created only after startup completes, so nullable ALTERs are
        # backfilled before the uniqueness guard is validated.
        for name, definition in (
            ("request_fingerprint", "VARCHAR(64)"),
            ("message_hash", "VARCHAR(64)"),
            ("attachment_content_hash", "VARCHAR(64)"),
            ("llm_config_fingerprint", "VARCHAR(64)"),
            ("mapping_context_fingerprint", "VARCHAR(64)"),
            ("execution_policy_fingerprint", "VARCHAR(64)"),
            ("compiler_version", "VARCHAR(80)"),
            ("scenario_baseline", "VARCHAR(64)"),
            ("execution_input", "JSON"),
            ("progress", "JSON"),
            ("llm_call_budget", "INTEGER"),
            ("llm_calls_used", "INTEGER"),
            ("lease_token", "VARCHAR(64)"),
            ("lease_expires_at", "TIMESTAMP"),
            ("lease_attempt", "INTEGER"),
            ("error", "TEXT"),
            ("result", "JSON"),
            ("completed_at", "DATETIME"),
            ("updated_at", "DATETIME"),
        ):
            if name not in job_columns:
                conn.exec_driver_sql(
                    f"ALTER TABLE assistant_compilation_jobs "
                    f"ADD COLUMN {name} {definition}"
                )
        rows = conn.exec_driver_sql(
            "SELECT id FROM assistant_compilation_jobs "
            "WHERE request_fingerprint IS NULL OR request_fingerprint = ''"
        ).fetchall()
        for (job_id,) in rows:
            legacy_fingerprint = hashlib.sha256(
                f"legacy-assistant-compilation-job:{job_id}".encode("utf-8")
            ).hexdigest()
            conn.execute(
                text(
                    "UPDATE assistant_compilation_jobs "
                    "SET request_fingerprint = :fingerprint WHERE id = :id"
                ),
                {"fingerprint": legacy_fingerprint, "id": job_id},
            )
        conn.exec_driver_sql(
            "UPDATE assistant_compilation_jobs SET "
            "message_hash = COALESCE(message_hash, ''), "
            "attachment_content_hash = COALESCE(attachment_content_hash, ''), "
            "llm_config_fingerprint = COALESCE(llm_config_fingerprint, ''), "
            "mapping_context_fingerprint = COALESCE(mapping_context_fingerprint, ''), "
            "execution_policy_fingerprint = COALESCE(execution_policy_fingerprint, ''), "
            "compiler_version = COALESCE(compiler_version, 'legacy'), "
            "scenario_baseline = COALESCE(scenario_baseline, ''), "
            "execution_input = COALESCE(execution_input, '{}'), "
            "progress = COALESCE(progress, '{}'), "
            "llm_call_budget = COALESCE(llm_call_budget, 1), "
            "llm_calls_used = COALESCE(llm_calls_used, 0), "
            "lease_token = COALESCE(lease_token, ''), "
            "lease_attempt = COALESCE(lease_attempt, 0), "
            "error = COALESCE(error, ''), "
            "result = COALESCE(result, '{}'), "
            "updated_at = COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)"
        )
        unique_names = {
            item.get("name")
            for item in inspect(conn).get_unique_constraints(
                "assistant_compilation_jobs"
            )
        }
        index_names = {
            item.get("name")
            for item in inspect(conn).get_indexes("assistant_compilation_jobs")
        }
        if (
            "uq_assistant_compilation_jobs_fingerprint"
            not in unique_names | index_names
        ):
            conn.exec_driver_sql(
                "CREATE UNIQUE INDEX uq_assistant_compilation_jobs_fingerprint "
                "ON assistant_compilation_jobs (request_fingerprint)"
            )
        if (
            "ix_assistant_compilation_jobs_status_lease_expiry"
            not in index_names
        ):
            conn.exec_driver_sql(
                "CREATE INDEX ix_assistant_compilation_jobs_status_lease_expiry "
                "ON assistant_compilation_jobs (status, lease_expires_at)"
            )


def _migrate_scenario_model_draft_resources() -> None:
    """Verify and index the inert scene-level assistant draft store.

    The table is introduced atomically by ``Base.metadata.create_all``.  This
    explicit, idempotent startup migration makes the safety boundary fail
    closed on interrupted/manual deployments instead of silently falling back
    to proposal-only storage.
    """
    table_name = "scenario_model_draft_resources"
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table(table_name):
            raise RuntimeError(f"{table_name} 表未创建")
        columns = {
            column["name"] for column in inspector.get_columns(table_name)
        }
        # The staging table predates active-lineage tracking in some local and
        # self-hosted deployments.  Add only inert provenance columns here;
        # runtime definition tables remain untouched.
        timestamp_definition = (
            "TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP"
            if engine.dialect.name == "postgresql"
            else f"DATETIME({MYSQL_DATETIME_PRECISION})"
            if engine.dialect.name == "mysql"
            else "DATETIME"
        )
        lineage_columns = {
            "lineage_started_at": timestamp_definition,
            "predecessor_draft_id": "VARCHAR(32) NOT NULL DEFAULT ''",
            "predecessor_revision": "INTEGER NOT NULL DEFAULT -1",
            "superseded_by_proposal_id": "VARCHAR(64) NOT NULL DEFAULT ''",
        }
        for name, definition in lineage_columns.items():
            if name not in columns:
                conn.exec_driver_sql(
                    f"ALTER TABLE {table_name} ADD COLUMN {name} {definition}"
                )
        conn.exec_driver_sql(
            "UPDATE scenario_model_draft_resources "
            "SET lineage_started_at = COALESCE(lineage_started_at, created_at)"
        )
        refreshed_columns = {
            column["name"]: column
            for column in inspect(conn).get_columns(table_name)
        }
        lineage_column = refreshed_columns.get("lineage_started_at")
        if lineage_column is None:
            raise RuntimeError(f"{table_name} 缺少 lineage_started_at")
        if engine.dialect.name == "postgresql" and bool(
            lineage_column.get("nullable", True)
        ):
            conn.exec_driver_sql(
                "ALTER TABLE scenario_model_draft_resources "
                "ALTER COLUMN lineage_started_at SET NOT NULL"
            )
        elif engine.dialect.name == "mysql":
            installed_type = lineage_column.get("type")
            compiled_type = (
                str(installed_type.compile(dialect=conn.dialect)).upper()
                if installed_type is not None
                else ""
            )
            if (
                bool(lineage_column.get("nullable", True))
                or compiled_type != f"DATETIME({MYSQL_DATETIME_PRECISION})"
                or lineage_column.get("default") is not None
            ):
                conn.exec_driver_sql(
                    "ALTER TABLE scenario_model_draft_resources "
                    f"MODIFY lineage_started_at DATETIME({MYSQL_DATETIME_PRECISION}) "
                    "NOT NULL"
                )
        inspector = inspect(conn)
        required_columns = {
            "id", "tenant_id", "scenario_id", "created_by_user_id",
            "source_thread_id", "source_message_id", "compilation_job_id",
            "proposal_id", "task_id", "resource_kind", "resource_key",
            "resource_identity", "title", "source_payload", "payload",
            "validation_issues", "source_refs", "materialization_source",
            "draft_status", "enabled", "publishable", "resolved_resource_id",
            "revision", "lineage_started_at", "predecessor_draft_id",
            "predecessor_revision", "superseded_by_proposal_id",
            "created_at", "updated_at",
        }
        columns = {
            column["name"] for column in inspector.get_columns(table_name)
        }
        missing = sorted(required_columns - columns)
        if missing:
            raise RuntimeError(
                f"{table_name} 缺少安全存储字段：{', '.join(missing)}"
            )

        unique_names = {
            item.get("name")
            for item in inspect(conn).get_unique_constraints(table_name)
        }
        index_names = {
            item.get("name") for item in inspect(conn).get_indexes(table_name)
        }
        if "uq_scenario_model_draft_resource_identity" not in unique_names | index_names:
            conn.exec_driver_sql(
                "CREATE UNIQUE INDEX uq_scenario_model_draft_resource_identity "
                "ON scenario_model_draft_resources "
                "(tenant_id, scenario_id, proposal_id, resource_identity)"
            )
        if "ix_scenario_model_drafts_scenario_status" not in index_names:
            conn.exec_driver_sql(
                "CREATE INDEX ix_scenario_model_drafts_scenario_status "
                "ON scenario_model_draft_resources "
                "(tenant_id, scenario_id, draft_status, updated_at)"
            )
        if "ix_scenario_model_drafts_lineage_started_at" not in index_names:
            conn.exec_driver_sql(
                "CREATE INDEX ix_scenario_model_drafts_lineage_started_at "
                "ON scenario_model_draft_resources (lineage_started_at)"
            )
        if "ix_scenario_model_drafts_predecessor" not in index_names:
            conn.exec_driver_sql(
                "CREATE INDEX ix_scenario_model_drafts_predecessor "
                "ON scenario_model_draft_resources "
                "(tenant_id, scenario_id, predecessor_draft_id)"
            )
        if "ix_scenario_model_drafts_superseded_by_proposal_id" not in index_names:
            conn.exec_driver_sql(
                "CREATE INDEX ix_scenario_model_drafts_superseded_by_proposal_id "
                "ON scenario_model_draft_resources (superseded_by_proposal_id)"
            )


def _migrate_property_default_json() -> None:
    """Make legacy VARCHAR defaults valid JSON before the ORM reads them."""
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("ontology_properties"):
            return
        columns = {column["name"] for column in inspector.get_columns("ontology_properties")}
        if "default_value" not in columns:
            return
        conn.exec_driver_sql(
            "UPDATE ontology_properties SET default_value = 'null' "
            "WHERE default_value IS NULL OR default_value = ''"
        )
        conn.exec_driver_sql(
            "UPDATE ontology_properties SET default_value = json_quote(default_value) "
            "WHERE json_valid(default_value) = 0"
        )


def _migrate_external_api_key_audit() -> None:
    """Add non-forged credential lifecycle actor fields to legacy databases.

    A historical key only contains its subject user, not the person who issued
    or revoked it.  Leaving new actor columns NULL is deliberate: assigning the
    subject as issuer would fabricate a governance record for owner-issued
    member credentials.  New lifecycle transitions create append-only audit
    events and persist both actors transactionally.
    """
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("external_api_keys"):
            return
        existing = {column["name"] for column in inspector.get_columns("external_api_keys")}
        for name in ("issued_by_user_id", "revoked_by_user_id"):
            if name not in existing:
                # Existing rows retain NULL/unknown actor information.  Basic
                # ALTER ADD COLUMN works on SQLite, PostgreSQL and MySQL.
                conn.exec_driver_sql(f"ALTER TABLE external_api_keys ADD COLUMN {name} VARCHAR(32)")
        refreshed = inspect(conn)
        index_names = {index["name"] for index in refreshed.get_indexes("external_api_keys")}
        for name, column in (
            ("ix_external_api_keys_issued_by_user_id", "issued_by_user_id"),
            ("ix_external_api_keys_revoked_by_user_id", "revoked_by_user_id"),
        ):
            if name not in index_names:
                try:
                    conn.exec_driver_sql(f"CREATE INDEX {name} ON external_api_keys ({column})")
                except Exception:  # noqa: BLE001
                    # A second web worker can win the inspect/create race during
                    # rolling startup.  The column is still safe and nullable;
                    # a pre-existing index is the desired final state.
                    pass

        # Existing keys carry a subject but never an authoritative issuer or
        # revoker.  Preserve actor columns as NULL and backfill one explicit,
        # append-only migration event instead of inventing a person.  New rows
        # always receive normal issued/revoked events in the same transaction
        # as their lifecycle mutation.
        if not inspector.has_table("external_api_key_audit_events"):
            return
        legacy_rows = conn.exec_driver_sql(
            "SELECT k.id, k.tenant_id, k.user_id, k.created_at "
            "FROM external_api_keys AS k "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM external_api_key_audit_events AS e "
            "WHERE e.api_key_id = k.id)"
        ).fetchall()
        for key_id, tenant_id, user_id, created_at in legacy_rows:
            # A corrupt pre-tenancy row cannot be attached to a trustworthy
            # tenant audit trail.  Leave it untouched/fail-closed rather than
            # generating a cross-tenant event during migration.
            if not tenant_id:
                continue
            conn.execute(
                text(
                    "INSERT INTO external_api_key_audit_events "
                    "(id, api_key_id, tenant_id, subject_user_id, actor_user_id, event_type, details, created_at) "
                    "VALUES (:id, :api_key_id, :tenant_id, :subject_user_id, NULL, :event_type, :details, :created_at)"
                ),
                {
                    "id": uuid.uuid4().hex,
                    "api_key_id": key_id,
                    "tenant_id": tenant_id,
                    "subject_user_id": user_id,
                    "event_type": "legacy_imported",
                    "details": json.dumps({"actor_provenance": "unknown_legacy"}),
                    "created_at": created_at or datetime.now(timezone.utc),
                },
            )


def _migrate_function_runtimes() -> None:
    """Add the safe, closed-list runtime descriptor to legacy function rows."""
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("function_definitions"):
            return
        existing = {column["name"] for column in inspector.get_columns("function_definitions")}
        if "runtime_kind" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE function_definitions ADD COLUMN runtime_kind VARCHAR(40) DEFAULT 'contract'"
            )
        if "runtime_config" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE function_definitions ADD COLUMN runtime_config JSON"
            )
        conn.exec_driver_sql(
            "UPDATE function_definitions SET runtime_kind = 'contract' "
            "WHERE runtime_kind IS NULL OR TRIM(runtime_kind) = ''"
        )
        conn.exec_driver_sql(
            "UPDATE function_definitions SET runtime_config = '{}' WHERE runtime_config IS NULL"
        )


def _migrate_agent_capability_scope() -> None:
    """Add the Agent capability contract without inventing legacy selections.

    Existing NULL rows remain identifiable for the UI, but runtime interprets
    them as explicit-empty. This prevents an upgrade from silently granting all
    current and future business capabilities.
    """
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("agents"):
            return
        existing = {column["name"] for column in inspector.get_columns("agents")}
        if "capability_scope" not in existing:
            conn.exec_driver_sql("ALTER TABLE agents ADD COLUMN capability_scope JSON")


def _add_column_ddl(conn, table_name: str, column_name: str, ddl: str) -> str:
    quote = conn.dialect.identifier_preparer.quote
    return (
        f"ALTER TABLE {quote(table_name)} ADD COLUMN {quote(column_name)} {ddl}"
    )


def _migrate_ontology_api_names() -> None:
    """Add and deterministically backfill stable ontology/link API metadata.

    ``create_all`` cannot add columns to an installed database.  Keep this
    migration idempotent and tolerant of partial/very old schemas so an upgrade
    can always reach the application-level repair tools.
    """

    from .services import ontology_service

    column_ddl = {
        "ontology_entities": {
            "api_name": "VARCHAR(100) NOT NULL DEFAULT ''",
        },
        "ontology_properties": {
            "api_name": "VARCHAR(100) NOT NULL DEFAULT ''",
        },
        "ontology_relations": {
            "api_name": "VARCHAR(100) NOT NULL DEFAULT ''",
            "source_display_name": "VARCHAR(200) NOT NULL DEFAULT ''",
            "source_api_name": "VARCHAR(100) NOT NULL DEFAULT ''",
            "target_display_name": "VARCHAR(200) NOT NULL DEFAULT ''",
            "target_api_name": "VARCHAR(100) NOT NULL DEFAULT ''",
            "storage_kind": "VARCHAR(32) NOT NULL DEFAULT 'none'",
        },
    }
    with engine.begin() as conn:
        inspector = inspect(conn)
        available_tables = set(inspector.get_table_names())
        for table_name, definitions in column_ddl.items():
            if table_name not in available_tables:
                continue
            existing = {
                column["name"] for column in inspect(conn).get_columns(table_name)
            }
            for column_name, ddl in definitions.items():
                if column_name not in existing:
                    conn.exec_driver_sql(
                        _add_column_ddl(conn, table_name, column_name, ddl)
                    )

        def migrated_name(
            used: set[str],
            current: object,
            *,
            display_name: object,
            prefix: str,
            stable_key: object,
        ) -> str:
            candidate = ontology_service.normalize_api_name(
                current,
                display_name=display_name,
                prefix=prefix,
                stable_key=stable_key,
            )
            return ontology_service.reserve_api_name(
                used,
                candidate,
                display_name=display_name,
                prefix=prefix,
                stable_key=stable_key,
                # Existing duplicates must be repaired, not abort startup.
                explicit=False,
            )

        if "ontology_entities" in available_tables:
            used_by_scenario: dict[str, set[str]] = {}
            rows = conn.execute(text(
                "SELECT id, scenario_id, name, api_name "
                "FROM ontology_entities ORDER BY scenario_id, id"
            )).mappings().all()
            for row in rows:
                used = used_by_scenario.setdefault(str(row["scenario_id"]), set())
                api_name = migrated_name(
                    used,
                    row["api_name"],
                    display_name=row["name"],
                    prefix="entity",
                    stable_key=row["id"],
                )
                if api_name != str(row["api_name"] or ""):
                    conn.execute(
                        text("UPDATE ontology_entities SET api_name=:api_name WHERE id=:id"),
                        {"api_name": api_name, "id": row["id"]},
                    )

        if "ontology_properties" in available_tables:
            used_by_entity: dict[str, set[str]] = {}
            rows = conn.execute(text(
                "SELECT id, entity_id, name, api_name "
                "FROM ontology_properties ORDER BY entity_id, id"
            )).mappings().all()
            for row in rows:
                used = used_by_entity.setdefault(str(row["entity_id"]), set())
                api_name = migrated_name(
                    used,
                    row["api_name"],
                    display_name=row["name"],
                    prefix="property",
                    stable_key=row["id"],
                )
                if api_name != str(row["api_name"] or ""):
                    conn.execute(
                        text("UPDATE ontology_properties SET api_name=:api_name WHERE id=:id"),
                        {"api_name": api_name, "id": row["id"]},
                    )

        if "ontology_relations" in available_tables:
            mapped_storage: dict[str, str] = {}
            if "relation_data_mappings" in available_tables:
                mapping_columns = {
                    column["name"]
                    for column in inspect(conn).get_columns("relation_data_mappings")
                }
                if {"relation_id", "mode"}.issubset(mapping_columns):
                    for mapping in conn.execute(text(
                        "SELECT relation_id, mode FROM relation_data_mappings"
                    )).mappings():
                        mode = str(mapping["mode"] or "")
                        mapped_storage[str(mapping["relation_id"])] = (
                            "join_table" if mode == "join_table" else "foreign_key"
                            if mode in {"source_fk", "target_fk"} else "none"
                        )
            used_by_scenario = {}
            rows = conn.execute(text(
                "SELECT id, scenario_id, name, api_name, source_display_name, "
                "source_api_name, target_display_name, target_api_name, storage_kind "
                "FROM ontology_relations ORDER BY scenario_id, id"
            )).mappings().all()
            for row in rows:
                used = used_by_scenario.setdefault(str(row["scenario_id"]), set())
                api_name = migrated_name(
                    used,
                    row["api_name"],
                    display_name=row["name"],
                    prefix="relation",
                    stable_key=row["id"],
                )
                current = dict(row)
                try:
                    navigation = ontology_service.normalize_relation_navigation(
                        relation_name=row["name"],
                        relation_api_name=api_name,
                        current=current,
                    )
                except ValueError:
                    # Repair a legacy pair that reused one identifier for both
                    # directions while retaining any useful display labels.
                    navigation = ontology_service.normalize_relation_navigation(
                        relation_name=row["name"],
                        relation_api_name=api_name,
                        source_display_name=row["source_display_name"],
                        source_api_name=row["source_api_name"],
                        target_display_name=row["target_display_name"],
                    )
                try:
                    storage_kind = ontology_service.normalize_relation_storage_kind(
                        row["storage_kind"]
                    )
                except ValueError:
                    storage_kind = "none"
                if storage_kind == "none":
                    storage_kind = mapped_storage.get(str(row["id"]), "none")
                values = {
                    "id": row["id"],
                    "api_name": api_name,
                    **navigation,
                    "storage_kind": storage_kind,
                }
                if any(
                    str(row[field] or "") != str(values[field] or "")
                    for field in (
                        "api_name", "source_display_name", "source_api_name",
                        "target_display_name", "target_api_name", "storage_kind",
                    )
                ):
                    conn.execute(
                        text(
                            "UPDATE ontology_relations SET api_name=:api_name, "
                            "source_display_name=:source_display_name, "
                            "source_api_name=:source_api_name, "
                            "target_display_name=:target_display_name, "
                            "target_api_name=:target_api_name, storage_kind=:storage_kind "
                            "WHERE id=:id"
                        ),
                        values,
                    )

        if _settings.database_url.startswith("sqlite"):
            for ddl in (
                "CREATE INDEX IF NOT EXISTS ix_ontology_entities_api_name "
                "ON ontology_entities (api_name)",
                "CREATE INDEX IF NOT EXISTS ix_ontology_properties_api_name "
                "ON ontology_properties (api_name)",
                "CREATE INDEX IF NOT EXISTS ix_ontology_relations_api_name "
                "ON ontology_relations (api_name)",
            ):
                table_name = ddl.split(" ON ", 1)[1].split(" ", 1)[0]
                if table_name in available_tables:
                    conn.exec_driver_sql(ddl)


def _migrate_ontology_entity_lifecycle() -> None:
    """Add the non-destructive Object Type lifecycle to installed databases.

    ``create_all`` only creates columns for new installations.  Existing rows
    predate lifecycle management and therefore retain their previous visible
    behaviour by being backfilled as ``active``.  The migration is deliberately
    idempotent and never deletes definitions or facts.
    """

    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("ontology_entities"):
            return
        existing = {
            column["name"] for column in inspector.get_columns("ontology_entities")
        }
        if "lifecycle_status" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE ontology_entities ADD COLUMN lifecycle_status "
                "VARCHAR(20) NOT NULL DEFAULT 'active'"
            )
        conn.exec_driver_sql(
            "UPDATE ontology_entities SET lifecycle_status = 'active' "
            "WHERE lifecycle_status IS NULL OR TRIM(lifecycle_status) = ''"
        )


def _migrate_artifact_template_catalog() -> None:
    """Catalog and immutably pin legacy file-id based template Actions.

    ``create_all`` installs the two catalog tables.  This data migration is
    deliberately separate and idempotent so existing deployments immediately
    gain managed AP001 and other template entries while the runtime retains a
    legacy fallback for any damaged file that cannot be inspected.
    """
    from .services import template_catalog_service

    with Session(bind=engine, autoflush=False, expire_on_commit=False) as db:
        try:
            template_catalog_service.migrate_legacy_template_actions(db)
            db.commit()
        except Exception:
            db.rollback()
            raise


def _nullable_orphan_repair_statement(
    child_table_name: str,
    child_column_name: str,
    parent_table_name: str,
):
    """Build a dialect-quoted orphan repair without interpolating SQL names."""
    child = table(child_table_name, column(child_column_name))
    parent = table(parent_table_name, column("id"))
    child_reference = child.c[child_column_name]
    parent_exists = exists(
        select(parent.c.id).where(parent.c.id == child_reference)
    )
    return (
        update(child)
        .where(child_reference.is_not(None), ~parent_exists)
        .values({child_column_name: None})
    )


def _repair_nullable_orphan_references() -> None:
    """Preserve legacy history while repairing nullable foreign-key links.

    Older processes and misconfigured test runs could write audit/trace rows
    through a connection that did not enforce SQLite foreign keys.  The parent
    resources may later disappear even though these relationships are declared
    ``ON DELETE SET NULL``.  Nulling only missing nullable references restores
    the schema invariant without deleting the historical record.
    """

    repairs = (
        ("assistant_audit_logs", "scenario_id", "business_scenarios"),
        ("assistant_audit_logs", "thread_id", "assistant_threads"),
        ("assistant_threads", "scenario_id", "business_scenarios"),
        ("llm_invocation_traces", "llm_config_id", "llm_configs"),
        ("llm_invocation_traces", "tenant_id", "tenants"),
    )
    with engine.begin() as conn:
        inspector = inspect(conn)
        available_tables = set(inspector.get_table_names())
        for child_table, child_column, parent_table in repairs:
            if child_table not in available_tables or parent_table not in available_tables:
                continue
            columns = {
                column["name"]: column
                for column in inspector.get_columns(child_table)
            }
            column = columns.get(child_column)
            if column is None or not bool(column.get("nullable", True)):
                continue
            conn.execute(
                _nullable_orphan_repair_statement(
                    child_table,
                    child_column,
                    parent_table,
                )
            )


def _verify_mysql_storage_engine(conn) -> None:
    """Reject mixed/non-transactional platform schemas before altering them."""
    if conn.dialect.name != "mysql":
        return

    configured_engine = str(
        conn.execute(text("SELECT @@SESSION.default_storage_engine")).scalar_one()
        or ""
    )
    if configured_engine.casefold() != "innodb":
        raise RuntimeError("MySQL 会话未能启用 InnoDB，已中止数据库初始化")

    platform_tables = set(Base.metadata.tables)
    installed = conn.execute(
        text(
            "SELECT table_name, engine FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'"
        )
    ).all()
    incompatible = sorted(
        str(table_name)
        for table_name, storage_engine in installed
        if str(table_name) in platform_tables
        and str(storage_engine or "").casefold() != "innodb"
    )
    if incompatible:
        raise RuntimeError(
            "检测到非 InnoDB 平台表，禁止在混合存储引擎上启动："
            + ", ".join(incompatible)
        )


def init_db() -> None:
    # Import every metadata module so direct maintenance/fixture callers get
    # the same schema as the ASGI application, which imports routers first.
    # The external integration credential model intentionally lives outside
    # ``models.py`` to keep browser and API-key auth boundaries explicit.
    from . import external_api_models, models  # noqa: F401

    ensure_runtime_directories(_settings)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            _verify_mysql_storage_engine(conn)
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - keep DSNs and credentials out of errors.
        raise RuntimeError(
            "平台数据库连接失败，请检查 DATABASE_URL、数据库服务和账号权限"
        ) from exc

    # PostgreSQL is the governed production control plane. Schema changes are
    # applied once by Alembic with a migration identity, never concurrently by
    # API workers holding the restricted runtime role.
    if engine.dialect.name == "postgresql":
        _verify_postgresql_schema_revision()
        _verify_schema()
        return

    Base.metadata.create_all(bind=engine)
    _migrate_ontology_api_names()
    _migrate_ontology_entity_lifecycle()
    _migrate_data_sources_nullable_scenario()
    _migrate_workflows_dag()
    _migrate_data_mapping_status()
    _migrate_data_mapping_runtime_bindings()
    _migrate_mapping_refresh_provenance()
    _migrate_action_safety()
    _migrate_workflow_lifecycle()
    _migrate_tenancy()
    _migrate_assistant_scopes()
    _migrate_assistant_thread_ownership()
    _migrate_conversation_ownership()
    _migrate_document_index()
    _migrate_bucket_storage_metadata()
    _migrate_document_index_active_key()
    _migrate_llm_management()
    _migrate_llm_trace_context()
    _migrate_message_citations()
    _migrate_permission_controls()
    _migrate_release_governance()
    _migrate_connector_governance()
    _migrate_mcp_name_identity()
    _migrate_workflow_run_environment()
    _migrate_workflow_run_execution_key()
    _migrate_runtime_definition_pins()
    _migrate_assistant_attachment_lifecycle()
    _migrate_assistant_compilation_jobs()
    _migrate_scenario_model_draft_resources()
    _migrate_property_default_json()
    _migrate_ontology_runtime_metadata()
    _migrate_action_decision_chain()
    _migrate_function_runtimes()
    _migrate_agent_capability_scope()
    _migrate_external_api_key_audit()
    _migrate_artifact_template_catalog()
    _repair_nullable_orphan_references()
    _migrate_mysql_datetime_precision()
    _verify_schema()


def _verify_postgresql_schema_revision() -> None:
    with engine.connect() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("alembic_version"):
            raise RuntimeError(
                "PostgreSQL 平台库尚未执行版本化迁移；请先运行 Alembic upgrade head"
            )
        revisions = {
            str(value)
            for value in conn.execute(
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
    """Fail startup if the installed schema is still incomplete.

    ``create_all`` adds missing tables but intentionally does not alter an
    existing table.  The explicit column check catches interrupted or
    manually-created deployments before requests can reach a partial schema.
    It only reads schema metadata and never changes application data.
    """
    with engine.connect() as conn:
        inspector = inspect(conn)
        missing_tables: list[str] = []
        missing_columns: dict[str, list[str]] = {}
        for table_name, table in Base.metadata.tables.items():
            physical_name = table.name
            if not inspector.has_table(physical_name, schema=table.schema):
                missing_tables.append(table_name)
                continue
            installed_columns = {
                column["name"]
                for column in inspector.get_columns(
                    physical_name,
                    schema=table.schema,
                )
            }
            missing = sorted(
                column.name
                for column in table.columns
                if column.name not in installed_columns
            )
            if missing:
                missing_columns[table_name] = missing

        if missing_tables or missing_columns:
            details = []
            if missing_tables:
                details.append(f"缺少表: {', '.join(sorted(missing_tables))}")
            if missing_columns:
                details.extend(
                    f"{table}: {', '.join(columns)}"
                    for table, columns in sorted(missing_columns.items())
                )
            raise RuntimeError(
                "平台数据库结构不完整，启动已停止，请检查迁移执行结果："
                + "; ".join(details)
            )
