from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as parquet
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import (
    BucketFile,
    DataSource,
    DatasetFragment,
    DatasetField,
    DatasetRelation,
    DatasetSchema,
    DatasetVersion,
    LogicalDataset,
    Tenant,
)
from app.services import (
    dataset_query_service,
    datasource_service,
    medical_audit_service,
    object_storage_service,
)
from app.services.policies import PolicyViolation, validate_read_only_sql


def _parquet(path: Path) -> bytes:
    parquet.write_table(
        pa.table({"name": ["alpha", "beta"], "amount": [10, 20]}), path
    )
    return path.read_bytes()


def _catalog(path: Path) -> dataset_query_service.DatasetCatalog:
    content = path.read_bytes()
    fragment = dataset_query_service.DatasetFragmentSpec(
        id="fragment-1",
        bucket_name="ontology",
        object_key="managed/fragment.parquet",
        version_id="v1",
        content_sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
        ordinal=0,
    )
    relation = dataset_query_service.DatasetRelationSpec(
        id="relation-1",
        relation_key="规则表",
        ordinal=0,
        row_count=2,
        fragments=(fragment,),
        expected_columns=("name", "amount"),
    )
    derived = dataset_query_service.DatasetRelationSpec(
        id="relation-2",
        relation_key="高额规则",
        ordinal=1,
        row_count=-1,
        fragments=(),
        kind="view",
        view_sql='SELECT name, amount FROM "规则表" WHERE amount >= 20',
        expected_columns=("name", "amount"),
    )
    return dataset_query_service.DatasetCatalog(
        dataset_id="dataset-1",
        dataset_version_id="version-1",
        relations=(relation, derived),
    )


def _lease(path: Path) -> SimpleNamespace:
    return SimpleNamespace(path=path, release=lambda: None)


def _cache_settings(
    *, max_object_bytes: int, max_total_bytes: int, max_age_seconds: int = 3600
) -> SimpleNamespace:
    return SimpleNamespace(
        dataset_cache_max_object_bytes=max_object_bytes,
        dataset_cache_max_bytes=max_total_bytes,
        dataset_cache_max_age_seconds=max_age_seconds,
    )


def _fragment(content: bytes, suffix: str) -> dataset_query_service.DatasetFragmentSpec:
    return dataset_query_service.DatasetFragmentSpec(
        id=f"fragment-{suffix}",
        bucket_name="ontology",
        object_key=f"managed/{suffix}.parquet",
        version_id=f"version-{suffix}",
        content_sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
        ordinal=0,
    )


def test_dataset_connection_uses_verified_views_and_named_parameters(tmp_path: Path) -> None:
    parquet_path = tmp_path / "fragment.parquet"
    _parquet(parquet_path)
    with patch.object(
        dataset_query_service,
        "_acquire_cached_fragment",
        return_value=_lease(parquet_path),
    ):
        connection = dataset_query_service.DatasetConnection(_catalog(parquet_path))
    try:
        columns, rows = connection.execute(
            'SELECT "amount" FROM "规则表" WHERE "name" = :name OR "name" = :name',
            {"name": "beta"},
        )
        assert columns == ["amount"]
        assert rows == [(20,)]

        columns, rows = connection.execute(
            "WITH selected AS MATERIALIZED ("
            "SELECT instr(?, name) AS matched, "
            "date_diff('day', date('2024-01-01'), date('2024-01-03')) + 1 AS days "
            'FROM "规则表" WHERE name = ?'
            ") SELECT matched, days FROM selected",
            ("contains beta", "beta"),
        )
        assert columns == ["matched", "days"]
        assert rows == [(10, 3)]

        tables = connection.describe()
        assert tables[0]["name"] == "规则表"
        assert tables[0]["row_count"] == 2
        assert tables[1]["name"] == "高额规则"
        assert tables[1]["row_count"] == -1
        assert connection.execute('SELECT * FROM "高额规则"')[1] == [
            ("beta", 20)
        ]

        outside = (tmp_path / "outside.csv").resolve()
        outside.write_text("secret\nvalue\n", encoding="utf-8")
        with pytest.raises(dataset_query_service.DatasetQueryError):
            connection.execute(f"SELECT * FROM read_csv_auto('{outside.as_posix()}')")
    finally:
        connection.close()


