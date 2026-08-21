"""SQLAlchemy engine / session management."""
from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timezone
import json
import uuid

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
            "data_source_binding_key": "VARCHAR(180) DEFAULT ''",
            "data_source_binding_ref": "JSON DEFAULT '{}'",
            "status": "VARCHAR(20) DEFAULT 'unknown'",
            "last_error": "TEXT DEFAULT ''",
            "last_checked_at": "DATETIME",
            "last_refreshed_at": "DATETIME",
            "last_row_count": "INTEGER DEFAULT 0",
            "last_imported_count": "INTEGER DEFAULT 0",
            "environment_status": "JSON DEFAULT '{}'",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE data_mappings ADD COLUMN {name} {definition}")


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
        columns = {
            "mapping_snapshot": "JSON",
            "definition_snapshot_id": "VARCHAR(32)",
            "release_id": "VARCHAR(32)",
            "definition_hash": "VARCHAR(64)",
            "definition_source": "VARCHAR(20)",
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
                "connector_audit": "JSON",
            }.items():
                if name not in log_columns:
                    conn.exec_driver_sql(
                        f"ALTER TABLE action_execution_logs ADD COLUMN {name} {definition}"
                    )
            conn.exec_driver_sql(
                "UPDATE action_execution_logs SET connector_audit = '[]' "
                "WHERE connector_audit IS NULL"
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
                owner = conn.exec_driver_sql(
                    "SELECT user_id FROM assistant_audit_logs "
                    "WHERE thread_id = :thread_id ORDER BY created_at ASC LIMIT 1",
                    {"thread_id": thread_id},
                ).fetchone()
                if owner and owner[0]:
                    conn.exec_driver_sql(
                        "UPDATE assistant_threads SET created_by_user_id = :user_id WHERE id = :thread_id",
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
            rows = conn.exec_driver_sql(
                "SELECT id FROM document_index_jobs WHERE tenant_id = :tenant_id "
                "AND active_key = :active_key ORDER BY created_at DESC, id DESC",
                {"tenant_id": tenant_id, "active_key": active_key},
            ).fetchall()
            for (job_id,) in rows[1:]:
                conn.exec_driver_sql(
                    "UPDATE document_index_jobs SET status = 'failed', active_key = NULL, "
                    "error = '已由较新的同文件索引任务替代' WHERE id = :job_id",
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
            conn.exec_driver_sql(
                "INSERT INTO external_api_key_audit_events "
                "(id, api_key_id, tenant_id, subject_user_id, actor_user_id, event_type, details, created_at) "
                "VALUES (:id, :api_key_id, :tenant_id, :subject_user_id, NULL, :event_type, :details, :created_at)",
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


def init_db() -> None:
    # Import every metadata module so direct maintenance/fixture callers get
    # the same schema as the ASGI application, which imports routers first.
    # The external integration credential model intentionally lives outside
    # ``models.py`` to keep browser and API-key auth boundaries explicit.
    from . import external_api_models, models  # noqa: F401

    Base.metadata.create_all(bind=engine)
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
    _migrate_document_index_active_key()
    _migrate_llm_management()
    _migrate_llm_trace_context()
    _migrate_message_citations()
    _migrate_permission_controls()
    _migrate_release_governance()
    _migrate_connector_governance()
    _migrate_workflow_run_environment()
    _migrate_workflow_run_execution_key()
    _migrate_runtime_definition_pins()
    _migrate_external_api_key_audit()
