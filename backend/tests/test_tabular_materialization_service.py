from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import Workbook
import pyarrow.parquet as parquet
import pytest

from app.services import tabular_materialization_service as service


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        dataset_duckdb_memory_limit_bytes=64 * 1024 * 1024,
        dataset_duckdb_threads=1,
        dataset_duckdb_max_temp_directory_bytes=64 * 1024 * 1024,
        validation_dataset_materialization_timeout_seconds=60.0,
    )


def test_duckdb_excel_materialization_uses_profile_and_writes_typed_parquet(
    tmp_path,
) -> None:
    source = tmp_path / "source.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data'; SELECT 1;--"
    sheet.append(["Report title"])
    sheet.append(["claim_id", "amount", "approved"])
    sheet.append(["000001", "12.50", "yes"])
    sheet.append(["000002", "not-a-number", "no"])
    workbook.save(source)
    workbook.close()

    request = service.ExcelSheetRequest(
        name=sheet.title,
        header_row_index=1,
        fields=(
            service.ExcelField(name="claim_id", logical_type="string"),
            service.ExcelField(name="amount", logical_type="number"),
            service.ExcelField(name="approved", logical_type="boolean"),
        ),
        output_stem="relation-0001",
    )
    with patch.object(service, "get_settings", return_value=_settings()):
        results, engine = service.materialize_excel_workbook(
            source, tmp_path, [request]
        )

    fragment = results[0]
    assert fragment is not None
    table = parquet.read_table(fragment["path"])
    assert table.column_names == ["claim_id", "amount", "approved"]
    assert table.column("claim_id").to_pylist() == ["000001", "000002"]
    assert table.column("amount").to_pylist() == [12.5, None]
    assert table.column("approved").to_pylist() == [True, False]
    assert fragment["row_count"] == 2
    assert engine["name"] == "duckdb-excel"
    assert engine["duckdb_version"]


def test_missing_excel_extension_fails_closed_without_runtime_install(tmp_path) -> None:
    source = tmp_path / "source.xlsx"
    source.write_bytes(b"synthetic")

    class MissingExtensionConnection:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def execute(self, statement, parameters=None):
            self.statements.append(str(statement))
            if str(statement) == "LOAD excel":
                raise RuntimeError("not installed")
            return self

        def interrupt(self) -> None:
            return None

        def close(self) -> None:
            return None

    connection = MissingExtensionConnection()
    with (
        patch("duckdb.connect", return_value=connection),
        patch.object(service, "get_settings", return_value=_settings()),
        pytest.raises(
            service.TabularMaterializationError,
            match="未预装 DuckDB Excel 扩展",
        ),
    ):
        service.materialize_excel_workbook(source, tmp_path, [])

    assert not any(statement.startswith("INSTALL ") for statement in connection.statements)
