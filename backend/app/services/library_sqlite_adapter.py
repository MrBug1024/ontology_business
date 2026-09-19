"""Bounded inspection of managed SQLite snapshots; no path from callers."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import tempfile
import time

from ..models import DataSource
from . import datasource_service, library_object_reader


MAX_SQLITE_BYTES = 32 * 1024 * 1024
MAX_TABLES = 40
MAX_COLUMNS = 80
SQLITE_SIGNATURE = b"SQLite format 3\x00"


def inspect_path(path: Path, *, timeout_seconds: float = 5, sample=None) -> list[dict] | dict:
    """The private caller supplies a server-created staging path, never a DTO."""
    if path.is_symlink() or not path.is_file() or not 100 <= path.stat().st_size <= MAX_SQLITE_BYTES:
        raise ValueError("SQLite3 文件为空、无效或超过 32 MB")
    with path.open("rb") as handle:
        if handle.read(16) != SQLITE_SIGNATURE:
            raise ValueError("文件不是有效的 SQLite3 数据库快照")
    deadline = time.monotonic() + min(5, timeout_seconds)
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1)
    try:
        connection.enable_load_extension(False)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA cache_size=-2048")
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1024 * 1024)
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 4096)
        connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ}
        def authorize(action: int, first: str | None, second: str | None, _db: str | None, _trigger: str | None) -> int:
            if action in allowed or (action == sqlite3.SQLITE_PRAGMA and first == "table_info") or (action == sqlite3.SQLITE_FUNCTION and second in {"like", "substr"}):
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        connection.set_authorizer(authorize)
        # Views/virtual tables may invoke extension code or functions; this
        # snapshot adapter enumerates ordinary persisted tables only.
        names = connection.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' AND sql NOT LIKE 'CREATE VIRTUAL%' ORDER BY name LIMIT 41").fetchall()
        if len(names) > MAX_TABLES:
            raise ValueError("SQLite3 超过 40 张表，请缩小快照范围")
        tables = []
        for (name,) in names:
            if time.monotonic() >= deadline:
                raise TimeoutError("metadata deadline")
            quoted = '"' + name.replace('"', '""') + '"'
            columns = connection.execute(f"PRAGMA table_info({quoted})").fetchmany(MAX_COLUMNS + 1)
            if len(columns) > MAX_COLUMNS:
                raise ValueError("SQLite3 资料表超过 80 列")
            tables.append({"name": name, "row_count": -1, "columns": [
                {"name": column[1], "type": column[2], "pk": bool(column[5])} for column in columns]})
        if sample is not None:
            from .library_sample_query import query, result
            statement, values, metadata = query(tables, sample, "sqlite3")
            return result(connection.execute(statement, values).fetchmany(sample.limit + 1), metadata, sample)
        return tables
    finally:
        connection.close()


def sqlite_schema(source: DataSource, *, timeout_seconds: float = 20, sample=None) -> list[dict] | dict:
    files = list(source.files)
    if len(files) != 1:
        raise ValueError("请先上传一个 SQLite3 数据库快照")
    file = files[0]
    if (source.config or {}).get("snapshot_file_id") != file.id or (source.config or {}).get("snapshot_sha256") != file.content_sha256:
        raise ValueError("SQLite3 快照与资料库记录不一致")
    if not 100 <= file.size <= MAX_SQLITE_BYTES or len(file.content_sha256 or "") != 64:
        raise ValueError("SQLite3 快照缺少有效内容身份")
    bucket, key, _name = datasource_service.minio_file_identity(file, source)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ontology-sqlite-") as directory:
        path = Path(directory) / "snapshot.sqlite3"
        size = library_object_reader.download_snapshot(bucket, key, path,
            version_id=file.object_version_id or "", max_bytes=MAX_SQLITE_BYTES, timeout_seconds=timeout_seconds)
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if size != file.size or digest != file.content_sha256:
            raise ValueError("SQLite3 快照内容身份校验失败")
        remaining = timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("metadata deadline")
        return inspect_path(path, timeout_seconds=remaining, sample=sample)