def test_named_parameter_translation_skips_literals_comments_and_casts() -> None:
    sql, values = dataset_query_service._named_to_positional(
        "SELECT ':literal' AS value, 1::BIGINT AS number -- :comment\nWHERE 2 = :actual",
        {"actual": 2},
    )
    assert "':literal'" in sql
    assert "1::BIGINT" in sql
    assert ":comment" in sql
    assert sql.endswith("WHERE 2 = ?")
    assert values == (2,)


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("duckdb", "SELECT sleep_ms(3600000)"),
        ("duckdb", "SELECT * FROM read_csv_auto('/tmp/outside.csv')"),
        ("duckdb", "SELECT * FROM query_table('outside')"),
        ("duckdb", "SELECT current_setting('allowed_paths')"),
        ("duckdb", 'SELECT "sleep_ms"(3600000)'),
    ],
)
def test_dialect_read_only_policy_rejects_external_and_resource_abuse(
    dialect: str, sql: str
) -> None:
    with pytest.raises(PolicyViolation):
        validate_read_only_sql(sql, dialect=dialect)


def test_dialect_policy_keeps_ordinary_bound_selects_valid() -> None:
    statement = (
        "SELECT id FROM ledger WHERE id = :id "
        "AND note = 'SLEEP(1) is inert text'"
    )
    assert validate_read_only_sql(statement, dialect="postgres") == statement


def test_duckdb_resource_limits_timeout_and_connection_recovery(
    tmp_path: Path,
) -> None:
    parquet_path = tmp_path / "fragment.parquet"
    _parquet(parquet_path)
    released: list[bool] = []
    lease = SimpleNamespace(
        path=parquet_path,
        release=lambda: released.append(True),
    )
    settings = SimpleNamespace(
        dataset_query_timeout_seconds=0.1,
        dataset_query_max_concurrency=1,
        dataset_duckdb_memory_limit_bytes=64 * 1024 * 1024,
        dataset_duckdb_threads=1,
        dataset_duckdb_temp_directory=str((tmp_path / "duckdb-temp").resolve()),
        dataset_duckdb_max_temp_directory_bytes=64 * 1024 * 1024,
    )
    with patch.object(
        dataset_query_service, "get_settings", return_value=settings
    ), patch.object(
        dataset_query_service, "_acquire_cached_fragment", return_value=lease
    ):
        connection = dataset_query_service.DatasetConnection(_catalog(parquet_path))
    try:
        configured = dict(
            connection._connection.execute(  # noqa: SLF001 - policy verification
                "SELECT name, value FROM duckdb_settings() WHERE name IN "
                "('memory_limit', 'threads', 'temp_directory', "
                "'max_temp_directory_size', 'lock_configuration')"
            ).fetchall()
        )
        assert configured["memory_limit"] == "64.0 MiB"
        assert configured["threads"] == "1"
        assert configured["max_temp_directory_size"] == "64.0 MiB"
        assert configured["lock_configuration"] == "true"
        assert Path(configured["temp_directory"]).resolve() == (
            tmp_path / "duckdb-temp"
        ).resolve()

        execution_slot = connection._query_semaphore  # noqa: SLF001
        assert execution_slot is not None
        assert execution_slot.acquire(timeout=0)
        try:
            with pytest.raises(
                dataset_query_service.DatasetQueryError, match="执行槽"
            ):
                connection.execute("SELECT 1")
        finally:
            execution_slot.release()

        started = time.monotonic()
        with pytest.raises(dataset_query_service.DatasetQueryError, match="超时"):
            connection.execute(
                "SELECT sum(sin(i)) FROM range(10000000000) AS generated(i)"
            )
        assert time.monotonic() - started < 5
        assert connection.execute("SELECT 42")[1] == [(42,)]
    finally:
        connection.close()
    assert released == [True]


