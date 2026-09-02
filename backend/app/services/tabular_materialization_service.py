"""Bounded native materialization of verified tabular assets."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import threading
from typing import Any, Sequence

from ..config import get_settings


class TabularMaterializationError(ValueError):
    pass


@dataclass(frozen=True)
class ExcelField:
    name: str
    logical_type: str


@dataclass(frozen=True)
class ExcelSheetRequest:
    name: str
    header_row_index: int
    fields: tuple[ExcelField, ...]
    output_stem: str


def _sql_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _excel_column_name(ordinal: int) -> str:
    if ordinal < 0:
        raise TabularMaterializationError("Excel 列序号无效")
    value = ordinal + 1
    output = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        output = chr(65 + remainder) + output
    return output


def _source_value(identifier: str) -> str:
    source = f"CAST({_quote_identifier(identifier)} AS VARCHAR)"
    return f"NULLIF(TRIM({source}), '')"


def _field_expression(source_identifier: str, field: ExcelField) -> str:
    value = _source_value(source_identifier)
    logical_type = field.logical_type
    if logical_type == "integer":
        expression = f"TRY_CAST(TRUNC(TRY_CAST({value} AS DECIMAL(38, 10))) AS BIGINT)"
    elif logical_type == "number":
        expression = f"TRY_CAST({value} AS DOUBLE)"
    elif logical_type == "boolean":
        expression = (
            f"CASE WHEN LOWER({value}) IN ('true', '1', 'yes', 'y', '是') THEN TRUE "
            f"WHEN LOWER({value}) IN ('false', '0', 'no', 'n', '否') THEN FALSE "
            "ELSE NULL END"
        )
    elif logical_type == "date":
        expression = f"TRY_CAST({value} AS DATE)"
    elif logical_type == "datetime":
        expression = f"TRY_CAST({value} AS TIMESTAMP)"
    else:
        expression = value
    return f"{expression} AS {_quote_identifier(field.name)}"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _configure_connection(connection: Any, work_dir: Path) -> dict[str, str]:
    settings = get_settings()
    memory_limit = int(settings.dataset_duckdb_memory_limit_bytes)
    threads = int(settings.dataset_duckdb_threads)
    max_temp_bytes = int(settings.dataset_duckdb_max_temp_directory_bytes)
    spill_dir = (work_dir / "duckdb-spill").resolve()
    spill_dir.mkdir(parents=True, exist_ok=True)
    connection.execute("SET autoinstall_known_extensions = false")
    connection.execute("SET autoload_known_extensions = false")
    try:
        connection.execute("LOAD excel")
    except Exception as exc:  # noqa: BLE001 - deployment guard is intentionally stable.
        raise TabularMaterializationError(
            "服务端未预装 DuckDB Excel 扩展，不能高效物化 XLSX/XLSM"
        ) from exc
    connection.execute("SET memory_limit = ?", [f"{memory_limit}B"])
    connection.execute("SET threads = ?", [threads])
    connection.execute("SET temp_directory = ?", [str(spill_dir)])
    connection.execute("SET max_temp_directory_size = ?", [f"{max_temp_bytes}B"])
    connection.execute("SET preserve_insertion_order = false")
    connection.execute("SET enable_progress_bar = false")
    connection.execute("SET allow_persistent_secrets = false")
    extension = connection.execute(
        "SELECT extension_version FROM duckdb_extensions() "
        "WHERE extension_name = 'excel' AND loaded"
    ).fetchone()
    import duckdb

    return {
        "name": "duckdb-excel",
        "duckdb_version": str(duckdb.__version__),
        "extension_version": str(extension[0] or "") if extension else "",
    }


def _copy_sheet(
    connection: Any,
    source_path: Path,
    output_dir: Path,
    request: ExcelSheetRequest,
) -> dict[str, Any] | None:
    if not request.fields:
        raise TabularMaterializationError("Excel 工作表没有可物化字段")
    first_row = request.header_row_index + 2
    if not 2 <= first_row <= 1_048_576:
        raise TabularMaterializationError("Excel 表头位置无效")
    excel_columns = [_excel_column_name(index) for index in range(len(request.fields))]
    final_column = excel_columns[-1]
    cell_range = f"A{first_row}:{final_column}1048576"
    source_clause = (
        f"read_xlsx({_sql_literal(source_path)}, sheet={_sql_literal(request.name)}, "
        f"header=false, all_varchar=true, range={_sql_literal(cell_range)})"
    )
    described = connection.execute(
        f"DESCRIBE SELECT * FROM {source_clause}"
    ).fetchall()
    source_columns = [str(row[0]) for row in described[: len(request.fields)]]
    if len(source_columns) != len(request.fields):
        raise TabularMaterializationError("Excel 工作表列数与已验证 profile 不一致")
    projections = ", ".join(
        _field_expression(source_column, field)
        for source_column, field in zip(source_columns, request.fields, strict=True)
    )
    nonempty = " OR ".join(
        f"{_source_value(source_column)} IS NOT NULL"
        for source_column in source_columns
    )
    destination = (output_dir / f"{request.output_stem}.parquet").resolve()
    if destination.parent != output_dir.resolve():
        raise TabularMaterializationError("Parquet 输出路径越界")
    statement = (
        f"COPY (SELECT {projections} FROM {source_clause} "
        f"WHERE {nonempty}) TO {_sql_literal(destination)} "
        "(FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    connection.execute(statement)
    row = connection.execute(
        "SELECT count(*) FROM read_parquet(?)", [str(destination)]
    ).fetchone()
    row_count = int(row[0] if row else 0)
    if row_count <= 0:
        destination.unlink(missing_ok=True)
        return None
    return {
        "path": destination,
        "row_count": row_count,
        "byte_size": destination.stat().st_size,
        "content_sha256": _file_sha256(destination),
    }


def materialize_excel_workbook(
    source_path: Path,
    output_dir: Path,
    requests: Sequence[ExcelSheetRequest],
) -> tuple[list[dict[str, Any] | None], dict[str, str]]:
    """Convert selected sheets directly to Parquet under bounded DuckDB settings."""
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - deployment dependency guard
        raise TabularMaterializationError("服务端缺少 DuckDB 物化引擎") from exc
    source = source_path.resolve(strict=True)
    destination = output_dir.resolve(strict=True)
    connection = duckdb.connect(":memory:")
    timed_out = threading.Event()
    timeout_seconds = float(
        getattr(get_settings(), "validation_dataset_materialization_timeout_seconds", 3600.0)
    )

    def interrupt() -> None:
        timed_out.set()
        connection.interrupt()

    timer = threading.Timer(timeout_seconds, interrupt)
    timer.daemon = True
    timer.start()
    try:
        engine = _configure_connection(connection, destination)
        results = [
            _copy_sheet(connection, source, destination, request)
            for request in requests
        ]
        if timed_out.is_set():
            raise TabularMaterializationError("Excel 物化超时")
        return results, engine
    except TabularMaterializationError:
        raise
    except Exception as exc:  # noqa: BLE001 - do not expose paths or engine details.
        if timed_out.is_set():
            raise TabularMaterializationError("Excel 物化超时") from exc
        raise TabularMaterializationError("Excel 文件物化失败") from exc
    finally:
        timer.cancel()
        connection.close()
