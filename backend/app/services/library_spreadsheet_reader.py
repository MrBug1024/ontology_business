"""Read actual managed workbook cells in a resource-bounded trusted child."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from zipfile import ZipFile

from minio.error import S3Error
from urllib3.exceptions import HTTPError

from ..distillation_sample_schemas import DatabaseSampleArguments


MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_OUTPUT_BYTES = 180_000
MAX_SCAN_ROWS = 10_000
MAX_SECONDS = 35


def inspect_path(path: Path, args: DatabaseSampleArguments) -> dict:
    from . import library_sample_query as samples, library_xlsx_stream as xlsx

    with ZipFile(path) as archive:
        members = archive.infolist()
        if not members or len(members) > 2000:
            raise ValueError("表格容器条目数量超过边界")
        expanded = sum(item.file_size for item in members)
        compressed = sum(item.compress_size for item in members)
        if expanded > 4 * 1024**3 or expanded > max(compressed, 1) * 150:
            raise ValueError("表格解压大小超过边界")
        if any(item.flag_bits & 1 or ".." in Path(item.filename).parts for item in members):
            raise ValueError("表格容器无效")
    with ZipFile(path) as archive:
        sheets, epoch = xlsx.worksheets(archive)
        dates = xlsx.date_styles(archive)
        tables, positions = [], {}
        for sheet in sheets:
            preview = [(number, row) for number, row in xlsx.rows(archive, sheet["path"], 25, dates, epoch)
                if any(value is not None for value in row)]
            # Match library profiling: skip merged title rows by choosing the
            # earliest row with the most populated cells in the bounded preview.
            if preview:
                index, row = max(preview, key=lambda item: sum(value is not None for value in item[1]))
                last = max(i for i, value in enumerate(row) if value is not None)
                names = [str(value).strip() if value is not None else f"未命名列{i+1}"
                    for i, value in enumerate(row[:last+1])]
                if any(len(name) > 200 for name in names) or len(names) != len(set(names)):
                    raise ValueError("表头过长或重复，无法确定字段身份")
                tables.append({"name": sheet["name"], "columns": [{"name": name, "type": "text"} for name in names]})
                positions[sheet["name"]] = index
        available = samples.catalog(tables)
        selected = next((table for table in available if table["table_key"] == args.table_key), None)
        if args.table_key is None and len(available) == 1:
            selected = available[0]
        if selected is None:
            if args.table_key is not None:
                raise ValueError("工作表引用已变化，请重新读取目录")
            return {"kind": "database_catalog", "tables": available,
                "limitations": ["请选择 table_key 再读取工作表真实单元格。"]}
        fields = {field["field_key"]: field for field in selected["fields"]}
        chosen = args.field_keys or list(fields)[:20]
        if not chosen or len(set(chosen)) != len(chosen) or set(chosen).difference(fields):
            raise ValueError("样本字段引用无效")
        if any(item.field_key not in fields for item in args.filters):
            raise ValueError("筛选字段引用无效")
        table = next(table for table in tables if table["name"] == selected["name"])
        indexes = {field["field_key"]: next(i for i, column in enumerate(table["columns"])
            if column["name"] == field["name"]) for field in selected["fields"]}
        sheet = next(item for item in sheets if item["name"] == selected["name"])
        start = positions[sheet["name"]] + 1
        rows, source_rows = [], []
        scanned = 0
        scan_limit = MAX_SCAN_ROWS if args.filters else args.limit + 26
        for number, row in xlsx.rows(archive, sheet["path"], scan_limit, dates, epoch):
            if number < start:
                continue
            scanned += 1
            values = {key: None if index >= len(row) or row[index] is None else str(row[index])[:1001]
                for key, index in indexes.items()}
            if not any(value is not None for value in values.values()):
                continue
            if not all(_matches(values[item.field_key], item.operator, item.value) for item in args.filters):
                continue
            rows.append([values[key] for key in chosen])
            source_rows.append(number)
            if len(rows) > args.limit:
                break
        result = samples.result(rows, {**selected, "fields": [fields[key] for key in chosen],
            "ordered_by_primary_key": False}, args)
        result.update(source_format="xlsx", source_rows=source_rows[:args.limit], scanned_rows=scanned,
            scan_limit_reached=scanned >= MAX_SCAN_ROWS - 25, header_row=positions[sheet["name"]], tables=available)
        result["limitations"] = ["直接读取已校验原文件的缓存单元格值；不执行公式或宏。source_rows 为 Excel 行号。",
            "按工作表顺序有界抽样，最多扫描 10000 行；筛选按文本比较，不代表全量或随机样本。",
            "按前 25 个非空行识别表头，header_row 给出所选行号；需核对标题行/合并单元格。超长和疑似凭据单元格已排除。"]
        return result


def _matches(value: str | None, operator: str, expected: str) -> bool:
    if value is None or len(value) > 1000:
        return False
    return {"eq": value == expected, "gte": value >= expected, "lte": value <= expected}[operator]


def parse_staged(path: Path, args: DatabaseSampleArguments) -> dict:
    environment = {key: value for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "LANG", "LC_ALL"}}
    environment.update(PYTHONPATH=str(Path(__file__).resolve().parents[2]), PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1")
    try:
        completed = subprocess.run([sys.executable, "-m", "app.services.library_spreadsheet_reader"],
            input=json.dumps({"path": str(path), "arguments": args.model_dump()}, ensure_ascii=False).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=MAX_SECONDS, env=environment,
            cwd=Path(__file__).resolve().parents[2], check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("表格读取超时，请缩小文件或样本范围") from exc
    if completed.returncode or len(completed.stdout) > MAX_OUTPUT_BYTES:
        raise ValueError("表格解析或资源边界检查未通过，请核对格式或缩小样本范围")
    return json.loads(completed.stdout)


def read_snapshot(snapshot: dict, args: DatabaseSampleArguments) -> dict:
    from . import library_object_reader

    if not 0 < snapshot["size"] <= MAX_FILE_BYTES or len(snapshot["sha256"] or "") != 64:
        raise ValueError("文件缺少受管内容身份或超过 512 MB")
    if not snapshot["filename"].lower().endswith((".xlsx", ".xlsm")):
        raise ValueError("当前样本工具支持 XLSX/XLSM；其他文档请使用资料阅读工具")
    with tempfile.TemporaryDirectory(prefix="ontology-spreadsheet-") as directory:
        path = Path(directory) / "sample.xlsx"
        try:
            size = library_object_reader.download_snapshot(snapshot["bucket"], snapshot["key"], path,
                version_id=snapshot["version"], max_bytes=MAX_FILE_BYTES, timeout_seconds=90)
        except (TimeoutError, OSError, S3Error, HTTPError) as exc:
            raise ValueError("受管文件下载失败或超过 90 秒，请稍后重试或缩小文件") from exc
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if size != snapshot["size"] or digest != snapshot["sha256"]:
            raise ValueError("受管文件内容已变化，请重新读取")
        return parse_staged(path, args)


def main() -> int:
    from .distillation_parse_limits import restrict_process

    try:
        restrict_process()
        raw = sys.stdin.buffer.read(32_769)
        if len(raw) > 32_768:
            return 1
        request = json.loads(raw)
        path = Path(request["path"])
        resolved = path.resolve(strict=True)
        root = Path(tempfile.gettempdir()).resolve()
        if (path.is_symlink() or not resolved.is_relative_to(root)
                or not resolved.parent.name.startswith("ontology-spreadsheet-")
                or resolved.name != "sample.xlsx" or not 0 < resolved.stat().st_size <= MAX_FILE_BYTES):
            return 1
        output = json.dumps(inspect_path(resolved, DatabaseSampleArguments.model_validate(request["arguments"])),
            ensure_ascii=False, allow_nan=False).encode()
        if len(output) > MAX_OUTPUT_BYTES:
            return 1
        sys.stdout.buffer.write(output)
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
