"""Isolated PostgreSQL and explicit real-MinIO acceptance for research libraries."""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from threading import Barrier

from fastapi import HTTPException
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from isolated_postgresql import isolated_postgresql, seed_workspace, tenant_session
from app.models import BucketFile, DataSource, ObjectDeletionJob
from app.routers import data_sources
from app.schemas import DataSourceIn
from app.services import library_database_service as libraries
from app.services import library_sqlite_upload_service as uploads
from app.services import datasource_service
from app.services import object_deletion_service as deletion
from app.services import object_storage_service as objects
from app.services.upload_staging_service import StagedUpload


def staged_database(tmp_path: Path, *, corrupt: bool = False) -> StagedUpload:
    path = tmp_path / "research.sqlite3"
    if corrupt:
        path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
    else:
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE outcomes (id INTEGER PRIMARY KEY, description TEXT)")
            connection.execute("INSERT INTO outcomes VALUES (1, 'synthetic outcome')")
    return StagedUpload(path, path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())


def create_source(isolated, workspace):
    with tenant_session(isolated.runtime_engine, workspace) as db:
        source = data_sources.create_data_source(DataSourceIn(name="Synthetic SQLite research", type="sqlite3",
            scenario_id=workspace["scenario_id"], config={}), db)
        return source.id


def test_real_sqlite_upload_structure_and_outbox_cleanup(isolated_postgresql, tmp_path, monkeypatch):
    """Settings may supply MinIO credentials; neither values nor URLs are printed."""
    if not objects.is_configured():
        pytest.skip("Real managed MinIO is not configured")
    workspace = seed_workspace(isolated_postgresql.admin_engine)
    monkeypatch.setattr(deletion, "_default_session_factory", lambda: Session(isolated_postgresql.runtime_engine))
    source_id = create_source(isolated_postgresql, workspace)
    staged = staged_database(tmp_path)
    identity = None
    try:
        with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
            source = db.get(DataSource, source_id)
            file = uploads.attach_snapshot(db, source, staged, "research.sqlite3")
            identity = (file.bucket_name, file.object_key, file.object_version_id)
            assert file.content_sha256 == staged.content_sha256
            assert file.status == "parsed"
            assert db.get(DataSource, source_id).connector_revision == 2
            tables = data_sources.list_tables(source_id, db)
            assert tables[0]["name"] == "outcomes"
            assert tables[0]["columns"][0]["pk"]
            assert tables[0]["row_count"] == -1
            repeated = uploads.attach_snapshot(db, db.get(DataSource, source_id), staged, "research.sqlite3")
            assert repeated.id == file.id
            conflicting = StagedUpload(staged.path, staged.byte_size, "0" * 64)
            with pytest.raises(HTTPException) as duplicate:
                uploads.attach_snapshot(db, db.get(DataSource, source_id), conflicting, "research.sqlite3")
            assert duplicate.value.status_code == 409
            db.rollback()
            with pytest.raises(HTTPException) as changed:
                data_sources.update_data_source(source_id, DataSourceIn(name="Moved", type="sqlite3",
                    scenario_id=workspace["other_scenario_id"], config={}), db)
            assert changed.value.status_code == 409
            db.rollback()
            assert db.scalar(select(BucketFile.id).where(BucketFile.data_source_id == source_id)) == file.id
            data_sources.delete_data_source(source_id, db)
            assert db.get(DataSource, source_id) is None
            jobs = db.scalars(select(ObjectDeletionJob).where(ObjectDeletionJob.origin_id == file.id,
                ObjectDeletionJob.status == "completed")).all()
            assert jobs
        with pytest.raises(FileNotFoundError):
            objects.stat_object(identity[0], identity[1], version_id=identity[2])
    finally:
        staged.remove()
        # Only a key produced for this random fixture source may be removed.
        if identity is not None:
            assert f"/data-sources/{source_id}/" in identity[1]
            objects.delete_all_object_versions(identity[0], identity[1])


def test_corrupt_sqlite_is_rejected_before_upload_intent(isolated_postgresql, tmp_path, monkeypatch):
    workspace = seed_workspace(isolated_postgresql.admin_engine)
    with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
        source = DataSource(tenant_id=workspace["tenant_id"], scenario_id=workspace["scenario_id"],
            name="Corrupt test", type="sqlite3", resource_scope="modeling", config={})
        db.add(source)
        db.commit()
        monkeypatch.setattr(deletion, "prepare_bucket_file_upload", lambda *_args: pytest.fail("Intent before validation"))
        staged = staged_database(tmp_path, corrupt=True)
        with pytest.raises(HTTPException) as failure:
            uploads.attach_snapshot(db, source, staged, "corrupt.sqlite3")
        assert failure.value.status_code == 422
        assert "database" not in str(failure.value.detail).lower()
        assert db.scalar(select(BucketFile.id).where(BucketFile.data_source_id == source.id)) is None


