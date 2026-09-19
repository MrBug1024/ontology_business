"""Behavioral bounds for external discovery metadata, cancellation and cleanup."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from app.services import distillation_analysis_service, distillation_connector_service


def _source() -> SimpleNamespace:
    return SimpleNamespace(type="postgres", config={"host": "synthetic.invalid", "port": 5432,
        "database": "synthetic", "user": "synthetic", "password": "synthetic-nonreusable"})


class MetadataCursor:
    def __init__(self, *, relation_count=1, column_count=1, stall=False):
        self.relation_count = relation_count
        self.column_count = column_count
        self.stall = stall
        self.statements = []
        self.parameters = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def execute(self, statement, parameters=None):
        self.statements.append(statement)
        self.parameters.append(parameters)
        if self.stall:
            await asyncio.sleep(10)

    async def fetchall(self):
        if len(self.statements) == 1:
            return [(index + 1, f"relation_{index}") for index in range(self.relation_count)]
        return [(f"column_{index}", "text", index == 0) for index in range(self.column_count)]


class MetadataConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    async def close(self):
        self.closed = True


def _connect(monkeypatch, cursor):
    connection = MetadataConnection(cursor)
    parameters = {}

    async def connect(**kwargs):
        if distillation_connector_service.sys.platform == "win32":
            assert isinstance(asyncio.get_running_loop(), asyncio.SelectorEventLoop)
        parameters.update(kwargs)
        return connection

    monkeypatch.setattr(distillation_connector_service.psycopg.AsyncConnection, "connect", connect)
    return connection, parameters


def test_metadata_connection_is_disposable_read_only_and_never_reads_business_rows(monkeypatch):
    cursor = MetadataCursor()
    connection, parameters = _connect(monkeypatch, cursor)
    result = distillation_connector_service.postgres_schema(_source())
    assert result == [{"name": "relation_0", "row_count": -1,
                       "columns": [{"name": "column_0", "type": "text", "pk": True}]}]
    assert connection.closed
    assert parameters["connect_timeout"] == 5
    assert "default_transaction_read_only=on" in parameters["options"]
    assert "statement_timeout=3000" in parameters["options"]
    assert "pg_catalog.pg_class" in cursor.statements[0]
    assert "pg_catalog.pg_attribute" in cursor.statements[1]
    assert cursor.parameters[1] == (1,)


def test_overall_metadata_timeout_cancels_and_closes_connection(monkeypatch):
    connection, _ = _connect(monkeypatch, MetadataCursor(stall=True))
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        distillation_connector_service.postgres_schema(_source(), timeout_seconds=0.02)
    assert connection.closed
    assert time.monotonic() - started < 1


@pytest.mark.parametrize("relation_count,column_count", [(41, 1), (1, 81)])
def test_schema_cardinality_bounds_fail_closed_and_close(monkeypatch, relation_count, column_count):
    connection, _ = _connect(monkeypatch, MetadataCursor(relation_count=relation_count, column_count=column_count))
    with pytest.raises(ValueError, match="too many"):
        distillation_connector_service.postgres_schema(_source())
    assert connection.closed


def test_connector_failures_are_safe_limitations_and_total_deadline_stops_new_connections(monkeypatch):
    clock = iter([0, 1, 46])
    monkeypatch.setattr(distillation_analysis_service.time, "monotonic", lambda: next(clock))
    calls = []

    def fail(source, **kwargs):
        calls.append(kwargs["timeout_seconds"])
        raise TimeoutError("password=synthetic-value; private endpoint diagnostics")

    monkeypatch.setattr(distillation_connector_service, "postgres_schema", fail)
    schemas, limitations = distillation_analysis_service._database_schemas([("first", _source()), ("second", _source())])
    assert schemas == []
    assert calls == [20]
    assert len(limitations) == 2
    assert all("超时" in limitation for limitation in limitations)
    assert "synthetic" not in " ".join(limitations)


def test_exhausted_file_budget_does_not_issue_another_content_read():
    from app.distillation_schemas import Evidence
    from app.services.distillation_material_excerpt_service import file_excerpts

    class NoReadSession:
        def execute(self, *args):
            pytest.fail("no file should be loaded after the content budget is exhausted")

    excerpts, limits, consumed = file_excerpts(NoReadSession(), Evidence(key="ev", title="material"), 0)
    assert excerpts == [] and consumed == 0
    assert "未读取" in limits[0]


def test_distillation_material_is_not_an_executable_connector():
    from app.services import connector_service

    source = SimpleNamespace(id="source", type="distillation", tenant_id="tenant", scenario_id="scenario")
    scenario = SimpleNamespace(id="scenario", tenant_id="tenant")

    class SourceSession:
        def get(self, model, source_id):
            return source

    summary = connector_service.connector_summary(source, "data_source")
    assert summary["capabilities"] == []
    with pytest.raises(connector_service.ConnectorBindingConflictError, match="建模理解"):
        connector_service.require_connector_target(SourceSession(), scenario, kind="data_source", connector_id="source")


def test_mapping_boundary_rejects_handoff_before_attempting_connector_binding(monkeypatch):
    from fastapi import HTTPException
    from app.routers import scenarios

    monkeypatch.setattr(scenarios, "_source_in_scenario", lambda *args: SimpleNamespace(
        resource_scope="modeling", type="distillation",
    ))
    with pytest.raises(HTTPException) as error:
        scenarios._modeling_source_in_scenario(None, "scenario", "source")
    assert error.value.status_code == 422