def test_cache_capacity_never_evicts_an_in_use_fragment(tmp_path: Path) -> None:
    contents = {
        "managed/first.parquet": b"a" * 16,
        "managed/second.parquet": b"b" * 16,
    }
    first = _fragment(contents["managed/first.parquet"], "first")
    second = _fragment(contents["managed/second.parquet"], "second")
    calls: list[str] = []

    def download(
        bucket_name: str,
        object_key: str,
        destination: Path,
        *,
        version_id: str,
        max_bytes: int,
    ) -> object_storage_service.ObjectInfo:
        del version_id
        assert bucket_name == "ontology"
        content = contents[object_key]
        assert max_bytes == len(content)
        Path(destination).write_bytes(content)
        calls.append(object_key)
        return object_storage_service.ObjectInfo(
            bucket_name=bucket_name,
            object_key=object_key,
            size=len(content),
        )

    cache_root = tmp_path / "cache"
    settings = _cache_settings(max_object_bytes=16, max_total_bytes=16)
    with patch.object(dataset_query_service, "_CACHE_ROOT", cache_root), patch.object(
        dataset_query_service, "get_settings", return_value=settings
    ), patch.object(
        object_storage_service, "download_object_to_file", side_effect=download
    ):
        first_lease = dataset_query_service._acquire_cached_fragment(first)
        try:
            with pytest.raises(dataset_query_service.DatasetQueryError, match="容量不足"):
                dataset_query_service._acquire_cached_fragment(second)
            assert first_lease.path.is_file()
            assert calls == ["managed/first.parquet"]
        finally:
            first_lease.release()

        second_lease = dataset_query_service._acquire_cached_fragment(second)
        try:
            assert not (cache_root / f"{first.content_sha256}.parquet").exists()
            assert second_lease.path.is_file()
        finally:
            second_lease.release()


def test_cache_age_eviction_and_concurrent_single_download(tmp_path: Path) -> None:
    old_content = b"o" * 32
    shared_content = b"s" * 32
    old_fragment = _fragment(old_content, "old")
    shared_fragment = _fragment(shared_content, "shared")
    contents = {
        old_fragment.object_key: old_content,
        shared_fragment.object_key: shared_content,
    }
    calls: list[str] = []
    calls_guard = threading.Lock()

    def download(
        bucket_name: str,
        object_key: str,
        destination: Path,
        *,
        version_id: str,
        max_bytes: int,
    ) -> object_storage_service.ObjectInfo:
        del version_id
        content = contents[object_key]
        assert bucket_name == "ontology"
        assert max_bytes == len(content)
        time.sleep(0.05)
        Path(destination).write_bytes(content)
        with calls_guard:
            calls.append(object_key)
        return object_storage_service.ObjectInfo(
            bucket_name=bucket_name,
            object_key=object_key,
            size=len(content),
        )

    cache_root = tmp_path / "cache"
    settings = _cache_settings(
        max_object_bytes=32,
        max_total_bytes=128,
        max_age_seconds=1,
    )
    with patch.object(dataset_query_service, "_CACHE_ROOT", cache_root), patch.object(
        dataset_query_service, "get_settings", return_value=settings
    ), patch.object(
        object_storage_service, "download_object_to_file", side_effect=download
    ):
        old_lease = dataset_query_service._acquire_cached_fragment(old_fragment)
        old_lease.release()
        old_marker = cache_root / ".access" / f"{old_fragment.content_sha256}.access"
        os.utime(old_marker, (1, 1))

        with ThreadPoolExecutor(max_workers=2) as executor:
            leases = list(
                executor.map(
                    lambda _index: dataset_query_service._acquire_cached_fragment(
                        shared_fragment
                    ),
                    range(2),
                )
            )
        try:
            assert calls.count(shared_fragment.object_key) == 1
            assert not old_lease.path.exists()
        finally:
            for lease in leases:
                lease.release()


