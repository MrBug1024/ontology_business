"""Library contracts and bounded adapters without live third-party services."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import ssl
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.models import BucketFile, DataSource
from app.schemas import DataSourceIn
from app.routers import data_sources
from app.services import library_credential_service as credentials
from app.services import library_database_service as libraries
from app.services import library_mysql_adapter as mysql
from app.services import library_sqlite_adapter as sqlite
from app.services import object_storage_service as objects
from app.services.workflow_payload_service import WorkflowPayloadKeyring


@pytest.fixture
def keyring(monkeypatch):
    ring = WorkflowPayloadKeyring(active_key_id="synthetic", keys={"synthetic": secrets.token_bytes(32)})
    monkeypatch.setattr(credentials, "load_keyring", lambda: ring)
    return ring


def remote():
    return {"host": "example.invalid", "port": 3306, "database": "research", "user": "reader", "password": "synthetic-ephemeral-password"}


def test_remote_credentials_are_encrypted_and_never_returned_to_ui(keyring):
    stored = libraries.write_config("mysql", remote())
    assert "password" not in stored
    assert remote()["password"] not in json.dumps(stored)
    assert credentials.open_config(stored)["password"] == remote()["password"]
    public = credentials.public_config(stored)
    assert public["password_configured"] is True
    assert "password" not in public and credentials.FIELD not in public
    assert remote()["password"] not in repr(DataSourceIn(name="Research", type="mysql", config=remote()))


def test_empty_password_edit_keeps_the_encrypted_secret(keyring):
    stored = libraries.write_config("mysql", remote())
    edited = remote() | {"password": "", "host": "other.invalid"}
    updated = libraries.write_config("mysql", edited, old_config=stored)
    assert credentials.open_config(updated)["password"] == remote()["password"]
    assert updated[credentials.FIELD] != stored[credentials.FIELD]


def test_changed_connection_context_cannot_decrypt_password(keyring):
    stored = libraries.write_config("mysql", remote())
    stored["database"] = "different"
    with pytest.raises(ValueError, match="凭据不可用"):
        credentials.open_config(stored)


def test_missing_encryption_key_fails_before_persistence(monkeypatch):
    monkeypatch.setattr(credentials, "load_keyring", lambda: (_ for _ in ()).throw(RuntimeError("private internals")))
    with pytest.raises(ValueError, match="WORKFLOW_PAYLOAD_ENCRYPTION_KEYS") as failure:
        libraries.write_config("postgres", remote())
    assert "private internals" not in str(failure.value)


@pytest.mark.parametrize("field,value", [("path", "C:/private/database.db"), ("dsn", "mysql://example.invalid"), ("unix_socket", "/run/db.sock"), (credentials.FIELD, {})])
def test_remote_config_rejects_unmanaged_options(field, value):
    with pytest.raises(ValueError):
        libraries.remote_config(remote() | {field: value})


@pytest.mark.parametrize("kind", ["sqlite3", "file_bucket"])
def test_file_library_cannot_choose_server_paths_or_objects(kind):
    with pytest.raises(ValueError, match="受管上传"):
        libraries.write_config(kind, {"path": "C:/private/database.db"})


def test_public_envelope_rejects_extra_fields():
    with pytest.raises(ValidationError):
        DataSourceIn(name="Research", type="mysql", config=remote(), tenant_id="fake")


def test_mysql_rejects_plaintext_server_before_authentication(monkeypatch):
    calls = []
    monkeypatch.setattr(mysql.pymysql.Connection, "_request_authentication", lambda _self: calls.append("sent credential"))
    connection = mysql.RequiredTLSConnection.__new__(mysql.RequiredTLSConnection)
    connection.server_capabilities = 0
    with pytest.raises(ValueError, match="TLS"):
        connection._request_authentication()
    assert calls == []


def test_mysql_requires_deployment_host_approval_before_dns(monkeypatch):
    monkeypatch.setattr(mysql, "get_settings", lambda: SimpleNamespace(library_mysql_allowed_hosts=""))
    monkeypatch.setattr(mysql.socket, "getaddrinfo", lambda *_args: pytest.fail("DNS before approval"))
    with pytest.raises(mysql.LibraryConfigurationError, match="LIBRARY_MYSQL_ALLOWED_HOSTS"):
        mysql._resolve("example.invalid", 3306)


def test_mysql_metadata_uses_bound_schema_and_readonly_disposable_tls_connection(monkeypatch):
    calls, connections = [], []
    class Socket:
        def settimeout(self, value): assert 0 < value <= 5
        def connect(self, value): assert value == ("192.0.2.12", 3306)
        def close(self): calls.append("socket closed")
    class Connection:
        def __init__(self, **kwargs):
            assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
            assert kwargs["ssl"].check_hostname
            assert not kwargs["local_infile"]
            assert kwargs["defer_connect"]
            connections.append(self)
        def connect(self, **kwargs): assert isinstance(kwargs["sock"], Socket)
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def execute(self, sql, parameters=None): calls.append((sql, parameters))
        def fetchall(self):
            return [("items",)] if "TABLE_NAME FROM" in calls[-1][0] else [("id", "bigint", "PRI")]
        def close(self): calls.append("connection closed")
    monkeypatch.setattr(mysql, "_resolve", lambda *_args: (2, 1, 6, "", ("192.0.2.12", 3306)))
    monkeypatch.setattr(mysql.socket, "socket", lambda *_args: Socket())
    monkeypatch.setattr(mysql, "RequiredTLSConnection", Connection)
    result = mysql.mysql_schema(remote())
    assert result[0]["columns"][0]["pk"]
    assert any(isinstance(item, tuple) and item[0] == "SET SESSION TRANSACTION READ ONLY" for item in calls)
    assert any(isinstance(item, tuple) and item[1] == ("research", "items") for item in calls)
    assert calls[-2:] == ["connection closed", "socket closed"]


def sqlite_file(tmp_path: Path) -> Path:
    path = tmp_path / "snapshot.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE "business items" (id INTEGER PRIMARY KEY, value TEXT)')
        connection.execute('INSERT INTO "business items" VALUES (1, ?)', ("sensitive row must stay unread",))
    return path


def test_sqlite_reads_real_structure_without_rows_or_file_mutation(tmp_path):
    path = sqlite_file(tmp_path)
    before = path.read_bytes()
    tables = sqlite.inspect_path(path)
    assert tables == [{"name": "business items", "row_count": -1, "columns": [
        {"name": "id", "type": "INTEGER", "pk": True}, {"name": "value", "type": "TEXT", "pk": False}]}]
    assert path.read_bytes() == before
    assert "sensitive row" not in json.dumps(tables)


def test_sqlite_rejects_fake_extension_and_excess_tables(tmp_path):
    path = tmp_path / "fake.db"
    path.write_bytes(b"not a database" * 20)
    with pytest.raises(ValueError, match="有效"):
        sqlite.inspect_path(path)
    path.unlink()
    with sqlite3.connect(path) as connection:
        for index in range(41):
            connection.execute(f'CREATE TABLE table_{index} (id INTEGER)')
    with pytest.raises(ValueError, match="40"):
        sqlite.inspect_path(path)


def test_sqlite_managed_download_verifies_content_identity_and_cleans_tempfile(tmp_path, monkeypatch):
    path = sqlite_file(tmp_path)
    body = path.read_bytes()
    source = DataSource(id="a" * 32, type="sqlite3", config={})
    source.files = [BucketFile(id="b" * 32, data_source_id=source.id, size=len(body), content_sha256=hashlib.sha256(body).hexdigest(), object_version_id="version", filename="snapshot.db")]
    source.config = {"snapshot_file_id": source.files[0].id, "snapshot_sha256": source.files[0].content_sha256}
    destinations = []
    monkeypatch.setattr(sqlite.datasource_service, "minio_file_identity", lambda *_args: ("managed", "exact-key", "snapshot.db"))
    def download(bucket, key, destination, **kwargs):
        assert (bucket, key) == ("managed", "exact-key")
        assert kwargs["version_id"] == "version" and kwargs["max_bytes"] == sqlite.MAX_SQLITE_BYTES
        destinations.append(destination)
        destination.write_bytes(body)
        return len(body)
    monkeypatch.setattr(sqlite.library_object_reader, "download_snapshot", download)
    assert libraries.database_schema(source)[0]["name"] == "business items"
    assert all(not destination.exists() for destination in destinations)
    source.files[0].content_sha256 = "0" * 64
    source.config["snapshot_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="身份"):
        libraries.database_schema(source)
    assert all(not destination.exists() for destination in destinations)


def test_metadata_capacity_rejects_extra_connections_and_recovers(monkeypatch):
    monkeypatch.setattr(libraries, "_database_schema", lambda *_args, **_kwargs: [{"name": "bounded"}])
    for _ in range(libraries.MAX_CONCURRENT_READS):
        assert libraries._READ_SLOTS.acquire(blocking=False)
    try:
        with pytest.raises(ValueError, match="繁忙"):
            libraries.database_schema(DataSource(type="mysql"))
    finally:
        for _ in range(libraries.MAX_CONCURRENT_READS):
            libraries._READ_SLOTS.release()
    assert libraries.database_schema(DataSource(type="mysql")) == [{"name": "bounded"}]


def test_sqlite_multipart_failure_cleans_staging_and_hides_parser_details(monkeypatch):
    app = FastAPI()
    app.include_router(data_sources.router)
    app.dependency_overrides[data_sources.get_tenant_db] = lambda: object()
    monkeypatch.setattr(data_sources, "_sqlite_upload_source", lambda *_args: DataSource(type="sqlite3"))
    paths = []
    def reject(_db, _source, staged, _filename):
        paths.append(staged.path)
        assert staged.path.is_file()
        raise sqlite3.DatabaseError("private parser internals")
    monkeypatch.setattr(data_sources.library_sqlite_upload_service, "attach_snapshot", reject)
    with TestClient(app) as client:
        response = client.post("/data-sources/synthetic/sqlite-file", files={
            "file": ("snapshot.db", b"SQLite format 3\x00" + b"\x00" * 200, "application/octet-stream")})
    assert response.status_code == 422
    assert "private parser internals" not in response.text
    assert paths and all(not path.exists() for path in paths)
