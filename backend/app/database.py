"""PostgreSQL engine and session management."""
from __future__ import annotations

import hashlib
from collections.abc import Generator
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import Request
from sqlalchemy import (
    DateTime as SQLAlchemyDateTime,
    MetaData,
    Table,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import ensure_runtime_directories, get_settings


class Base(DeclarativeBase):
    pass


def orm_datetime(*, timezone: bool = True):
    """Return the PostgreSQL timestamp type used by all ORM models."""
    return SQLAlchemyDateTime(timezone=timezone)


POSTGRESQL_SCHEMA_REVISION = "20260831_17"

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


def _require_sqlite_legacy_helper(helper_name: str) -> None:
    """Keep embedded-database repair helpers outside PostgreSQL governance."""

    if engine.dialect.name != "sqlite":
        raise RuntimeError(
            f"{helper_name} 仅用于 SQLite 旧库回归；PostgreSQL 必须通过 Alembic 迁移"
        )


def _quoted_identifier(connection, identifier: str) -> str:
    """Quote a migration identifier with the active SQL dialect."""

    return connection.dialect.identifier_preparer.quote(identifier)


def _migrate_agent_capability_scope() -> None:
    """Idempotent compatibility helper for pre-Alembic local databases.

    Production startup never calls this function; PostgreSQL remains governed
    exclusively by Alembic.  It is retained for old embedded databases and
    migration regression tests so adding the nullable column cannot grant a
    legacy Agent capabilities or rewrite existing rows.
    """
    with engine.begin() as connection:
        inspector = inspect(connection)
        if not inspector.has_table("agents"):
            return
        columns = {item["name"] for item in inspector.get_columns("agents")}
        if "capability_scope" in columns:
            return
        json_type = "JSONB" if connection.dialect.name == "postgresql" else "JSON"
        connection.exec_driver_sql(
            f"ALTER TABLE agents ADD COLUMN capability_scope {json_type} NULL"
        )


def _migrate_mapping_refresh_provenance() -> None:
    """Fail closed for pre-freeze embedded mapping refresh jobs.

    Production PostgreSQL remains Alembic-only. This compatibility helper
    preserves terminal history, but cancels active legacy work because it
    cannot prove the frozen mapping, relation, or release that authorized its
    external read.
    """

    table_name = "data_mapping_refresh_jobs"
    with engine.begin() as connection:
        inspector = inspect(connection)
        if not inspector.has_table(table_name):
            return
        existing = {
            item["name"] for item in inspector.get_columns(table_name)
        }
        is_legacy = "mapping_snapshot" not in existing
        lacks_relation_fingerprint = "relation_mapping_fingerprint" not in existing
        json_type = "JSONB" if connection.dialect.name == "postgresql" else "JSON"
        definitions = {
            "mapping_snapshot": json_type,
            "definition_snapshot_id": "VARCHAR(32)",
            "release_id": "VARCHAR(32)",
            "definition_hash": "VARCHAR(64)",
            "definition_source": "VARCHAR(20)",
            "relation_mapping_fingerprint": "VARCHAR(64)",
        }
        for name, definition in definitions.items():
            if name not in existing:
                connection.exec_driver_sql(
                    f"ALTER TABLE {table_name} ADD COLUMN {name} {definition}"
                )

        empty_json = "'{}'::jsonb" if connection.dialect.name == "postgresql" else "'{}'"
        connection.exec_driver_sql(
            f"UPDATE {table_name} SET mapping_snapshot = {empty_json} "
            "WHERE mapping_snapshot IS NULL"
        )
        connection.exec_driver_sql(
            f"UPDATE {table_name} SET definition_hash = '' "
            "WHERE definition_hash IS NULL"
        )
        connection.exec_driver_sql(
            f"UPDATE {table_name} SET definition_source = 'live' "
            "WHERE definition_source IS NULL OR TRIM(definition_source) = ''"
        )
        connection.exec_driver_sql(
            f"UPDATE {table_name} SET relation_mapping_fingerprint = '' "
            "WHERE relation_mapping_fingerprint IS NULL"
        )
        terminal = "'succeeded', 'failed', 'timed_out', 'cancelled'"
        if is_legacy:
            connection.exec_driver_sql(
                f"UPDATE {table_name} SET status = 'cancelled', "
                "error = '映射刷新定义快照缺失，部署升级后已安全取消，请重新提交', "
                "active_key = NULL, next_retry_at = NULL, "
                "completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP) "
                f"WHERE status NOT IN ({terminal})"
            )
            connection.exec_driver_sql(
                f"UPDATE {table_name} SET definition_source = 'legacy'"
            )
        elif lacks_relation_fingerprint:
            connection.exec_driver_sql(
                f"UPDATE {table_name} SET status = 'cancelled', "
                "error = '关系映射定义指纹缺失，部署升级后已安全取消，请重新提交', "
                "active_key = NULL, next_retry_at = NULL, "
                "completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP) "
                f"WHERE status NOT IN ({terminal})"
            )
        connection.exec_driver_sql(
            f"UPDATE {table_name} SET status = 'cancelled', "
            "error = '开发环境定义指纹缺失，部署升级后已安全取消，请重新提交', "
            "active_key = NULL, next_retry_at = NULL, "
            "completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP) "
            f"WHERE status NOT IN ({terminal}) AND definition_source = 'live' "
            "AND (definition_hash IS NULL OR TRIM(definition_hash) = '')"
        )

        indexes = {
            item.get("name")
            for item in inspect(connection).get_indexes(table_name)
        }
        index_definitions = {
            "ix_mapping_refresh_jobs_release": "release_id, definition_snapshot_id",
            "ix_mapping_refresh_jobs_definition_snapshot_id": "definition_snapshot_id",
        }
        for name, columns in index_definitions.items():
            if name not in indexes:
                connection.exec_driver_sql(
                    f"CREATE INDEX {name} ON {table_name} ({columns})"
                )


def _migrate_ontology_entity_lifecycle() -> None:
    """Backfill the non-destructive entity lifecycle in embedded stores."""

    table_name = "ontology_entities"
    with engine.begin() as connection:
        inspector = inspect(connection)
        if not inspector.has_table(table_name):
            return
        columns = {
            item["name"] for item in inspector.get_columns(table_name)
        }
        if "lifecycle_status" not in columns:
            connection.exec_driver_sql(
                f"ALTER TABLE {table_name} ADD COLUMN lifecycle_status "
                "VARCHAR(20) NOT NULL DEFAULT 'active'"
            )
        connection.exec_driver_sql(
            f"UPDATE {table_name} SET lifecycle_status = 'active' "
            "WHERE lifecycle_status IS NULL OR TRIM(lifecycle_status) = ''"
        )


def _migrate_external_api_key_audit() -> None:
    """Idempotently preserve unknown actors in pre-Alembic API-key stores.

    Production startup does not call this helper.  It remains available for
    old embedded installations and migration regressions: missing governance
    actor columns are added as nullable values, and an append-only import event
    records the legacy subject without inventing an issuer or revoker.
    """
    with engine.begin() as connection:
        inspector = inspect(connection)
        if not inspector.has_table("external_api_keys"):
            return
        key_columns = {
            item["name"] for item in inspector.get_columns("external_api_keys")
        }
        for column in ("issued_by_user_id", "revoked_by_user_id"):
            if column not in key_columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE external_api_keys ADD COLUMN {column} VARCHAR(32) NULL"
                )

        inspector = inspect(connection)
        if not inspector.has_table("external_api_key_audit_events"):
            return
        event_columns = {
            item["name"]
            for item in inspector.get_columns("external_api_key_audit_events")
        }
        for column in ("subject_user_id", "actor_user_id"):
            if column not in event_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE external_api_key_audit_events "
                    f"ADD COLUMN {column} VARCHAR(32) NULL"
                )

        rows = connection.execute(
            text(
                "SELECT k.id, k.tenant_id, k.user_id, k.created_at "
                "FROM external_api_keys AS k "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM external_api_key_audit_events AS e "
                "WHERE e.api_key_id = k.id"
                ")"
            )
        ).mappings()
        for row in rows:
            connection.execute(
                text(
                    "INSERT INTO external_api_key_audit_events "
                    "(id, api_key_id, tenant_id, subject_user_id, actor_user_id, "
                    "event_type, details, created_at) "
                    "VALUES (:id, :api_key_id, :tenant_id, :subject_user_id, NULL, "
                    ":event_type, :details, :created_at)"
                ),
                {
                    "id": uuid4().hex,
                    "api_key_id": row["id"],
                    "tenant_id": row["tenant_id"],
                    "subject_user_id": row["user_id"],
                    "event_type": "legacy_imported",
                    "details": "{}",
                    "created_at": row["created_at"] or datetime.now(timezone.utc),
                },
            )