def test_list_tables_uses_validated_catalog_without_materializing_fragments() -> None:
    catalog = dataset_query_service.DatasetCatalog(
        dataset_id="dataset-1",
        dataset_version_id="version-1",
        relations=(
            dataset_query_service.DatasetRelationSpec(
                id="relation-base",
                relation_key="ledger",
                ordinal=0,
                row_count=3,
                fragments=(),
                declared_columns=(("id", "BIGINT", False, 0),),
            ),
            dataset_query_service.DatasetRelationSpec(
                id="relation-view",
                relation_key="large_ledger",
                ordinal=1,
                row_count=-1,
                fragments=(),
                kind="view",
                declared_columns=(("id", "BIGINT", False, 0),),
            ),
        ),
    )
    source = SimpleNamespace(type="dataset")
    with patch.object(
        dataset_query_service, "_load_catalog", return_value=catalog
    ), patch.object(dataset_query_service, "_acquire_cached_fragment") as acquire:
        tables = dataset_query_service.list_tables(source)
    acquire.assert_not_called()
    assert tables == [
        {
            "name": "ledger",
            "columns": [{"name": "id", "type": "BIGINT", "pk": True}],
            "row_count": 3,
        },
        {
            "name": "large_ledger",
            "columns": [{"name": "id", "type": "BIGINT", "pk": True}],
            "row_count": -1,
        },
    ]


def test_derived_views_follow_dependencies_and_reject_external_scans(
    tmp_path: Path,
) -> None:
    parquet_path = tmp_path / "fragment.parquet"
    _parquet(parquet_path)
    base_catalog = _catalog(parquet_path)
    base = base_catalog.relations[0]
    final_view = dataset_query_service.DatasetRelationSpec(
        id="relation-final",
        relation_key="最终视图",
        ordinal=1,
        row_count=-1,
        fragments=(),
        kind="view",
        view_sql='SELECT name FROM "中间视图"',
        expected_columns=("name",),
    )
    intermediate_view = dataset_query_service.DatasetRelationSpec(
        id="relation-middle",
        relation_key="中间视图",
        ordinal=2,
        row_count=-1,
        fragments=(),
        kind="view",
        view_sql='SELECT name FROM "规则表" WHERE amount >= 20',
        expected_columns=("name",),
    )
    catalog = dataset_query_service.DatasetCatalog(
        dataset_id="dataset-1",
        dataset_version_id="version-1",
        relations=(base, final_view, intermediate_view),
    )
    with patch.object(
        dataset_query_service,
        "_acquire_cached_fragment",
        return_value=_lease(parquet_path),
    ):
        connection = dataset_query_service.DatasetConnection(catalog)
    try:
        assert connection.execute('SELECT * FROM "最终视图"')[1] == [("beta",)]
    finally:
        connection.close()

    unsafe_view = dataset_query_service.DatasetRelationSpec(
        id="relation-unsafe",
        relation_key="unsafe",
        ordinal=1,
        row_count=-1,
        fragments=(),
        kind="view",
        view_sql=(
            'SELECT b.* FROM "规则表" AS b '
            "CROSS JOIN read_csv_auto('C:/secrets.csv') AS leaked"
        ),
    )
    unsafe_catalog = dataset_query_service.DatasetCatalog(
        dataset_id="dataset-1",
        dataset_version_id="version-1",
        relations=(base, unsafe_view),
    )
    with patch.object(
        dataset_query_service,
        "_acquire_cached_fragment",
        return_value=_lease(parquet_path),
    ), pytest.raises(dataset_query_service.DatasetQueryError):
        dataset_query_service.DatasetConnection(unsafe_catalog)
    with pytest.raises(dataset_query_service.DatasetQueryError):
        dataset_query_service._validate_derived_select(
            'SELECT * FROM "规则表"; DROP TABLE "规则表"'
        )


