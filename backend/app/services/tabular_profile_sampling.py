"""Bounded structural discovery; exact totals belong to full materialization."""
from __future__ import annotations

from itertools import islice
from typing import Any, Callable, Iterable


def sample_rows(
    values: Iterable[Iterable[Any]],
    *,
    sample_limit: int,
    column_limit: int,
    header_search_rows: int,
    has_value: Callable[[Any], bool],
) -> tuple[int, list[Any], list[list[Any]], bool, int | None] | None:
    # Bound physical rows too: a huge blank prefix must not turn structural
    # discovery into a scan of the complete workbook. One lookahead distinguishes
    # a complete small table from a sample without trusting Excel dimensions.
    scan_limit = sample_limit + header_search_rows + 1
    observed: list[tuple[int, list[Any]]] = []
    scanned = 0
    for index, raw in enumerate(islice(values, scan_limit)):
        scanned += 1
        row = list(islice(raw, column_limit + 1))
        if len(row) > column_limit:
            raise ValueError(f"表格列数不能超过 {column_limit}")
        if any(has_value(value) for value in row):
            observed.append((index, row))
    exhausted = scanned < scan_limit
    if not observed:
        if not exhausted:
            raise ValueError("表格前部没有可识别的表头，请移除大段空行后重新上传")
        return None
    header_position = max(
        range(min(header_search_rows, len(observed))),
        key=lambda index: sum(has_value(value) for value in observed[index][1]),
    )
    header_index, header = observed[header_position]
    records = observed[header_position + 1:]
    sample = [row for _, row in records[:sample_limit]]
    return header_index, header, sample, not exhausted or len(records) > len(sample), (
        len(records) if exhausted else None
    )
