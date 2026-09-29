"""Read-only Excel discovery without an implicit full-sheet dimension scan."""
from __future__ import annotations

from openpyxl.reader.excel import ExcelReader
from openpyxl.cell.text import Text
from openpyxl.xml.constants import SHARED_STRINGS, SHEET_MAIN_NS
from openpyxl.xml.functions import iterparse
from openpyxl.worksheet._read_only import ReadOnlyWorksheet
from openpyxl.utils.cell import coordinate_to_tuple


_NS = "{" + SHEET_MAIN_NS + "}"


def _sample_string_indices(source, row_limit: int, column_limit: int) -> set[int]:
    """Find string references only in the physical rows profiling can consume."""
    needed: set[int] = set()
    events = iterparse(source, events=("start", "end"))
    _, root = next(events)
    ordinal = 0
    for event, node in events:
        if event != "end" or node.tag != _NS + "row":
            continue
        ordinal = int(node.get("r", ordinal + 1))
        if ordinal > row_limit:
            break
        for column, cell in enumerate(node, 1):
            cell_column = coordinate_to_tuple(cell.get("r"))[1] if cell.get("r") else column
            if cell_column > column_limit:
                raise ValueError("Excel 样本超过允许的列数")
            value = cell.find(_NS + "v")
            if cell.get("t") == "s" and value is not None and value.text is not None:
                needed.add(int(value.text))
        node.clear()
        root.clear()
    return needed


def _read_selected_strings(source, needed: set[int]) -> dict[int, str]:
    selected: dict[int, str] = {}
    events = iterparse(source, events=("start", "end"))
    _, root = next(events)
    index = 0
    remaining = set(needed)
    for event, node in events:
        if event != "end" or node.tag != _NS + "si":
            continue
        if index in remaining:
            # Preserve openpyxl's rich-text and escaped-string semantics.
            selected[index] = Text.from_tree(node).content.replace("x005F_", "")
            remaining.remove(index)
        node.clear()
        root.clear()
        if not remaining:
            break
        index += 1
    if remaining:
        raise ValueError("Excel 样本引用了不存在的共享字符串")
    return selected


class _ProfileWorksheet(ReadOnlyWorksheet):
    def _get_size(self):
        # openpyxl scans to </sheetData> when an exporter omitted <dimension>.
        # Discovery already bounds physical rows and columns; declared Excel
        # dimensions are neither needed nor trusted as exact record counts.
        self._min_column = 1
        self._min_row = 1
        self._max_column = None
        self._max_row = None


class _ProfileReader(ExcelReader):
    def read_strings(self):
        part = self.package.find(SHARED_STRINGS)
        self.strings_path = part.PartName[1:] if part is not None else None
        self.shared_strings = {}

    def read_worksheets(self):
        # Use openpyxl's value/date/shared-string parser, with local worksheet
        # instances. No global monkeypatch can affect concurrent full imports.
        sheets = [(sheet, relationship) for sheet, relationship in self.parser.find_sheets()
                  if relationship.target in self.valid_files and "chartsheet" not in relationship.Type]
        needed: set[int] = set()
        for _, relationship in sheets:
            with self.archive.open(relationship.target) as source:
                needed.update(_sample_string_indices(source, self.profile_row_limit, self.profile_column_limit))
        if needed:
            if self.strings_path is None:
                raise ValueError("Excel 缺少样本引用的共享字符串表")
            with self.archive.open(self.strings_path) as source:
                self.shared_strings = _read_selected_strings(source, needed)
        for sheet, relationship in sheets:
            if relationship.target not in self.valid_files or "chartsheet" in relationship.Type:
                continue
            worksheet = _ProfileWorksheet(self.wb, sheet.name, relationship.target, self.shared_strings)
            worksheet.sheet_state = sheet.state
            self.wb._sheets.append(worksheet)


def load_profile_workbook(source, *, row_limit: int, column_limit: int):
    """Caller must validate the Office container and close the returned workbook."""
    reader = _ProfileReader(source, read_only=True, data_only=True, keep_links=False, keep_vba=False)
    reader.profile_row_limit = row_limit
    reader.profile_column_limit = column_limit
    try:
        reader.read()
        return reader.wb
    except Exception:
        reader.archive.close()
        raise
