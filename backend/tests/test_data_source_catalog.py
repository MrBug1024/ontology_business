"""Scenario library catalog scope and bounded route contracts."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import or_, select
from sqlalchemy.dialects import postgresql

from app.models import DataSource
from app.routers import data_sources
from app.services import distillation_library_service as library
from app.services import permission_service, tenant_service


def _compiled_scope(filters: tuple) -> str:
    statement = select(DataSource.id).where(*filters)
    return str(statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    ))


def test_authorized_catalog_scope_is_tenant_fenced_and_includes_shared(monkeypatch):
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        permission_service,
        "require_principal",
        lambda _db: SimpleNamespace(tenant_id="tenant-a"),
    )
    monkeypatch.setattr(
        tenant_service,
        "require_scenario",
        lambda _db, scenario_id: SimpleNamespace(id=scenario_id, tenant_id="tenant-a"),
    )
    monkeypatch.setattr(
        permission_service,
        "require_scenario_permission",
        lambda _db, scenario, verb: calls.append((verb, scenario.id)),
    )
    monkeypatch.setattr(
        permission_service,
        "require_tenant_permission",
        lambda _db, verb: calls.append((verb, None)),
    )

    sql = _compiled_scope(library.authorized_source_filters(
        SimpleNamespace(info={}),
        "scenario-a",
        include_shared=True,
    ))

    assert calls == [("read", "scenario-a"), ("read", None)]
    assert "data_sources.tenant_id = 'tenant-a'" in sql
    assert "data_sources.scenario_id = 'scenario-a'" in sql
    assert "data_sources.scenario_id IS NULL" in sql
    assert "data_sources.resource_scope = 'modeling'" in sql


def test_catalog_scope_omits_shared_when_workspace_read_is_denied(monkeypatch):
    monkeypatch.setattr(
        permission_service,
        "require_principal",
        lambda _db: SimpleNamespace(tenant_id="tenant-a"),
    )

    monkeypatch.setattr(
        tenant_service,
        "require_scenario",
        lambda _db, scenario_id: SimpleNamespace(id=scenario_id, tenant_id="tenant-a"),
    )
    monkeypatch.setattr(permission_service, "require_scenario_permission", lambda *_args: None)
    monkeypatch.setattr(
        permission_service,
        "require_tenant_permission",
        lambda *_args: (_ for _ in ()).throw(HTTPException(403, "workspace denied")),
    )

    sql = _compiled_scope(library.authorized_source_filters(
        SimpleNamespace(info={}),
        "scenario-a",
        include_shared=True,
    ))

    assert "data_sources.scenario_id = 'scenario-a'" in sql
    assert "data_sources.scenario_id IS NULL" not in sql


def test_external_scenario_context_never_expands_into_shared_catalog(monkeypatch):
    workspace_checks: list[str] = []
    monkeypatch.setattr(
        permission_service,
        "require_principal",
        lambda _db: SimpleNamespace(tenant_id="tenant-a"),
    )
    monkeypatch.setattr(
        tenant_service,
        "require_scenario",
        lambda _db, scenario_id: SimpleNamespace(id=scenario_id, tenant_id="tenant-a"),
    )
    monkeypatch.setattr(permission_service, "require_scenario_permission", lambda *_args: None)
    monkeypatch.setattr(
        permission_service,
        "require_tenant_permission",
        lambda _db, verb: workspace_checks.append(verb),
    )
    db = SimpleNamespace(info={"external_scenario_id": "scenario-a"})

    sql = _compiled_scope(library.authorized_source_filters(
        db,
        "scenario-a",
        include_shared=True,
    ))

    assert workspace_checks == []
    assert "data_sources.scenario_id = 'scenario-a'" in sql
    assert "data_sources.scenario_id IS NULL" not in sql


def test_public_foreign_scenario_returns_only_its_public_sources(monkeypatch):
    monkeypatch.setattr(
        permission_service,
        "require_principal",
        lambda _db: SimpleNamespace(tenant_id="tenant-a"),
    )
    monkeypatch.setattr(
        tenant_service,
        "require_scenario",
        lambda _db, scenario_id: SimpleNamespace(id=scenario_id, tenant_id="tenant-b"),
    )
    monkeypatch.setattr(permission_service, "require_scenario_permission", lambda *_args: None)
    monkeypatch.setattr(
        permission_service,
        "require_tenant_permission",
        lambda *_args: (_ for _ in ()).throw(AssertionError("foreign catalog must not read workspace scope")),
    )

    sql = _compiled_scope(library.authorized_source_filters(
        SimpleNamespace(info={}),
        "public-scenario",
        include_shared=True,
    ))

    assert "data_sources.tenant_id = 'tenant-b'" in sql
    assert "data_sources.scenario_id = 'public-scenario'" in sql
    assert "data_sources.is_public IS true" in sql
    assert "data_sources.scenario_id IS NULL" not in sql


def test_workspace_catalog_is_shared_only_and_rejects_external_context(monkeypatch):
    monkeypatch.setattr(
        permission_service,
        "require_principal",
        lambda _db: SimpleNamespace(tenant_id="tenant-a"),
    )
    monkeypatch.setattr(permission_service, "require_tenant_permission", lambda *_args: None)
    sql = _compiled_scope(library.authorized_source_filters(
        SimpleNamespace(info={}),
        None,
        include_shared=True,
    ))
    assert "data_sources.tenant_id = 'tenant-a'" in sql
    assert "data_sources.scenario_id IS NULL" in sql

    with pytest.raises(HTTPException) as denied:
        library.authorized_source_filters(
            SimpleNamespace(info={"external_scenario_id": "scenario-a"}),
            None,
            include_shared=True,
        )
    assert denied.value.status_code == 403


class _Rows:
    def __init__(self, rows: list[DataSource]):
        self.rows = rows

    def scalars(self):
        return self

    def all(self) -> list[DataSource]:
        return self.rows


class _CatalogSession:
    def __init__(self, rows: list[DataSource]):
        self.info = {"tenant_id": "tenant-a"}
        self.rows = rows
        self.statement = None
        self.executions = 0

    def execute(self, statement):
        self.statement = statement
        self.executions += 1
        return _Rows(self.rows)


def _source(
    source_id: str,
    scenario_id: str | None,
    *,
    tenant_id: str = "tenant-a",
) -> DataSource:
    source = DataSource(
        id=source_id,
        tenant_id=tenant_id,
        scenario_id=scenario_id,
        name=source_id,
        type="file_bucket",
        resource_scope="modeling",
        config={},
        status="ok",
        last_error="",
        created_at=datetime.now(timezone.utc),
    )
    source.files = []
    return source


def test_scenario_catalog_uses_envelope_limit_plus_one_and_read_only_shared(monkeypatch):
    session = _CatalogSession([
        _source("scenario-source", "scenario-a"),
        _source("shared-source", None),
        _source("next-source", "scenario-a"),
    ])
    requested: list[tuple[str | None, bool]] = []
    monkeypatch.setattr(data_sources, "_can_access_data_source", lambda *_args, **_kwargs: True)

    def filters(_db, scenario_id, *, include_shared):
        requested.append((scenario_id, include_shared))
        return (
            DataSource.tenant_id == "tenant-a",
            or_(DataSource.scenario_id == scenario_id, DataSource.scenario_id.is_(None)),
        )

    monkeypatch.setattr(library, "authorized_source_filters", filters)

    result = data_sources.list_data_source_catalog(
        scenario_id="scenario-a",
        offset=3,
        limit=2,
        db=session,
    )

    assert requested == [("scenario-a", True)]
    assert [item.id for item in result.items] == ["scenario-source", "shared-source"]
    assert result.items[0].can_write and result.items[0].can_delete
    assert not result.items[1].can_write and not result.items[1].can_delete
    assert result.has_more and result.next_offset == 5
    assert session.statement._offset_clause.value == 3
    assert session.statement._limit_clause.value == 3


def test_public_foreign_catalog_items_are_read_only(monkeypatch):
    session = _CatalogSession([
        _source("public-source", "public-scenario", tenant_id="tenant-b"),
    ])
    session.rows[0].last_error = "postgres://private-host:5432:password leaked"
    monkeypatch.setattr(
        library,
        "authorized_source_filters",
        lambda *_args, **_kwargs: (DataSource.id == "public-source",),
    )
    result = data_sources.list_data_source_catalog(
        scenario_id="public-scenario",
        offset=0,
        limit=20,
        db=session,
    )
    assert [item.id for item in result.items] == ["public-source"]
    assert not result.items[0].can_write
    assert not result.items[0].can_delete
    assert result.items[0].last_error == ""
    assert not result.has_more and result.next_offset is None


def test_legacy_list_hides_connector_errors_for_read_only_source(monkeypatch):
    session = _CatalogSession([])
    source = _source("scenario-source", "scenario-a")
    source.last_error = "postgres://private-host:5432/password leaked"
    session.rows = [source]
    monkeypatch.setattr(
        tenant_service,
        "require_scenario",
        lambda _db, scenario_id: SimpleNamespace(id=scenario_id, tenant_id="tenant-a"),
    )
    monkeypatch.setattr(permission_service, "require_scenario_permission", lambda *_args: None)
    monkeypatch.setattr(tenant_service, "visible_clause", lambda *_args: True)
    monkeypatch.setattr(tenant_service, "current_tenant_id", lambda _db: "tenant-a")

    def access(_db, _source, *, writable=False):
        return not writable

    monkeypatch.setattr(data_sources, "_can_access_data_source", access)

    result = data_sources.list_data_sources(scenario_id="scenario-a", db=session)

    assert len(result) == 1
    assert result[0].last_error == ""
    assert not result[0].can_write
    assert not result[0].can_delete


def test_legacy_list_remains_unpaged_and_catalog_rejects_unbounded_query(monkeypatch):
    session = _CatalogSession([])
    monkeypatch.setattr(data_sources, "_can_access_data_source", lambda *_args, **_kwargs: True)
    assert data_sources.list_data_sources(db=session) == []
    assert session.statement._limit_clause is None
    assert session.statement._offset_clause is None

    app = FastAPI()
    app.include_router(data_sources.router)
    app.dependency_overrides[data_sources.get_tenant_db] = lambda: session
    with TestClient(app) as client:
        response = client.get(
            "/data-sources/catalog",
            params={"scenario_id": "scenario-a", "limit": 201},
        )
    assert response.status_code == 422
    assert session.executions == 1