def _migrate_conversation_ownership() -> None:
    """Add legacy conversation ownership metadata without inventing an owner.

    Production startup does not call this helper. It is retained for migration
    regressions and old embedded databases where a pre-ownership transcript has
    no trustworthy creator signal and must therefore remain inaccessible.
    """
    with engine.begin() as connection:
        inspector = inspect(connection)
        if not inspector.has_table("conversations"):
            return
        columns = {
            item["name"] for item in inspector.get_columns("conversations")
        }
        if "created_by_user_id" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE conversations "
                "ADD COLUMN created_by_user_id VARCHAR(32) NULL"
            )
        index_names = {
            item["name"] for item in inspect(connection).get_indexes("conversations")
        }
        if "ix_conversations_created_by_user_id" not in index_names:
            connection.exec_driver_sql(
                "CREATE INDEX ix_conversations_created_by_user_id "
                "ON conversations (created_by_user_id)"
            )


def _migrate_scenario_model_draft_resources() -> None:
    """Verify the inert candidate store for legacy embedded databases.

    PostgreSQL production schema changes remain Alembic-only. This helper is
    deliberately limited to adding provenance columns and indexes; it does not
    activate, publish, or otherwise reinterpret an existing candidate.
    """
    table_name = "scenario_model_draft_resources"
    with engine.begin() as connection:
        inspector = inspect(connection)
        if not inspector.has_table(table_name):
            raise RuntimeError(f"{table_name} 表未创建")
        columns = {
            item["name"] for item in inspector.get_columns(table_name)
        }
        timestamp_type = (
            "TIMESTAMP WITH TIME ZONE"
            if connection.dialect.name == "postgresql"
            else "DATETIME"
        )
        lineage_columns = {
            "lineage_started_at": timestamp_type,
            "predecessor_draft_id": "VARCHAR(32) NOT NULL DEFAULT ''",
            "predecessor_revision": "INTEGER NOT NULL DEFAULT -1",
            "superseded_by_proposal_id": "VARCHAR(64) NOT NULL DEFAULT ''",
        }
        for name, definition in lineage_columns.items():
            if name not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE {table_name} ADD COLUMN {name} {definition}"
                )
        connection.exec_driver_sql(
            "UPDATE scenario_model_draft_resources "
            "SET lineage_started_at = COALESCE(lineage_started_at, created_at)"
        )

        required_columns = {
            "id",
            "tenant_id",
            "scenario_id",
            "created_by_user_id",
            "source_thread_id",
            "source_message_id",
            "compilation_job_id",
            "proposal_id",
            "task_id",
            "resource_kind",
            "resource_key",
            "resource_identity",
            "title",
            "source_payload",
            "payload",
            "validation_issues",
            "source_refs",
            "materialization_source",
            "draft_status",
            "enabled",
            "publishable",
            "resolved_resource_id",
            "revision",
            "lineage_started_at",
            "predecessor_draft_id",
            "predecessor_revision",
            "superseded_by_proposal_id",
            "created_at",
            "updated_at",
        }
        installed_columns = {
            item["name"] for item in inspect(connection).get_columns(table_name)
        }
        missing = sorted(required_columns - installed_columns)
        if missing:
            raise RuntimeError(
                f"{table_name} 缺少安全存储字段：{', '.join(missing)}"
            )

        unique_names = {
            item.get("name")
            for item in inspect(connection).get_unique_constraints(table_name)
        }
        index_names = {
            item.get("name")
            for item in inspect(connection).get_indexes(table_name)
        }
        indexes = {
            "uq_scenario_model_draft_resource_identity": (
                "CREATE UNIQUE INDEX uq_scenario_model_draft_resource_identity "
                "ON scenario_model_draft_resources "
                "(tenant_id, scenario_id, proposal_id, resource_identity)"
            ),
            "ix_scenario_model_drafts_scenario_status": (
                "CREATE INDEX ix_scenario_model_drafts_scenario_status "
                "ON scenario_model_draft_resources "
                "(tenant_id, scenario_id, draft_status, updated_at)"
            ),
            "ix_scenario_model_drafts_lineage_started_at": (
                "CREATE INDEX ix_scenario_model_drafts_lineage_started_at "
                "ON scenario_model_draft_resources (lineage_started_at)"
            ),
            "ix_scenario_model_drafts_predecessor": (
                "CREATE INDEX ix_scenario_model_drafts_predecessor "
                "ON scenario_model_draft_resources "
                "(tenant_id, scenario_id, predecessor_draft_id)"
            ),
            "ix_scenario_model_drafts_superseded_by_proposal_id": (
                "CREATE INDEX ix_scenario_model_drafts_superseded_by_proposal_id "
                "ON scenario_model_draft_resources (superseded_by_proposal_id)"
            ),
        }
        existing_names = unique_names | index_names
        for name, statement in indexes.items():
            if name not in existing_names:
                connection.exec_driver_sql(statement)