def test_fragment_cache_streams_once_and_verifies_hash_and_size(tmp_path: Path) -> None:
    content = b"verified parquet bytes"
    digest = hashlib.sha256(content).hexdigest()
    fragment = dataset_query_service.DatasetFragmentSpec(
        id="fragment-1",
        bucket_name="ontology",
        object_key="managed/fragment.parquet",
        version_id="",
        content_sha256=digest,
        byte_size=len(content),
        ordinal=0,
    )
    calls: list[Path] = []

    def download(
        bucket_name: str,
        object_key: str,
        destination: Path,
        *,
        version_id: str,
        max_bytes: int,
    ) -> object_storage_service.ObjectInfo:
        del version_id
        assert max_bytes == len(content)
        assert bucket_name == "ontology"
        assert object_key == "managed/fragment.parquet"
        target = Path(destination)
        target.write_bytes(content)
        calls.append(target)
        return object_storage_service.ObjectInfo(
            bucket_name=bucket_name,
            object_key=object_key,
            size=len(content),
        )

    with patch.object(dataset_query_service, "_CACHE_ROOT", tmp_path), patch.object(
        object_storage_service, "download_object_to_file", side_effect=download
    ):
        first = dataset_query_service._materialize_fragment(fragment)
        second = dataset_query_service._materialize_fragment(fragment)

    assert first == second
    assert first.read_bytes() == content
    assert len(calls) == 1


def test_catalog_resolution_uses_version_schema_and_enforces_tenant() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    LocalSession = sessionmaker(bind=engine, expire_on_commit=False)
    digest = "a" * 64
    base_schema = [
        {
            "name": "amount",
            "physical_type": "BIGINT",
            "nullable": True,
            "key_ordinal": None,
            "ordinal": 0,
        }
    ]
    derived_schema = [dict(base_schema[0])]
    derived_details = {
        "kind": "view",
        "view_sql": 'SELECT amount FROM "charges" WHERE amount >= 20',
        "schema": derived_schema,
        "schema_hash": dataset_query_service._canonical_json_sha256(derived_schema),
        "materialized": False,
    }
    schema_document = {
        "relations": {"charges": base_schema},
        "derived_relations": {"large_charges": derived_details},
    }
    version_manifest = {
        "relations": {
            "charges": {
                "row_count": 2,
                "byte_size": 128,
                "content_sha256": digest,
                "schema_hash": dataset_query_service._canonical_json_sha256(
                    base_schema
                ),
            }
        },
        "derived_relations": {"large_charges": derived_details},
    }
    with LocalSession() as session:
        session.add(Tenant(id="tenant-a", name="Tenant A"))
        session.add(
            DataSource(
                id="file-source",
                tenant_id="tenant-a",
                name="Managed files",
                type="file_bucket",
                config={"storage_backend": "minio"},
            )
        )
        session.add(
            LogicalDataset(
                id="dataset-1",
                tenant_id="tenant-a",
                key="medical",
                name="Medical",
            )
        )
        session.add(
            DatasetSchema(
                id="schema-1",
                tenant_id="tenant-a",
                dataset_id="dataset-1",
                schema_version=1,
                schema_hash=dataset_query_service._canonical_json_sha256(
                    schema_document
                ),
                schema_document=schema_document,
            )
        )
        session.add(
            DatasetRelation(
                id="relation-1",
                tenant_id="tenant-a",
                dataset_id="dataset-1",
                schema_id="schema-1",
                relation_key="charges",
                display_name="Charges",
                ordinal=0,
            )
        )
        session.add(
            DatasetRelation(
                id="relation-2",
                tenant_id="tenant-a",
                dataset_id="dataset-1",
                schema_id="schema-1",
                relation_key="large_charges",
                display_name="Large charges",
                kind="view",
                ordinal=1,
            )
        )
        session.add_all(
            [
                DatasetField(
                    id="field-1",
                    tenant_id="tenant-a",
                    dataset_id="dataset-1",
                    schema_id="schema-1",
                    dataset_relation_id="relation-1",
                    field_key="amount",
                    source_name="amount",
                    logical_type="integer",
                    physical_type="BIGINT",
                    nullable=True,
                    ordinal=0,
                ),
                DatasetField(
                    id="field-2",
                    tenant_id="tenant-a",
                    dataset_id="dataset-1",
                    schema_id="schema-1",
                    dataset_relation_id="relation-2",
                    field_key="amount",
                    source_name="amount",
                    logical_type="integer",
                    physical_type="BIGINT",
                    nullable=True,
                    ordinal=0,
                ),
            ]
        )
        session.add(
            DatasetVersion(
                id="version-1",
                tenant_id="tenant-a",
                dataset_id="dataset-1",
                schema_id="schema-1",
                version_number=1,
                status="ready",
                record_count=2,
                fragment_count=1,
                byte_size=128,
                content_hash="c" * 64,
                manifest=version_manifest,
            )
        )
        session.add(
            BucketFile(
                id="file-1",
                data_source_id="file-source",
                filename="charges.parquet",
                stored_path="minio://ontology/fragments/charges.parquet",
                storage_provider="minio",
                bucket_name="ontology",
                object_key="fragments/charges.parquet",
                size=128,
                mime="application/vnd.apache.parquet",
                content_sha256=digest,
            )
        )
        session.add(
            DatasetFragment(
                id="fragment-1",
                tenant_id="tenant-a",
                dataset_id="dataset-1",
                dataset_version_id="version-1",
                dataset_relation_id="relation-1",
                schema_id="schema-1",
                bucket_file_id="file-1",
                bucket_data_source_id="file-source",
                ordinal=0,
                status="ready",
                row_count=2,
                byte_size=128,
                content_sha256=digest,
            )
        )
        session.commit()

    source = SimpleNamespace(
        tenant_id="tenant-a",
        config={"dataset_version_id": "version-1", "dataset_id": "dataset-1"},
    )
    with patch.object(dataset_query_service, "SessionLocal", LocalSession):
        catalog = dataset_query_service._load_catalog(source)
        assert catalog.relations[0].relation_key == "charges"
        assert catalog.relations[0].row_count == 2
        assert catalog.relations[1].relation_key == "large_charges"
        assert catalog.relations[1].kind == "view"
        assert catalog.relations[1].fragments == ()

        source.tenant_id = "tenant-b"
        with pytest.raises(dataset_query_service.DatasetQueryError, match="无权访问"):
            dataset_query_service._load_catalog(source)

        source.tenant_id = "tenant-a"
        with LocalSession() as session:
            version = session.get(DatasetVersion, "version-1")
            version.manifest = {
                "relations": version.manifest["relations"],
                "derived_relations": {},
            }
            session.commit()
        with pytest.raises(dataset_query_service.DatasetQueryError, match="不一致"):
            dataset_query_service._load_catalog(source)


