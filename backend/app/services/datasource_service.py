"""数据源服务：关系型数据库连接 + 文件桶管理。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..config import BUCKETS_DIR, get_settings
from ..models import BucketFile, DataSource
from .policies import validate_read_only_sql

_engine_cache: dict[str, Engine] = {}

# Database driver exceptions commonly include a full DSN (and can therefore
# disclose a password, token, hostname or database name).  Connection-test
# callers intentionally receive this stable, non-diagnostic public message.
CONNECTION_TEST_FAILURE_MESSAGE = "连接测试失败，请检查数据源配置和网络可达性"


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
    except Exception:  # noqa: BLE001 - never expose driver diagnostics or DSNs.
        return False, CONNECTION_TEST_FAILURE_MESSAGE


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


def run_query(
    ds: DataSource,
    sql: str,
    limit: int | None = None,
    *,
    max_rows: int | None = None,
) -> dict[str, Any]:
    """执行单条只读 SQL 查询，返回列名与行数据。

    ``max_rows`` is an internal caller ceiling.  Mapping refresh jobs have a
    separately bounded batch size (currently 500), while interactive Agent
    queries must continue to use the lower application-wide query limit.
    """
    if ds.type == "file_bucket":
        raise ValueError("文件桶数据源不支持 SQL 查询")
    sql = validate_read_only_sql(sql)
    configured_max_rows = get_settings().max_query_rows
    caller_max_rows = configured_max_rows if max_rows is None else max(1, int(max_rows))
    limit = max(1, min(int(limit or caller_max_rows), caller_max_rows))
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


def run_parameterized_query(
    ds: DataSource,
    sql: str,
    parameters: Mapping[str, Any],
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """Execute one server-generated read-only statement with bound values.

    This is intentionally separate from ``run_query``: callers on the semantic
    path must never interpolate model/user filter values into SQL text.  The
    statement still passes the shared read-only policy so a future compiler
    regression fails closed before reaching a connector.
    """
    if ds.type == "file_bucket":
        raise ValueError("文件桶数据源不支持 SQL 查询")
    statement = validate_read_only_sql(sql)
    if not isinstance(parameters, Mapping) or any(
        not isinstance(key, str) or not key for key in parameters
    ):
        raise ValueError("参数化查询的绑定参数无效")
    max_rows = get_settings().max_query_rows
    resolved_limit = max(1, min(int(limit or max_rows), max_rows))
    engine = get_engine(ds)
    with engine.connect() as conn:
        result = conn.execute(text(statement), dict(parameters))
        columns = list(result.keys())
        rows = result.fetchmany(resolved_limit + 1)
        truncated = len(rows) > resolved_limit
        data = [
            [_jsonable(value) for value in row]
            for row in rows[:resolved_limit]
        ]
        return {
            "columns": columns,
            "rows": data,
            "row_count": len(data),
            "truncated": truncated,
        }


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


_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_OFFICE_MIMES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".md": "text/markdown; charset=utf-8",
    ".markdown": "text/markdown; charset=utf-8",
}


def validate_bucket_filename(filename: str) -> str:
    """Validate a user-visible filename instead of silently stripping a path.

    Silent ``Path(...).name`` normalisation hides traversal attempts and makes
    audit logs disagree with the user's request.  File-bucket APIs therefore
    accept a basename only and fail closed on platform-reserved names.
    """
    name = str(filename or "").strip()
    if not name or len(name) > 240:
        raise ValueError("文件名必须是 1 到 240 个字符")
    if name in {".", ".."} or Path(name).name != name or "/" in name or "\\" in name:
        raise ValueError("文件名不能包含目录或路径穿越字符")
    if any(ord(char) < 32 for char in name) or any(char in '<>:"|?*' for char in name):
        raise ValueError("文件名包含不安全字符")
    if name.endswith((" ", ".")):
        raise ValueError("文件名不能以空格或句点结尾")
    if Path(name).stem.upper() in _WINDOWS_RESERVED_NAMES:
        raise ValueError("文件名使用了系统保留名称")
    return name


def _bucket_name_candidates(requested_name: str):
    path = Path(requested_name)
    yield requested_name
    for index in range(2, 10_000):
        yield f"{path.stem} ({index}){path.suffix}"


def save_bucket_file(
    ds: DataSource,
    filename: str,
    content: bytes,
    *,
    mime: str | None = None,
    stable_file_id: str | None = None,
) -> BucketFile:
    """Save a file inside one file-bucket root.

    Ordinary uploads keep their collision-resistant visible-name behavior.
    Confirmed template executions may provide a stable 32-hex id: their bytes
    are atomically written below a private per-execution directory.  If the
    process dies after the filesystem write but before the database commit, a
    retry reuses the same path/id instead of leaking ``name (2).ext`` files.
    """
    if ds.type != "file_bucket":
        raise ValueError("只有文件桶数据源可以保存文件")
    if not isinstance(content, bytes):
        raise ValueError("文件内容必须是字节数据")
    requested_name = validate_bucket_filename(filename)
    root = bucket_dir(ds)
    storage_root = BUCKETS_DIR.resolve()
    if root.is_symlink():
        raise ValueError("文件桶存储目录不能是符号链接")
    try:
        root.resolve(strict=True).relative_to(storage_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("文件桶存储目录越界") from exc
    if stable_file_id is not None:
        stable_file_id = str(stable_file_id).lower()
        if not re.fullmatch(r"[a-f0-9]{32}", stable_file_id):
            raise ValueError("稳定文件标识必须是 32 位十六进制字符串")
        generated_root = root / ".generated"
        if generated_root.is_symlink():
            raise ValueError("生成附件目录不能是符号链接")
        generated_root.mkdir(parents=True, exist_ok=True)
        directory = generated_root / stable_file_id
        if directory.is_symlink():
            raise ValueError("生成附件执行目录不能是符号链接")
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory = directory.resolve(strict=True)
            directory.relative_to(root.resolve(strict=True))
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("生成附件存储目录越界") from exc
        target = directory / requested_name
        pending = directory / f".pending-{uuid.uuid4().hex}"
        try:
            descriptor = os.open(
                pending,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(pending, target)
            # A prior process may have died after writing its private temp
            # file. Once this stable retry succeeds, those same-execution
            # leftovers are safe to remove without touching the result.
            try:
                for leftover in directory.glob(".pending-*"):
                    if leftover != pending and not leftover.is_symlink() and leftover.is_file():
                        try:
                            leftover.unlink()
                        except OSError:
                            pass
            except OSError:
                pass
        except Exception:
            if pending.exists():
                pending.unlink()
            raise
        return BucketFile(
            id=stable_file_id,
            data_source_id=ds.id,
            filename=requested_name,
            stored_path=str(target),
            size=len(content),
            mime=mime or _guess_mime(requested_name),
            content_sha256=hashlib.sha256(content).hexdigest(),
            status="pending",
        )

    directory = root
    target: Path | None = None
    safe_name = requested_name
    for candidate_name in _bucket_name_candidates(requested_name):
        candidate = directory / candidate_name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(candidate, flags, 0o600)
        except FileExistsError:
            continue
        target = candidate
        safe_name = candidate_name
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            if candidate.exists():
                candidate.unlink()
            raise
        break
    if target is None:
        raise ValueError("同名文件过多，请更换文件名")
    return BucketFile(
        id=uuid.uuid4().hex,
        data_source_id=ds.id,
        filename=safe_name,
        stored_path=str(target),
        size=len(content),
        mime=mime or _guess_mime(safe_name),
        content_sha256=hashlib.sha256(content).hexdigest(),
        status="pending",
    )


def validate_bucket_file_for_download(
    bf: BucketFile,
    ds: DataSource,
) -> tuple[Path, int, str]:
    """Resolve and verify a persisted attachment before serving its bytes."""
    if bf.data_source_id != ds.id or ds.type != "file_bucket":
        raise ValueError("附件不属于指定文件桶")
    safe_name = validate_bucket_filename(bf.filename)
    root = bucket_dir(ds).resolve()
    try:
        path = Path(bf.stored_path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FileNotFoundError("文件已丢失") from exc
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("附件存储路径越界") from exc
    if not path.is_file() or path.name != safe_name:
        raise ValueError("附件存储路径与文件记录不一致")
    actual_size = path.stat().st_size
    if bf.size > 0 and actual_size != bf.size:
        raise ValueError("附件大小与文件记录不一致")
    if bf.content_sha256:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != bf.content_sha256:
            raise ValueError("附件内容哈希与文件记录不一致")
    canonical_mime = _OFFICE_MIMES.get(path.suffix.lower())
    recorded_mime = str(bf.mime or "").split(";", 1)[0].strip().lower()
    canonical_base = str(canonical_mime or "").split(";", 1)[0].strip().lower()
    # Historic seed/import paths stored Office files as octet-stream and
    # Markdown without an explicit charset.  Treat those as compatible while
    # still rejecting a contradictory typed MIME (for example XLSX bytes
    # recorded as DOCX).
    compatible_legacy = recorded_mime in {"", "application/octet-stream"}
    if canonical_mime and not compatible_legacy and recorded_mime != canonical_base:
        raise ValueError("附件 MIME 与文件格式不一致")
    return path, actual_size, canonical_mime or bf.mime or _guess_mime(safe_name)


def delete_bucket_file(bf: BucketFile) -> None:
    delete_bucket_file_path(bf.stored_path)


def delete_bucket_file_path(stored_path: str) -> None:
    """Safely unlink a captured bucket path after its DB row is committed away."""
    # Deletion callers already authorise the owning data source.  Avoid
    # following a corrupt/symlinked record outside the platform bucket root.
    p = Path(stored_path)
    try:
        resolved = p.resolve(strict=True)
        resolved.relative_to(BUCKETS_DIR.resolve())
    except (OSError, RuntimeError, ValueError):
        return
    if resolved.is_file():
        resolved.unlink()


def reconcile_generated_file_orphans(
    db: Session,
    *,
    older_than_seconds: int = 24 * 60 * 60,
) -> int:
    """Remove only stale private generation dirs without BucketFile metadata.

    Stable template output lives under ``<bucket>/.generated/<32-hex-id>``.
    A hard process exit can occur after the atomic file replace but before the
    database commit.  Startup reconciliation leaves recent directories alone
    (another worker may still be committing), keeps every id referenced by a
    BucketFile row, and refuses unexpected nesting/symlinks before deletion.
    Ordinary user-upload paths are never considered.
    """
    root = BUCKETS_DIR.resolve()
    if not root.exists() or not root.is_dir():
        return 0
    cutoff = time.time() - max(0, int(older_than_seconds))
    candidates: list[tuple[str, Path]] = []
    for source_dir in root.iterdir():
        generated_root = source_dir / ".generated"
        if source_dir.is_symlink() or not generated_root.is_dir() or generated_root.is_symlink():
            continue
        try:
            generated_root.resolve(strict=True).relative_to(root)
        except (OSError, RuntimeError, ValueError):
            continue
        for execution_dir in generated_root.iterdir():
            if (
                execution_dir.is_symlink()
                or not execution_dir.is_dir()
                or not re.fullmatch(r"[a-f0-9]{32}", execution_dir.name)
            ):
                continue
            try:
                children = list(execution_dir.iterdir())
                # Renderer directories are deliberately flat.  Skipping an
                # unexpected child is safer than recursively following it.
                if any(child.is_symlink() or not child.is_file() for child in children):
                    continue
                newest = max(
                    [execution_dir.stat().st_mtime, *(child.stat().st_mtime for child in children)]
                )
            except OSError:
                continue
            if newest <= cutoff:
                candidates.append((execution_dir.name, execution_dir))
    if not candidates:
        return 0

    candidate_ids = {item[0] for item in candidates}
    persisted: set[str] = set()
    ordered_ids = sorted(candidate_ids)
    for offset in range(0, len(ordered_ids), 500):
        persisted.update(
            str(item)
            for item in db.scalars(
                select(BucketFile.id).where(BucketFile.id.in_(ordered_ids[offset : offset + 500]))
            ).all()
        )

    removed = 0
    for file_id, directory in candidates:
        if file_id in persisted:
            continue
        try:
            resolved = directory.resolve(strict=True)
            resolved.relative_to(root)
            children = list(resolved.iterdir())
            if any(child.is_symlink() or not child.is_file() for child in children):
                continue
            if max([resolved.stat().st_mtime, *(child.stat().st_mtime for child in children)]) > cutoff:
                continue
            for child in children:
                child.unlink()
            resolved.rmdir()
            removed += 1
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            # Another startup worker may have completed the same cleanup.
            continue
    return removed


def _guess_mime(name: str) -> str:
    import mimetypes

    return _OFFICE_MIMES.get(Path(name).suffix.lower()) or mimetypes.guess_type(name)[0] or "application/octet-stream"


def copy_skill_dir(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