def _migrate_mcp_name_identity() -> None:
    """Install the normalized, tenant-scoped MCP name identity in SQLite.

    This is retained solely to exercise upgrades from the former embedded
    schema. Duplicate legacy identities abort the transaction and report the
    original row ids; the helper never renames or removes an MCP definition.
    """

    _require_sqlite_legacy_helper("_migrate_mcp_name_identity")
    from .models import normalize_mcp_name_key

    table_name = "mcp_configs"
    with engine.begin() as connection:
        inspector = inspect(connection)
        if not inspector.has_table(table_name):
            return
        columns = {
            item["name"] for item in inspector.get_columns(table_name)
        }
        required = {"id", "tenant_id", "name"}
        if not required.issubset(columns):
            missing = ", ".join(sorted(required - columns))
            raise RuntimeError(f"mcp_configs 旧库缺少身份字段：{missing}")

        table_sql = _quoted_identifier(connection, table_name)
        name_key_sql = _quoted_identifier(connection, "name_key")
        if "name_key" not in columns:
            connection.exec_driver_sql(
                f"ALTER TABLE {table_sql} ADD COLUMN {name_key_sql} VARCHAR(600)"
            )

        rows = connection.execute(
            text(
                "SELECT id, tenant_id, name FROM mcp_configs "
                "ORDER BY tenant_id, id"
            )
        ).mappings().all()
        identities: dict[tuple[str, str], list[str]] = {}
        updates: list[dict[str, str]] = []
        for row in rows:
            row_id = str(row["id"])
            tenant_id = str(row["tenant_id"] or "")
            name_key = normalize_mcp_name_key(str(row["name"] or ""))
            if tenant_id:
                identities.setdefault((tenant_id, name_key), []).append(row_id)
            updates.append({"id": row_id, "name_key": name_key})

        duplicate = next(
            (
                (tenant_id, name_key, ids)
                for (tenant_id, name_key), ids in identities.items()
                if len(ids) > 1
            ),
            None,
        )
        if duplicate is not None:
            tenant_id, name_key, ids = duplicate
            raise RuntimeError(
                "MCP 名称规范化后存在租户内重复，无法安全建立唯一约束："
                f"tenant={tenant_id}, name_key={name_key!r}, ids={','.join(ids)}"
            )
        if updates:
            connection.execute(
                text("UPDATE mcp_configs SET name_key=:name_key WHERE id=:id"),
                updates,
            )

        refreshed = inspect(connection)
        identity_names = {
            item.get("name")
            for item in refreshed.get_unique_constraints(table_name)
        } | {
            item.get("name")
            for item in refreshed.get_indexes(table_name)
            if item.get("unique")
        }
        index_name = "uq_mcp_configs_tenant_name_key"
        if index_name not in identity_names:
            index_sql = _quoted_identifier(connection, index_name)
            tenant_sql = _quoted_identifier(connection, "tenant_id")
            connection.exec_driver_sql(
                f"CREATE UNIQUE INDEX {index_sql} ON {table_sql} "
                f"({tenant_sql}, {name_key_sql})"
            )


