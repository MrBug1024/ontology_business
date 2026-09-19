"""Real PG sample reads, including source-enforced read-only transactions."""
import pytest

from app.distillation_sample_schemas import DatabaseSampleArguments
from app.models import DataSource
from app.services import distillation_connector_service as connector
from app.services.library_sample_query import catalog
from isolated_postgresql import isolated_postgresql


def test_postgresql_samples_use_readonly_transaction_and_current_schema_handles(isolated_postgresql, monkeypatch):
    isolated = isolated_postgresql
    url = isolated.runtime_engine.url
    with isolated.admin_engine.begin() as connection:
        role = connection.dialect.identifier_preparer.quote(url.username)
        connection.exec_driver_sql('CREATE SCHEMA sample_research')
        connection.exec_driver_sql('CREATE TABLE sample_research.records (id integer PRIMARY KEY, period text, result text)')
        connection.exec_driver_sql("INSERT INTO sample_research.records VALUES (1,'2026-01','closed'),(2,'2026-02','reworked')")
        connection.exec_driver_sql('CREATE VIEW sample_research.result_view AS SELECT * FROM sample_research.records')
        connection.exec_driver_sql(f'GRANT USAGE ON SCHEMA sample_research TO {role}')
        connection.exec_driver_sql(f'GRANT SELECT ON ALL TABLES IN SCHEMA sample_research TO {role}')
    original = connector.psycopg.AsyncConnection.connect

    async def scoped_connection(**kwargs):
        # Simulate the external read-only account's configured schema without
        # changing the shared deployment role or mocking any database operations.
        kwargs["options"] += " -c search_path=sample_research"
        connection = await original(**kwargs)
        cursor = await connection.execute("SHOW transaction_read_only")
        assert (await cursor.fetchone())[0] == "on"
        return connection

    monkeypatch.setattr(connector.psycopg.AsyncConnection, "connect", scoped_connection)
    source = DataSource(id="source", type="postgres", config={"host": url.host, "port": url.port,
        "database": url.database, "user": url.username, "password": url.password})
    tables = catalog(connector.postgres_schema(source))
    table = next(item for item in tables if item["name"] == "records")
    fields = {item["name"]: item["field_key"] for item in table["fields"]}
    args = DatabaseSampleArguments(data_source_id="source", table_key=table["table_key"],
        field_keys=[fields["period"], fields["result"]], filters=[{"field_key": fields["period"], "value": "2026-02"}])
    sample = connector.postgres_schema(source, sample=args)
    assert sample["rows"] == [{fields["period"]: "2026-02", fields["result"]: "reworked"}]
    args.filters[0].value = "' OR 1=1 --"
    assert connector.postgres_schema(source, sample=args)["rows"] == []
    args.table_key = next(item["table_key"] for item in tables if item["name"] == "result_view")
    args.field_keys, args.filters = [], []
    with pytest.raises(ValueError, match="持久表"):
        connector.postgres_schema(source, sample=args)