def test_datasource_routes_dataset_queries_without_sqlalchemy() -> None:
    source = SimpleNamespace(
        id="source-1",
        type="dataset",
        config={"dataset_version_id": "version-1"},
        connector_revision=1,
    )
    expected = {
        "columns": ["value"],
        "rows": [[1]],
        "row_count": 1,
        "truncated": False,
    }
    with patch.object(
        dataset_query_service, "run_query", return_value=expected
    ) as run, patch.object(datasource_service, "get_engine") as get_engine:
        result = datasource_service.run_parameterized_query(
            source,
            "SELECT :value AS value",
            {"value": 1},
            limit=5,
        )
    assert result == expected
    run.assert_called_once()
    get_engine.assert_not_called()


def test_medical_connection_accepts_dataset_source() -> None:
    class FakeDatasetConnection:
        def execute(self, sql, parameters):
            assert sql == "SELECT ? AS result"
            assert parameters == (7,)
            return ["result"], [(7,)]

        def close(self):
            return None

    source = SimpleNamespace(type="dataset", config={"dataset_version_id": "v1"})
    with patch.object(
        dataset_query_service,
        "open_connection",
        return_value=FakeDatasetConnection(),
    ):
        connection = medical_audit_service._AuditConnection(source)
        try:
            assert connection.execute("SELECT ? AS result", (7,)).fetchone() == {
                "result": 7
            }
        finally:
            connection.close()
