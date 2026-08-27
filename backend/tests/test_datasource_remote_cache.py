from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import BucketFile, BusinessScenario, DataSource, Tenant
from app.routers import data_sources as data_source_routes
from app.schemas import DataSourceIn
from app.services import datasource_service, object_storage_service


def test_data_source_url_escapes_credentials_and_database_name() -> None:
    source = DataSource(
        id="source-url",
        name="remote",
        type="mysql",
        config={
            "host": "db.example.test",
            "port": 3307,
            "database": "ontology/business",
            "user": "reader@tenant",
            "password": "p@ss:/#word",
        },
    )

    url = datasource_service._db_url(source)

    assert url.drivername == "mysql+pymysql"
    assert url.username == "reader@tenant"
    assert url.password == "p@ss:/#word"
    assert url.database == "ontology/business"
    assert url.query["charset"] == "utf8mb4"


def test_remote_platform_rejects_new_or_runtime_sqlite_sources() -> None:
    remote_settings = SimpleNamespace(uses_sqlite_database=False)
    payload = DataSourceIn(
        name="forbidden-local",
        type="sqlite",
        config={"path": "backend/data/new.db"},
    )
    with patch.object(
        data_source_routes, "get_settings", return_value=remote_settings
    ):
        with pytest.raises(HTTPException, match="禁止创建"):
            data_source_routes.create_data_source(payload, db=object())
        with pytest.raises(HTTPException, match="禁止切换"):
            data_source_routes.update_data_source("legacy-local", payload, db=object())

    source = DataSource(
        id="legacy-local",
        name="legacy-local",
        type="sqlite",
        config={"path": "backend/data/legacy.db"},
    )
    with patch.object(
        datasource_service, "get_settings", return_value=remote_settings
    ):
        with pytest.raises(ValueError, match="禁止访问"):
            datasource_service._db_url(source)


def test_populated_file_bucket_cannot_change_type_or_scenario(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'scope.db').as_posix()}")
    for table in (
        Tenant.__table__,
        BusinessScenario.__table__,
        DataSource.__table__,
        BucketFile.__table__,
    ):
        table.create(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    db.info["tenant_id"] = "tenant-a"
    content = b"immutable evidence"
    source = DataSource(
        id="source-a",
        tenant_id="tenant-a",
        scenario_id="scenario-a",
        name="evidence",
        type="file_bucket",
        config={
            "storage_backend": "minio",
            "bucket_name": "ontology",
            "prefix": "ontology-business",
        },
    )
    object_key = (
        "ontology-business/tenants/tenant-a/scenarios/scenario-a/"
        "data-sources/source-a/files/file-a/evidence.txt"
    )
    object_url = object_storage_service.stable_object_url("ontology", object_key)
    bucket_file = BucketFile(
        id="file-a",
        data_source_id=source.id,
        filename="evidence.txt",
        stored_path=object_url,
        storage_provider="minio",
        bucket_name="ontology",
        object_key=object_key,
        object_version_id="version-a",
        etag="etag-a",
        object_url=object_url,
        size=len(content),
        content_sha256=hashlib.sha256(content).hexdigest(),
    )
    db.add_all(
        [
            Tenant(id="tenant-a", name="tenant"),
            BusinessScenario(
                id="scenario-a", tenant_id="tenant-a", name="A"
            ),
            BusinessScenario(
                id="scenario-b", tenant_id="tenant-a", name="B"
            ),
            source,
            bucket_file,
        ]
    )
    db.commit()

    with (
        patch.object(
            data_source_routes.permission_service,
            "require_scenario_permission",
        ),
        patch.object(
            data_source_routes.template_catalog_service,
            "lock_scenarios_for_template_write",
        ),
    ):
        with pytest.raises(HTTPException, match="已有文件"):
            data_source_routes.update_data_source(
                source.id,
                DataSourceIn(
                    name=source.name,
                    type="mysql",
                    scenario_id=source.scenario_id,
                    config={"host": "db.example.test"},
                ),
                db=db,
            )
        with pytest.raises(HTTPException, match="已有文件"):
            data_source_routes.update_data_source(
                source.id,
                DataSourceIn(
                    name=source.name,
                    type="file_bucket",
                    scenario_id="scenario-b",
                    config={},
                ),
                db=db,
            )

    db.refresh(source)
    assert source.type == "file_bucket"
    assert source.scenario_id == "scenario-a"
    configured = object_storage_service.MinioConfiguration(
        endpoint="minio.example.test",
        access_key="access",
        secret_key="secret",
        bucket_name="ontology",
        prefix="ontology-business",
    )
    with (
        patch.object(
            object_storage_service, "require_configuration", return_value=configured
        ),
        patch.object(
            object_storage_service,
            "stat_object",
            return_value=object_storage_service.ObjectInfo(
                bucket_name="ontology",
                object_key=object_key,
                size=len(content),
                etag="etag-a",
                version_id="version-a",
            ),
        ),
        patch.object(object_storage_service, "get_object", return_value=content),
    ):
        assert datasource_service.read_bucket_file(bucket_file, source)[0] == content
        assert datasource_service.bucket_file_deletion_identity(
            bucket_file, source
        ) == ("minio", "ontology", object_key, "version-a")

    db.close()
    engine.dispose()


def test_table_listing_uses_structured_reflection_and_redis_ttl(tmp_path: Path) -> None:
    database_path = tmp_path / "source.db"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE entries (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO entries (id, name) VALUES (1, 'a'), (2, 'b')"
        )
        connection.exec_driver_sql(
            "CREATE VIEW entry_names AS SELECT name FROM entries"
        )
    engine.dispose()

    source = DataSource(
        id="source-cache",
        name="local",
        type="sqlite",
        config={"path": str(database_path)},
    )
    stored: dict[str, object] = {}

    def get_json(key: str, default=None):
        return stored.get(key, default)

    def set_json(key: str, value, *, ttl_seconds: int | None = None) -> bool:
        assert ttl_seconds == 120
        stored[key] = value
        return True

    with (
        patch.object(
            datasource_service,
            "get_settings",
            return_value=SimpleNamespace(uses_sqlite_database=True),
        ),
        patch.object(datasource_service.cache_service, "get_json", side_effect=get_json),
        patch.object(datasource_service.cache_service, "set_json", side_effect=set_json),
        patch.object(datasource_service.cache_service, "delete", return_value=False),
    ):
        first = datasource_service.list_tables(source)
        with create_engine(f"sqlite:///{database_path.as_posix()}").begin() as connection:
            connection.exec_driver_sql("CREATE TABLE later (id INTEGER PRIMARY KEY)")
        second = datasource_service.list_tables(source)
        source.connector_revision = 2
        third = datasource_service.list_tables(source)
        datasource_service.invalidate_engine(source)

    by_name = {item["name"]: item for item in first}
    assert set(by_name) == {"entries", "entry_names"}
    assert by_name["entries"]["row_count"] == 2
    assert by_name["entry_names"]["row_count"] == 2
    assert by_name["entries"]["columns"][0]["pk"] is True
    assert second == first
    assert "later" not in {item["name"] for item in second}
    assert "later" in {item["name"] for item in third}
    assert len(stored) == 2
