"""数据源服务：关系型数据库连接 + 文件桶管理。"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from ..config import BUCKETS_DIR, get_settings
from ..models import BucketFile, DataSource
from .policies import validate_read_only_sql

_engine_cache: dict[str, Engine] = {}


def _db_url(ds: DataSource) -> str:
    cfg = ds.config or {}
    if ds.type == "mysql":
        user = cfg.get("user", "root")
        pwd = cfg.get("password", "")
        host = cfg.get("host", "127.0.0.1")
        port = cfg.get("port", 3306)
        db = cfg.get("database", "")
        return f"mysql+pymysql://{user}:{pwd}@{host}:{port}/{db}?charset=utf8mb4"
    if ds.type == "postgres":
        user = cfg.get("user", "postgres")
        pwd = cfg.get("password", "")
        host = cfg.get("host", "127.0.0.1")
        port = cfg.get("port", 5432)
        db = cfg.get("database", "")
        return f"postgresql+psycopg2://{user}:{pwd}@{host}:{port}/{db}"
    if ds.type == "sqlite":
        path = cfg.get("path", "")
        return f"sqlite:///{path}"
    raise ValueError(f"未知数据库类型: {ds.type}")


def get_engine(ds: DataSource) -> Engine:
    key = f"{ds.id}:{json.dumps(ds.config, sort_keys=True, default=str)}"
    if key not in _engine_cache:
        _engine_cache[key] = create_engine(_db_url(ds), pool_pre_ping=True)
    return _engine_cache[key]


def invalidate_engine(ds: DataSource) -> None:
    """数据源配置变化后释放旧连接池，避免继续使用旧凭据或旧地址。"""
    prefix = f"{ds.id}:"
    for key, cached in list(_engine_cache.items()):
        if key.startswith(prefix):
            cached.dispose()
            _engine_cache.pop(key, None)


def test_connection(ds: DataSource) -> tuple[bool, str]:
    """测试数据库连接，返回 (ok, message)。"""
    try:
        engine = get_engine(ds)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "连接成功"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def list_tables(ds: DataSource) -> list[dict[str, Any]]:
    """列出数据库中的表及其列信息。"""
    engine = get_engine(ds)
    tables: list[dict[str, Any]] = []
    with engine.connect() as conn:
        if ds.type == "sqlite":
            rows = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            ).fetchall()
            names = [r[0] for r in rows]
        else:
            rows = conn.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema NOT IN ('information_schema','performance_schema','mysql','pg_catalog')")
            ).fetchall()
            names = [r[0] for r in rows]
        for name in names:
            cols: list[dict[str, Any]] = []
            try:
                if ds.type == "sqlite":
                    col_rows = conn.execute(text(f'PRAGMA table_info("{name}")')).fetchall()
                    for r in col_rows:
                        cols.append({"name": r[1], "type": r[2], "pk": bool(r[5])})
                else:
                    col_rows = conn.execute(
                        text(
                            "SELECT column_name, data_type FROM information_schema.columns "
                            "WHERE table_name = :t AND table_schema NOT IN ('information_schema','performance_schema','mysql','pg_catalog')"
                        ),
                        {"t": name},
                    ).fetchall()
                    cols = [{"name": r[0], "type": r[1], "pk": False} for r in col_rows]
            except Exception:  # noqa: BLE001
                cols = []
            row_count = -1
            try:
                row_count = conn.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar()
            except Exception:  # noqa: BLE001
                pass
            tables.append({"name": name, "columns": cols, "row_count": row_count})
    return tables


def run_query(ds: DataSource, sql: str, limit: int | None = None) -> dict[str, Any]:
    """执行单条只读 SQL 查询，返回列名与行数据。"""
    if ds.type == "file_bucket":
        raise ValueError("文件桶数据源不支持 SQL 查询")
    sql = validate_read_only_sql(sql)
    max_rows = get_settings().max_query_rows
    limit = max(1, min(int(limit or max_rows), max_rows))
    engine = get_engine(ds)
    with engine.connect() as conn:
        result = conn.execute(text(sql))
        columns = list(result.keys())
        rows = result.fetchmany(limit + 1)
        truncated = len(rows) > limit
        rows = rows[:limit]
        data = []
        for row in rows:
            data.append([_jsonable(v) for v in row])
        return {"columns": columns, "rows": data, "row_count": len(data), "truncated": truncated}


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


# ──────────────────────────────────────────────
# 文件桶
# ──────────────────────────────────────────────
def bucket_dir(ds: DataSource) -> Path:
    d = BUCKETS_DIR / ds.id
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_bucket_file(ds: DataSource, filename: str, content: bytes) -> BucketFile:
    """保存上传文件到文件桶目录。"""
    safe_name = Path(filename).name or f"file_{uuid.uuid4().hex[:8]}"
    target = bucket_dir(ds) / safe_name
    target.write_bytes(content)
    return BucketFile(
        data_source_id=ds.id,
        filename=safe_name,
        stored_path=str(target),
        size=len(content),
        mime=_guess_mime(safe_name),
        status="pending",
    )


def delete_bucket_file(bf: BucketFile) -> None:
    p = Path(bf.stored_path)
    if p.exists():
        p.unlink()


def _guess_mime(name: str) -> str:
    import mimetypes

    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def copy_skill_dir(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
