"""Bounded, disposable metadata reads for explicitly selected modeling sources."""
from __future__ import annotations

import asyncio
import sys

import psycopg
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import DataSource, DatasetField, DatasetRelation, DatasetVersion, LogicalDataset
from .datasource_service import normalize_postgres_config


CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_MS = 3000
SOURCE_TIMEOUT_SECONDS = 20
ALL_SOURCES_TIMEOUT_SECONDS = 45
MAX_TABLES = 40
MAX_COLUMNS = 80


async def _postgres_metadata(config: dict, sample=None) -> list[dict] | dict:
    connection = await psycopg.AsyncConnection.connect(
        host=config["host"], port=config["port"], dbname=config["database"],
        user=config["user"], password=config.get("password"), connect_timeout=CONNECT_TIMEOUT_SECONDS,
        options=f"-c default_transaction_read_only=on -c statement_timeout={STATEMENT_TIMEOUT_MS} -c lock_timeout=1000",
        application_name="ontology-business-discovery",
    )
    try:
        async with connection.cursor() as cursor:
            await cursor.execute("""
                SELECT relation.oid, relation.relname
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = pg_catalog.current_schema()
                  AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND pg_catalog.has_table_privilege(relation.oid, 'SELECT')
                ORDER BY relation.relname LIMIT 41
            """)
            relations = await cursor.fetchall()
            if len(relations) > MAX_TABLES:
                raise ValueError("too many relations")
            tables = []
            for relation_id, name in relations:
                await cursor.execute("""
                    SELECT attribute.attname,
                           pg_catalog.format_type(attribute.atttypid, attribute.atttypmod),
                           EXISTS (SELECT 1 FROM pg_catalog.pg_index AS index
                                   WHERE index.indrelid = attribute.attrelid AND index.indisprimary
                                     AND attribute.attnum = ANY(index.indkey))
                    FROM pg_catalog.pg_attribute AS attribute
                    WHERE attribute.attrelid = %s AND attribute.attnum > 0 AND NOT attribute.attisdropped
                    ORDER BY attribute.attnum LIMIT 81
                """, (relation_id,))
                columns = await cursor.fetchall()
                if len(columns) > MAX_COLUMNS:
                    raise ValueError("too many columns")
                tables.append({"name": name, "row_count": -1, "columns": [
                    {"name": column, "type": kind, "pk": primary} for column, kind, primary in columns]})
            if sample is not None:
                from .library_sample_query import query, result
                statement, values, metadata = query(tables, sample, "postgres")
                await cursor.execute("SELECT c.relkind FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=pg_catalog.current_schema() AND c.relname=%s", (metadata["name"],))
                kind = await cursor.fetchone()
                if not kind or kind[0] not in {"r", "p", "m"}:
                    raise ValueError("只允许读取有 SELECT 权限的持久表样本")
                await cursor.execute(statement, values)
                return result(await cursor.fetchmany(sample.limit + 1), metadata, sample)
            return tables
    finally:
        # Close directly: rolling back through a network round trip on timeout
        # could itself hang. No pool/cache or connection survives this operation.
        await connection.close()


def postgres_schema(source: DataSource, *, timeout_seconds: float = SOURCE_TIMEOUT_SECONDS, sample=None) -> list[dict] | dict:
    config = normalize_postgres_config(source.config)

    async def bounded_metadata() -> list[dict]:
        return await asyncio.wait_for(_postgres_metadata(config, sample), timeout=max(0.001, timeout_seconds))

    # psycopg async sockets require Selector on Windows; keep this scoped to the
    # one synchronous request instead of changing the process-wide loop policy.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        return runner.run(bounded_metadata())


def managed_dataset_schema(db: Session, source: DataSource) -> list[dict]:
    """Read immutable catalog columns without opening Parquet or a second session."""
    version_id = str((source.config or {}).get("dataset_version_id") or "")
    dataset_id = str((source.config or {}).get("dataset_id") or "")
    query = select(DatasetVersion.schema_id).join(LogicalDataset, LogicalDataset.id == DatasetVersion.dataset_id).where(
        DatasetVersion.id == version_id, DatasetVersion.tenant_id == source.tenant_id,
        DatasetVersion.status == "ready", LogicalDataset.tenant_id == source.tenant_id,
        LogicalDataset.lifecycle_status == "active",
    )
    if dataset_id:
        query = query.where(DatasetVersion.dataset_id == dataset_id)
    schema_id = db.scalar(query)
    if not schema_id:
        raise ValueError("dataset not available")
    relations = db.execute(select(DatasetRelation.id, DatasetRelation.relation_key).where(
        DatasetRelation.schema_id == schema_id, DatasetRelation.tenant_id == source.tenant_id,
    ).order_by(DatasetRelation.ordinal, DatasetRelation.id).limit(MAX_TABLES + 1)).all()
    if len(relations) > MAX_TABLES:
        raise ValueError("too many relations")
    columns = db.execute(select(DatasetField.dataset_relation_id, DatasetField.source_name, DatasetField.physical_type).where(
        DatasetField.schema_id == schema_id, DatasetField.tenant_id == source.tenant_id,
    ).order_by(DatasetField.dataset_relation_id, DatasetField.ordinal).limit(MAX_TABLES * MAX_COLUMNS + 1)).all()
    if len(columns) > MAX_TABLES * MAX_COLUMNS:
        raise ValueError("too many columns")
    grouped = {relation_id: {"name": name, "columns": []} for relation_id, name in relations}
    for relation_id, name, kind in columns:
        if relation_id not in grouped:
            raise ValueError("invalid column scope")
        grouped[relation_id]["columns"].append({"name": name, "type": kind})
        if len(grouped[relation_id]["columns"]) > MAX_COLUMNS:
            raise ValueError("too many columns")
    return list(grouped.values())