def _migrate_ontology_api_names() -> None:
    """Deterministically backfill stable ontology API names in SQLite only."""

    _require_sqlite_legacy_helper("_migrate_ontology_api_names")
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
    with engine.begin() as connection:
        inspector = inspect(connection)
        available_tables = set(inspector.get_table_names())
        for table_name, definitions in column_ddl.items():
            if table_name not in available_tables:
                continue
            existing = {
                item["name"]
                for item in inspect(connection).get_columns(table_name)
            }
            table_sql = _quoted_identifier(connection, table_name)
            for column_name, definition in definitions.items():
                if column_name not in existing:
                    column_sql = _quoted_identifier(connection, column_name)
                    connection.exec_driver_sql(
                        f"ALTER TABLE {table_sql} ADD COLUMN "
                        f"{column_sql} {definition}"
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
                explicit=False,
            )

        if "ontology_entities" in available_tables:
            used_by_scenario: dict[str, set[str]] = {}
            rows = connection.execute(text(
                "SELECT id, scenario_id, name, api_name "
                "FROM ontology_entities ORDER BY scenario_id, id"
            )).mappings().all()
            for row in rows:
                used = used_by_scenario.setdefault(
                    str(row["scenario_id"]), set()
                )
                api_name = migrated_name(
                    used,
                    row["api_name"],
                    display_name=row["name"],
                    prefix="entity",
                    stable_key=row["id"],
                )
                if api_name != str(row["api_name"] or ""):
                    connection.execute(
                        text(
                            "UPDATE ontology_entities SET api_name=:api_name "
                            "WHERE id=:id"
                        ),
                        {"api_name": api_name, "id": row["id"]},
                    )

        if "ontology_properties" in available_tables:
            used_by_entity: dict[str, set[str]] = {}
            rows = connection.execute(text(
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
                    connection.execute(
                        text(
                            "UPDATE ontology_properties SET api_name=:api_name "
                            "WHERE id=:id"
                        ),
                        {"api_name": api_name, "id": row["id"]},
                    )

        if "ontology_relations" in available_tables:
            mapped_storage: dict[str, str] = {}
            if "relation_data_mappings" in available_tables:
                mapping_columns = {
                    item["name"]
                    for item in inspect(connection).get_columns(
                        "relation_data_mappings"
                    )
                }
                if {"relation_id", "mode"}.issubset(mapping_columns):
                    mappings = connection.execute(text(
                        "SELECT relation_id, mode FROM relation_data_mappings"
                    )).mappings()
                    for mapping in mappings:
                        mode = str(mapping["mode"] or "")
                        mapped_storage[str(mapping["relation_id"])] = (
                            "join_table"
                            if mode == "join_table"
                            else "foreign_key"
                            if mode in {"source_fk", "target_fk"}
                            else "none"
                        )

            used_by_scenario = {}
            rows = connection.execute(text(
                "SELECT id, scenario_id, name, api_name, "
                "source_display_name, source_api_name, target_display_name, "
                "target_api_name, storage_kind FROM ontology_relations "
                "ORDER BY scenario_id, id"
            )).mappings().all()
            for row in rows:
                used = used_by_scenario.setdefault(
                    str(row["scenario_id"]), set()
                )
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
                    navigation = ontology_service.normalize_relation_navigation(
                        relation_name=row["name"],
                        relation_api_name=api_name,
                        source_display_name=row["source_display_name"],
                        source_api_name=row["source_api_name"],
                        target_display_name=row["target_display_name"],
                    )
                try:
                    storage_kind = (
                        ontology_service.normalize_relation_storage_kind(
                            row["storage_kind"]
                        )
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
                fields = (
                    "api_name",
                    "source_display_name",
                    "source_api_name",
                    "target_display_name",
                    "target_api_name",
                    "storage_kind",
                )
                if any(
                    str(row[field] or "") != str(values[field] or "")
                    for field in fields
                ):
                    connection.execute(
                        text(
                            "UPDATE ontology_relations SET "
                            "api_name=:api_name, "
                            "source_display_name=:source_display_name, "
                            "source_api_name=:source_api_name, "
                            "target_display_name=:target_display_name, "
                            "target_api_name=:target_api_name, "
                            "storage_kind=:storage_kind WHERE id=:id"
                        ),
                        values,
                    )

        api_indexes = {
            "ontology_entities": "ix_ontology_entities_api_name",
            "ontology_properties": "ix_ontology_properties_api_name",
            "ontology_relations": "ix_ontology_relations_api_name",
        }
        for table_name, index_name in api_indexes.items():
            if table_name not in available_tables:
                continue
            existing_indexes = {
                item.get("name")
                for item in inspect(connection).get_indexes(table_name)
            }
            if index_name not in existing_indexes:
                connection.exec_driver_sql(
                    f"CREATE INDEX {_quoted_identifier(connection, index_name)} "
                    f"ON {_quoted_identifier(connection, table_name)} "
                    f"({_quoted_identifier(connection, 'api_name')})"
                )


def _migrate_ontology_runtime_metadata() -> None:
    """Backfill generic ontology runtime metadata in legacy SQLite stores."""

    _require_sqlite_legacy_helper("_migrate_ontology_runtime_metadata")
    columns_by_table = {
        "business_scenarios": {"namespace": "VARCHAR(180)"},
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
        "data_mappings": {"transform_rules": "JSON"},
    }
    with engine.begin() as connection:
        inspector = inspect(connection)
        available_tables = set(inspector.get_table_names())
        for table_name, definitions in columns_by_table.items():
            if table_name not in available_tables:
                continue
            installed = {
                item["name"] for item in inspector.get_columns(table_name)
            }
            table_sql = _quoted_identifier(connection, table_name)
            for column_name, definition in definitions.items():
                if column_name not in installed:
                    connection.exec_driver_sql(
                        f"ALTER TABLE {table_sql} ADD COLUMN "
                        f"{_quoted_identifier(connection, column_name)} {definition}"
                    )

        scalar_defaults = (
            ("business_scenarios", "namespace", "default"),
            ("ontology_entities", "namespace", "default"),
            ("ontology_entities", "state_property", ""),
            ("ontology_relations", "namespace", "default"),
            ("ontology_instances", "state", ""),
            ("relation_instances", "source", "manual"),
            ("relation_instances", "source_ref", ""),
        )
        for table_name, column_name, value in scalar_defaults:
            if table_name not in available_tables:
                continue
            table_sql = _quoted_identifier(connection, table_name)
            column_sql = _quoted_identifier(connection, column_name)
            connection.execute(
                text(
                    f"UPDATE {table_sql} SET {column_sql} = :value "
                    f"WHERE {column_sql} IS NULL OR TRIM({column_sql}) = ''"
                ),
                {"value": value},
            )

        json_defaults = (
            ("ontology_properties", "constraints"),
            ("ontology_relations", "constraints"),
            ("ontology_instances", "quality"),
            ("relation_instances", "source_metadata"),
            ("data_mappings", "transform_rules"),
        )
        for table_name, column_name in json_defaults:
            if table_name not in available_tables:
                continue
            table_sql = _quoted_identifier(connection, table_name)
            column_sql = _quoted_identifier(connection, column_name)
            connection.exec_driver_sql(
                f"UPDATE {table_sql} SET {column_sql} = '{{}}' "
                f"WHERE {column_sql} IS NULL"
            )

        if "ontology_properties" in available_tables:
            property_columns = {
                item["name"]
                for item in inspect(connection).get_columns(
                    "ontology_properties"
                )
            }
            if "is_title" in property_columns:
                connection.execute(
                    text(
                        "UPDATE ontology_properties SET is_title=:false_value "
                        "WHERE is_title IS NULL"
                    ),
                    {"false_value": False},
                )
            if {"id", "entity_id", "is_key", "is_title"}.issubset(
                property_columns
            ):
                # Retain the lexicographically first legacy key as title only
                # when that entity has no explicit title already.
                connection.execute(
                    text(
                        "UPDATE ontology_properties SET is_title=:true_value "
                        "WHERE id IN ("
                        "SELECT MIN(candidate.id) "
                        "FROM ontology_properties AS candidate "
                        "WHERE candidate.is_key=:true_value AND NOT EXISTS ("
                        "SELECT 1 FROM ontology_properties AS titled "
                        "WHERE titled.entity_id=candidate.entity_id "
                        "AND titled.is_title=:true_value"
                        ") GROUP BY candidate.entity_id)"
                    ),
                    {"true_value": True},
                )

        if "ontology_instances" in available_tables:
            index_name = "ix_ontology_instances_state"
            indexes = {
                item.get("name")
                for item in inspect(connection).get_indexes(
                    "ontology_instances"
                )
            }
            if index_name not in indexes:
                connection.exec_driver_sql(
                    f"CREATE INDEX {_quoted_identifier(connection, index_name)} "
                    "ON ontology_instances (state)"
                )

        if "relation_instances" in available_tables:
            relation_columns = {
                item["name"]
                for item in inspect(connection).get_columns(
                    "relation_instances"
                )
            }
            edge_columns = {
                "id",
                "relation_id",
                "source_instance_id",
                "target_instance_id",
            }
            if edge_columns.issubset(relation_columns):
                rows = connection.execute(text(
                    "SELECT id, relation_id, source_instance_id, "
                    "target_instance_id FROM relation_instances ORDER BY id"
                )).mappings().all()
                seen: set[tuple[str, str, str]] = set()
                duplicate_ids: list[object] = []
                for row in rows:
                    identity = (
                        str(row["relation_id"]),
                        str(row["source_instance_id"]),
                        str(row["target_instance_id"]),
                    )
                    if identity in seen:
                        duplicate_ids.append(row["id"])
                    else:
                        seen.add(identity)
                if duplicate_ids:
                    connection.execute(
                        text("DELETE FROM relation_instances WHERE id=:id"),
                        [{"id": item} for item in duplicate_ids],
                    )

                refreshed = inspect(connection)
                guards = {
                    item.get("name")
                    for item in refreshed.get_unique_constraints(
                        "relation_instances"
                    )
                } | {
                    item.get("name")
                    for item in refreshed.get_indexes("relation_instances")
                    if item.get("unique")
                }
                index_name = "uq_relation_instances_edge"
                if index_name not in guards:
                    connection.exec_driver_sql(
                        f"CREATE UNIQUE INDEX "
                        f"{_quoted_identifier(connection, index_name)} "
                        "ON relation_instances "
                        "(relation_id, source_instance_id, target_instance_id)"
                    )


def _migrate_action_decision_chain() -> None:
    """Add non-forged Action decision provenance to legacy SQLite logs."""

    _require_sqlite_legacy_helper("_migrate_action_decision_chain")
    table_name = "action_execution_logs"
    with engine.begin() as connection:
        inspector = inspect(connection)
        if not inspector.has_table(table_name):
            return
        existing = {
            item["name"] for item in inspector.get_columns(table_name)
        }
        definitions = {
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
        table_sql = _quoted_identifier(connection, table_name)
        for column_name, definition in definitions.items():
            if column_name not in existing:
                connection.exec_driver_sql(
                    f"ALTER TABLE {table_sql} ADD COLUMN "
                    f"{_quoted_identifier(connection, column_name)} {definition}"
                )

        connection.exec_driver_sql(
            "UPDATE action_execution_logs SET actor_type='unknown' "
            "WHERE actor_type IS NULL OR TRIM(actor_type)=''"
        )
        connection.exec_driver_sql(
            "UPDATE action_execution_logs SET model_name='' "
            "WHERE model_name IS NULL"
        )
        connection.exec_driver_sql(
            "UPDATE action_execution_logs SET correlation_id='' "
            "WHERE correlation_id IS NULL"
        )
        for column_name in ("permission_decision", "data_context"):
            column_sql = _quoted_identifier(connection, column_name)
            connection.exec_driver_sql(
                f"UPDATE {table_sql} SET {column_sql}='{{}}' "
                f"WHERE {column_sql} IS NULL"
            )

        index_definitions = {
            "ix_action_execution_logs_actor_user_id": "actor_user_id",
            "ix_action_execution_logs_agent_id": "agent_id",
            "ix_action_execution_logs_llm_config_id": "llm_config_id",
            "ix_action_execution_logs_correlation_id": "correlation_id",
            "ix_action_execution_logs_parent_action_log_id": (
                "parent_action_log_id"
            ),
            "ix_action_execution_logs_agent_message_id": "agent_message_id",
            "ix_action_execution_logs_assistant_message_id": (
                "assistant_message_id"
            ),
        }
        indexes = {
            item.get("name")
            for item in inspect(connection).get_indexes(table_name)
        }
        for index_name, column_name in index_definitions.items():
            if index_name not in indexes:
                connection.exec_driver_sql(
                    f"CREATE INDEX {_quoted_identifier(connection, index_name)} "
                    f"ON {table_sql} "
                    f"({_quoted_identifier(connection, column_name)})"
                )

        unique_name = "uq_action_execution_logs_parent_preview"
        guards = {
            item.get("name")
            for item in inspect(connection).get_indexes(table_name)
        }
        if unique_name not in guards:
            connection.exec_driver_sql(
                f"CREATE UNIQUE INDEX "
                f"{_quoted_identifier(connection, unique_name)} ON {table_sql} "
                "(parent_action_log_id)"
            )

        references = (
            ("actor_user_id", "users", "id"),
            ("agent_id", "agents", "id"),
            ("llm_config_id", "llm_configs", "id"),
            ("parent_action_log_id", "action_execution_logs", "id"),
            ("agent_message_id", "messages", "id"),
            ("assistant_message_id", "assistant_messages", "id"),
        )
        refreshed = inspect(connection)
        for column_name, parent_table, parent_key in references:
            if not refreshed.has_table(parent_table):
                continue
            child_column_sql = _quoted_identifier(connection, column_name)
            parent_table_sql = _quoted_identifier(connection, parent_table)
            parent_key_sql = _quoted_identifier(connection, parent_key)
            connection.exec_driver_sql(
                f"UPDATE {table_sql} SET {child_column_sql}=NULL "
                f"WHERE {child_column_sql} IS NOT NULL AND NOT EXISTS ("
                f"SELECT 1 FROM {parent_table_sql} AS parent "
                f"WHERE parent.{parent_key_sql}="
                f"{table_sql}.{child_column_sql})"
            )
            trigger_base = f"trg_action_logs_{column_name}"
            trigger_insert = _quoted_identifier(
                connection, f"{trigger_base}_insert"
            )
            trigger_update = _quoted_identifier(
                connection, f"{trigger_base}_update"
            )
            trigger_delete = _quoted_identifier(
                connection, f"{trigger_base}_delete"
            )
            connection.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS {trigger_insert} "
                f"BEFORE INSERT ON {table_sql} "
                f"WHEN NEW.{child_column_sql} IS NOT NULL AND NOT EXISTS ("
                f"SELECT 1 FROM {parent_table_sql} AS parent "
                f"WHERE parent.{parent_key_sql}=NEW.{child_column_sql}) "
                "BEGIN SELECT RAISE(ABORT, "
                "'invalid action audit reference'); END"
            )
            connection.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS {trigger_update} "
                f"BEFORE UPDATE OF {child_column_sql} ON {table_sql} "
                f"WHEN NEW.{child_column_sql} IS NOT NULL AND NOT EXISTS ("
                f"SELECT 1 FROM {parent_table_sql} AS parent "
                f"WHERE parent.{parent_key_sql}=NEW.{child_column_sql}) "
                "BEGIN SELECT RAISE(ABORT, "
                "'invalid action audit reference'); END"
            )
            connection.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS {trigger_delete} "
                f"AFTER DELETE ON {parent_table_sql} BEGIN "
                f"UPDATE {table_sql} SET {child_column_sql}=NULL "
                f"WHERE {child_column_sql}=OLD.{parent_key_sql}; END"
            )


def _migrate_data_sources_nullable_scenario() -> None:
    """Rebuild a legacy SQLite data_sources table with a nullable scenario."""

    _require_sqlite_legacy_helper("_migrate_data_sources_nullable_scenario")
    table_name = "data_sources"
    with engine.connect() as connection:
        inspector = inspect(connection)
        if not inspector.has_table(table_name):
            return
        columns = {
            item["name"]: item
            for item in inspector.get_columns(table_name)
        }
        scenario_column = columns.get("scenario_id")
        if scenario_column is None or scenario_column.get("nullable", True):
            return

        source_metadata = MetaData()
        source_table = Table(
            table_name,
            source_metadata,
            autoload_with=connection,
        )
        schema_objects = [
            str(row.sql)
            for row in connection.execute(text(
                "SELECT sql FROM sqlite_master "
                "WHERE tbl_name=:table_name "
                "AND type IN ('index', 'trigger') AND sql IS NOT NULL "
                "ORDER BY CASE type WHEN 'index' THEN 0 ELSE 1 END, name"
            ), {"table_name": table_name}).all()
        ]

        # SQLite only applies PRAGMA foreign_keys outside a transaction.
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
            connection.commit()
            raise RuntimeError(
                "无法在 data_sources 旧库重建前暂停 SQLite 外键检查"
            )
        connection.commit()

        temporary_name = f"data_sources_migration_{uuid4().hex[:12]}"
        quote = connection.dialect.identifier_preparer.quote
        try:
            with connection.begin():
                target_metadata = MetaData()
                for reflected in source_metadata.tables.values():
                    if reflected is not source_table:
                        reflected.to_metadata(target_metadata)
                target_table = source_table.to_metadata(
                    target_metadata,
                    name=temporary_name,
                )
                target_table.c.scenario_id.nullable = True
                # SQLite index names are database-global; replay their exact
                # installed DDL after the temporary table takes the old name.
                target_table.indexes.clear()
                target_table.create(connection)

                column_list = ", ".join(
                    quote(column.name) for column in source_table.columns
                )
                connection.exec_driver_sql(
                    f"INSERT INTO {quote(temporary_name)} ({column_list}) "
                    f"SELECT {column_list} FROM {quote(table_name)}"
                )
                connection.exec_driver_sql(
                    f"DROP TABLE {quote(table_name)}"
                )
                connection.exec_driver_sql(
                    f"ALTER TABLE {quote(temporary_name)} "
                    f"RENAME TO {quote(table_name)}"
                )
                for ddl in schema_objects:
                    connection.exec_driver_sql(ddl)
        finally:
            if connection.in_transaction():
                connection.rollback()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            enabled = connection.exec_driver_sql(
                "PRAGMA foreign_keys"
            ).scalar_one()
            connection.commit()
            if enabled != 1:
                connection.invalidate()
                raise RuntimeError(
                    "data_sources 旧库迁移后未能恢复 SQLite 外键检查"
                )

        violations = connection.exec_driver_sql(
            "PRAGMA foreign_key_check"
        ).fetchall()
        connection.commit()
        if violations:
            raise RuntimeError(
                "data_sources 旧库迁移后检测到外键完整性错误"
            )


def _repair_nullable_orphan_references() -> None:
    """Null unverifiable optional links without deleting legacy history."""

    _require_sqlite_legacy_helper("_repair_nullable_orphan_references")
    repairs = (
        ("assistant_audit_logs", "scenario_id", "business_scenarios"),
        ("assistant_audit_logs", "thread_id", "assistant_threads"),
        ("assistant_threads", "scenario_id", "business_scenarios"),
        ("llm_invocation_traces", "llm_config_id", "llm_configs"),
        ("llm_invocation_traces", "tenant_id", "tenants"),
    )
    with engine.begin() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        for child_table, child_column, parent_table in repairs:
            if child_table not in tables or parent_table not in tables:
                continue
            child_columns = {
                item["name"]
                for item in inspector.get_columns(child_table)
            }
            parent_columns = {
                item["name"]
                for item in inspector.get_columns(parent_table)
            }
            if child_column not in child_columns or "id" not in parent_columns:
                continue
            child_table_sql = _quoted_identifier(connection, child_table)
            child_column_sql = _quoted_identifier(connection, child_column)
            parent_table_sql = _quoted_identifier(connection, parent_table)
            parent_id_sql = _quoted_identifier(connection, "id")
            connection.exec_driver_sql(
                f"UPDATE {child_table_sql} SET {child_column_sql}=NULL "
                f"WHERE {child_column_sql} IS NOT NULL AND NOT EXISTS ("
                f"SELECT 1 FROM {parent_table_sql} AS parent "
                f"WHERE parent.{parent_id_sql}="
                f"{child_table_sql}.{child_column_sql})"
            )


def _migrate_assistant_attachment_lifecycle() -> None:
    """Fail closed for orphaned legacy uploads and add their lifecycle data."""

    _require_sqlite_legacy_helper("_migrate_assistant_attachment_lifecycle")
    table_name = "assistant_attachments"
    with engine.begin() as connection:
        inspector = inspect(connection)
        if not inspector.has_table(table_name):
            return
        existing = {
            item["name"] for item in inspector.get_columns(table_name)
        }
        definitions = {
            "thread_id": "VARCHAR(32)",
            "consumed_at": "DATETIME",
            "expires_at": "DATETIME",
            # There is no trustworthy way to recover the uploaded byte hash
            # from parsed_text. Unknown legacy content therefore stays blank.
            "content_hash": "VARCHAR(64) NOT NULL DEFAULT ''",
        }
        table_sql = _quoted_identifier(connection, table_name)
        for column_name, definition in definitions.items():
            if column_name not in existing:
                connection.exec_driver_sql(
                    f"ALTER TABLE {table_sql} ADD COLUMN "
                    f"{_quoted_identifier(connection, column_name)} {definition}"
                )

        expiry = datetime.now(timezone.utc)
        connection.execute(
            text(
                "UPDATE assistant_attachments SET expires_at=:expiry "
                "WHERE expires_at IS NULL"
            ),
            {"expiry": expiry},
        )
        connection.exec_driver_sql(
            "UPDATE assistant_attachments SET content_hash='' "
            "WHERE content_hash IS NULL"
        )

        if inspector.has_table("assistant_threads"):
            connection.exec_driver_sql(
                "DELETE FROM assistant_attachments "
                "WHERE thread_id IS NOT NULL AND NOT EXISTS ("
                "SELECT 1 FROM assistant_threads "
                "WHERE assistant_threads.id=assistant_attachments.thread_id)"
            )
            trigger_definitions = {
                "trg_assistant_attachment_thread_insert": (
                    "BEFORE INSERT ON assistant_attachments "
                    "WHEN NEW.thread_id IS NOT NULL AND NOT EXISTS ("
                    "SELECT 1 FROM assistant_threads WHERE id=NEW.thread_id) "
                    "BEGIN SELECT RAISE(ABORT, "
                    "'invalid assistant attachment thread'); END"
                ),
                "trg_assistant_attachment_thread_update": (
                    "BEFORE UPDATE OF thread_id ON assistant_attachments "
                    "WHEN NEW.thread_id IS NOT NULL AND NOT EXISTS ("
                    "SELECT 1 FROM assistant_threads WHERE id=NEW.thread_id) "
                    "BEGIN SELECT RAISE(ABORT, "
                    "'invalid assistant attachment thread'); END"
                ),
                "trg_assistant_attachment_thread_delete": (
                    "AFTER DELETE ON assistant_threads BEGIN "
                    "DELETE FROM assistant_attachments "
                    "WHERE thread_id=OLD.id; END"
                ),
            }
            for trigger_name, definition in trigger_definitions.items():
                connection.exec_driver_sql(
                    f"CREATE TRIGGER IF NOT EXISTS "
                    f"{_quoted_identifier(connection, trigger_name)} {definition}"
                )

        indexes = {
            item.get("name")
            for item in inspect(connection).get_indexes(table_name)
        }
        index_definitions = {
            "ix_assistant_attachments_thread_id": "thread_id",
            "ix_assistant_attachments_expires_at": "expires_at",
            "ix_assistant_attachments_content_hash": "content_hash",
        }
        for index_name, column_name in index_definitions.items():
            if index_name not in indexes:
                connection.exec_driver_sql(
                    f"CREATE INDEX {_quoted_identifier(connection, index_name)} "
                    f"ON {table_sql} "
                    f"({_quoted_identifier(connection, column_name)})"
                )


def _migrate_assistant_compilation_jobs() -> None:
    """Safely isolate jobs created before durable compiler identities.

    A missing request identity is backfilled from a domain-separated hash of
    the immutable row id. This prevents two unrelated legacy rows from being
    merged while making no claim that they had the same original input. A
    running row that never persisted restart input is terminally failed: an
    operator must submit a new request instead of replaying invented context.
    """

    _require_sqlite_legacy_helper("_migrate_assistant_compilation_jobs")
    table_name = "assistant_compilation_jobs"
    with engine.begin() as connection:
        inspector = inspect(connection)
        if not inspector.has_table(table_name):
            return
        columns = {
            item["name"] for item in inspector.get_columns(table_name)
        }
        if "id" not in columns:
            raise RuntimeError(
                "assistant_compilation_jobs 旧库缺少 id，无法建立安全请求身份"
            )
        lacked_execution_input = "execution_input" not in columns
        definitions = {
            "request_fingerprint": "VARCHAR(64)",
            "execution_input": "JSON",
            "lease_token": "VARCHAR(64)",
            "lease_expires_at": "DATETIME",
            "lease_attempt": "INTEGER",
        }
        table_sql = _quoted_identifier(connection, table_name)
        for column_name, definition in definitions.items():
            if column_name not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE {table_sql} ADD COLUMN "
                    f"{_quoted_identifier(connection, column_name)} {definition}"
                )

        rows = connection.execute(text(
            "SELECT id, request_fingerprint "
            "FROM assistant_compilation_jobs ORDER BY id"
        )).mappings().all()
        fingerprint_updates: list[dict[str, str]] = []
        for row in rows:
            current = str(row["request_fingerprint"] or "").strip()
            if current:
                continue
            row_id = str(row["id"])
            fingerprint = hashlib.sha256(
                (
                    "ontology-platform:assistant-compilation-job:"
                    f"legacy-request-fingerprint:v1:{row_id}"
                ).encode("utf-8")
            ).hexdigest()
            fingerprint_updates.append({
                "id": row_id,
                "request_fingerprint": fingerprint,
            })
        if fingerprint_updates:
            connection.execute(
                text(
                    "UPDATE assistant_compilation_jobs "
                    "SET request_fingerprint=:request_fingerprint WHERE id=:id"
                ),
                fingerprint_updates,
            )

        connection.exec_driver_sql(
            "UPDATE assistant_compilation_jobs SET "
            "execution_input=COALESCE(execution_input, '{}'), "
            "lease_token=COALESCE(lease_token, ''), "
            "lease_attempt=COALESCE(lease_attempt, 0)"
        )

        if lacked_execution_input and "status" in columns:
            assignments = ["status='failed'"]
            if "error" in columns:
                assignments.append(
                    "error=CASE WHEN error IS NULL OR TRIM(error)='' "
                    "THEN '旧编译任务缺少可验证执行输入，升级后已安全终止，请重新提交' "
                    "ELSE error END"
                )
            if "completed_at" in columns:
                assignments.append(
                    "completed_at=COALESCE(completed_at, CURRENT_TIMESTAMP)"
                )
            connection.exec_driver_sql(
                "UPDATE assistant_compilation_jobs SET "
                + ", ".join(assignments)
                + " WHERE status='running'"
            )

        refreshed = inspect(connection)
        guards = {
            item.get("name")
            for item in refreshed.get_unique_constraints(table_name)
        } | {
            item.get("name")
            for item in refreshed.get_indexes(table_name)
            if item.get("unique")
        }
        unique_name = "uq_assistant_compilation_jobs_fingerprint"
        if unique_name not in guards:
            connection.exec_driver_sql(
                f"CREATE UNIQUE INDEX "
                f"{_quoted_identifier(connection, unique_name)} ON {table_sql} "
                "(request_fingerprint)"
            )

        installed = {
            item["name"]
            for item in refreshed.get_columns(table_name)
        }
        lease_index = "ix_assistant_compilation_jobs_status_lease_expiry"
        indexes = {
            item.get("name")
            for item in refreshed.get_indexes(table_name)
        }
        if (
            {"status", "lease_expires_at"}.issubset(installed)
            and lease_index not in indexes
        ):
            connection.exec_driver_sql(
                f"CREATE INDEX {_quoted_identifier(connection, lease_index)} "
                f"ON {table_sql} (status, lease_expires_at)"
            )

        attachment_table = "assistant_attachments"
        if refreshed.has_table(attachment_table):
            attachment_columns = {
                item["name"]
                for item in refreshed.get_columns(attachment_table)
            }
            attachment_sql = _quoted_identifier(
                connection, attachment_table
            )
            if "content_hash" not in attachment_columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE {attachment_sql} ADD COLUMN "
                    f"{_quoted_identifier(connection, 'content_hash')} "
                    "VARCHAR(64) NOT NULL DEFAULT ''"
                )
            # parsed_text is a derivative and must never be presented as the
            # uploaded-byte digest. Unknown hashes remain explicitly blank.
            connection.exec_driver_sql(
                "UPDATE assistant_attachments SET content_hash='' "
                "WHERE content_hash IS NULL"
            )
            attachment_index = "ix_assistant_attachments_content_hash"
            attachment_indexes = {
                item.get("name")
                for item in inspect(connection).get_indexes(attachment_table)
            }
            if attachment_index not in attachment_indexes:
                connection.exec_driver_sql(
                    f"CREATE INDEX "
                    f"{_quoted_identifier(connection, attachment_index)} "
                    f"ON {attachment_sql} (content_hash)"
                )


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
