"""SQLAlchemy engine / session management."""
from __future__ import annotations

from collections.abc import Generator

from fastapi import Request
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
connect_args = {"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {}
engine = create_engine(_settings.database_url, connect_args=connect_args, pool_pre_ping=True)
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
    with engine.begin() as conn:
        row = conn.exec_driver_sql(
            'SELECT "notnull" FROM pragma_table_info(\'data_sources\') WHERE name = \'scenario_id\''
        ).fetchone()
        if not row or not row[0]:
            return  # 已是可空（或表不存在），无需迁移
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql(
            """
            CREATE TABLE data_sources_new (
                id VARCHAR(32) NOT NULL,
                scenario_id VARCHAR(32),
                name VARCHAR(200) NOT NULL,
                type VARCHAR(30) NOT NULL,
                config JSON,
                status VARCHAR(20),
                last_error TEXT,
                created_at DATETIME,
                PRIMARY KEY (id),
                FOREIGN KEY(scenario_id) REFERENCES business_scenarios (id) ON DELETE CASCADE
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO data_sources_new SELECT id, scenario_id, name, type, config, status, last_error, created_at FROM data_sources"
        )
        conn.exec_driver_sql("DROP TABLE data_sources")
        conn.exec_driver_sql("ALTER TABLE data_sources_new RENAME TO data_sources")
        conn.exec_driver_sql("CREATE INDEX ix_data_sources_scenario_id ON data_sources (scenario_id)")
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")


def _migrate_workflows_dag() -> None:
    """SQLite 的 create_all 不会给已有表加列：为 ontology_workflows 补 nodes/edges 列。"""
    if not _settings.database_url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        cols = [
            r[1]
            for r in conn.exec_driver_sql("PRAGMA table_info('ontology_workflows')").fetchall()
        ]
        if not cols:
            return
        if "nodes" not in cols:
            conn.exec_driver_sql("ALTER TABLE ontology_workflows ADD COLUMN nodes JSON DEFAULT '[]'")
        if "edges" not in cols:
            conn.exec_driver_sql("ALTER TABLE ontology_workflows ADD COLUMN edges JSON DEFAULT '[]'")


def _migrate_data_mapping_status() -> None:
    """为已有数据映射补充检查、刷新和错误状态字段。"""
    if not _settings.database_url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        existing = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info('data_mappings')").fetchall()
        }
        if not existing:
            return
        columns = {
            "status": "VARCHAR(20) DEFAULT 'unknown'",
            "last_error": "TEXT DEFAULT ''",
            "last_checked_at": "DATETIME",
            "last_refreshed_at": "DATETIME",
            "last_row_count": "INTEGER DEFAULT 0",
            "last_imported_count": "INTEGER DEFAULT 0",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE data_mappings ADD COLUMN {name} {definition}")


def _migrate_action_safety() -> None:
    """为已有 Action 和执行日志补充确认、幂等和执行模式字段。"""
    if not _settings.database_url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        action_columns = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info('ontology_actions')").fetchall()
        }
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

        log_columns = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info('action_execution_logs')").fetchall()
        }
        if log_columns:
            for name, definition in {
                "mode": "VARCHAR(20) DEFAULT 'execute'",
                "idempotency_key": "VARCHAR(120)",
            }.items():
                if name not in log_columns:
                    conn.exec_driver_sql(
                        f"ALTER TABLE action_execution_logs ADD COLUMN {name} {definition}"
                    )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_action_execution_logs_idempotency_key "
                "ON action_execution_logs (idempotency_key)"
            )
            conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_action_execution_logs_idempotency "
                "ON action_execution_logs (scenario_id, target_type, target_id, idempotency_key)"
            )


def _migrate_workflow_lifecycle() -> None:
    """为已有工作流补充草稿/启用/停用生命周期状态。"""
    if not _settings.database_url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        existing = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info('ontology_workflows')").fetchall()
        }
        if not existing or "status" in existing:
            return
        # 旧版本只有 enabled 字段，迁移为可执行的 active，避免升级后已有流程突然无法运行。
        conn.exec_driver_sql(
            "ALTER TABLE ontology_workflows ADD COLUMN status VARCHAR(20) DEFAULT 'active'"
        )


def _migrate_tenancy() -> None:
    """为已有平台表补充租户列；旧数据在首个用户注册时认领。"""
    if not _settings.database_url.startswith("sqlite"):
        return
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
        for table, columns in columns_by_table.items():
            existing = {
                row[1]
                for row in conn.exec_driver_sql(f"PRAGMA table_info('{table}')").fetchall()
            }
            if not existing:
                continue
            for name, definition in columns.items():
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _migrate_assistant_scopes() -> None:
    """为已有助手会话补充上下文范围；旧消息可用最近一条消息的路径回填。"""
    if not _settings.database_url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        existing = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info('assistant_threads')").fetchall()
        }
        if not existing:
            return
        if "scope_key" not in existing:
            conn.exec_driver_sql("ALTER TABLE assistant_threads ADD COLUMN scope_key VARCHAR(700) DEFAULT 'global'")
        # 仅回填旧版本的 global 值；新会话会在创建时写入准确范围。
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
        message_columns = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info('assistant_messages')").fetchall()
        }
        if message_columns and "thinking" not in message_columns:
            conn.exec_driver_sql("ALTER TABLE assistant_messages ADD COLUMN thinking JSON DEFAULT '[]'")


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
        conn.exec_driver_sql("UPDATE bucket_files SET chunk_count = 0 WHERE chunk_count IS NULL")


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
        conn.exec_driver_sql("UPDATE messages SET citations = '[]' WHERE citations IS NULL")


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


def init_db() -> None:
    # Import models so they register on the metadata before create_all.
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate_data_sources_nullable_scenario()
    _migrate_workflows_dag()
    _migrate_data_mapping_status()
    _migrate_action_safety()
    _migrate_workflow_lifecycle()
    _migrate_tenancy()
    _migrate_assistant_scopes()
    _migrate_document_index()
    _migrate_llm_management()
    _migrate_message_citations()
    _migrate_permission_controls()