def test_library_cross_tenant_and_sql_boundaries(isolated_postgresql):
    own = seed_workspace(isolated_postgresql.admin_engine)
    other = seed_workspace(isolated_postgresql.admin_engine)
    with tenant_session(isolated_postgresql.runtime_engine, own) as db:
        source = data_sources.create_data_source(DataSourceIn(name="Bounded MySQL", type="mysql",
            scenario_id=own["scenario_id"], config={"host": "example.invalid", "port": 3306, "database": "research", "user": "reader"}), db)
        with pytest.raises(HTTPException) as sql_failure:
            data_sources.query(source.id, {"sql": "SELECT 1"}, db)
        assert sql_failure.value.status_code == 422
    with tenant_session(isolated_postgresql.runtime_engine, other) as db:
        with pytest.raises(HTTPException) as hidden:
            data_sources.list_tables(source.id, db)
        assert hidden.value.status_code == 404


def test_concurrent_sqlite_uploads_retain_only_one_snapshot(isolated_postgresql, tmp_path, monkeypatch):
    if not objects.is_configured():
        pytest.skip("Real managed MinIO is not configured")
    workspace = seed_workspace(isolated_postgresql.admin_engine)
    monkeypatch.setattr(deletion, "_default_session_factory", lambda: Session(isolated_postgresql.runtime_engine))
    source_id = create_source(isolated_postgresql, workspace)
    staged = staged_database(tmp_path)
    barrier = Barrier(2)
    identities = []
    original_put = datasource_service.save_bucket_file_path

    def synchronized_put(*args, **kwargs):
        file = original_put(*args, **kwargs)
        identities.append((file.bucket_name, file.object_key, file.object_version_id))
        barrier.wait(timeout=15)
        return file

    monkeypatch.setattr(datasource_service, "save_bucket_file_path", synchronized_put)

    def upload():
        with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
            try:
                uploads.attach_snapshot(db, db.get(DataSource, source_id), staged, "research.sqlite3")
                return "retained"
            except HTTPException as exc:
                return exc.status_code

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(upload), pool.submit(upload)
            outcomes = [first.result(timeout=30), second.result(timeout=30)]
        assert outcomes.count("retained") == 1 and outcomes.count(409) == 1
        with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
            files = db.scalars(select(BucketFile).where(BucketFile.data_source_id == source_id)).all()
            assert len(files) == 1
            assert db.get(DataSource, source_id).config["snapshot_file_id"] == files[0].id
            data_sources.delete_data_source(source_id, db)
    finally:
        staged.remove()
        for bucket, key, _version in identities:
            assert f"/data-sources/{source_id}/" in key
            objects.delete_all_object_versions(bucket, key)


def test_real_postgres_library_schema_retains_primary_keys(isolated_postgresql):
    # A database-local role setting selects a tiny fixture schema only for new
    # connector sessions; no business DB or global role default is changed.
    engine = isolated_postgresql.admin_engine
    runtime_url = isolated_postgresql.runtime_engine.url
    quote = engine.dialect.identifier_preparer.quote
    role, database = quote(runtime_url.username), quote(runtime_url.database)
    with engine.begin() as connection:
        connection.execute(text("CREATE SCHEMA library_acceptance"))
        connection.execute(text("CREATE TABLE library_acceptance.outcomes (id integer PRIMARY KEY, description text)"))
        connection.execute(text(f"GRANT USAGE ON SCHEMA library_acceptance TO {role}"))
        connection.execute(text(f"GRANT SELECT ON library_acceptance.outcomes TO {role}"))
        connection.execute(text(f"ALTER ROLE {role} IN DATABASE {database} SET search_path = library_acceptance, public"))
    try:
        source = DataSource(type="postgres", config={"host": runtime_url.host, "port": runtime_url.port,
            "database": runtime_url.database, "user": runtime_url.username, "password": runtime_url.password})
        tables = libraries.database_schema(source)
        assert tables == [{"name": "outcomes", "row_count": -1, "columns": [
            {"name": "id", "type": "integer", "pk": True}, {"name": "description", "type": "text", "pk": False}]}]
    finally:
        with engine.begin() as connection:
            connection.execute(text(f"ALTER ROLE {role} IN DATABASE {database} RESET search_path"))
