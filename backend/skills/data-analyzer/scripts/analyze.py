"""data-analyzer 技能入口：对 CSV/TSV 做描述性统计与分组聚合。"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


def _is_number(v: str) -> bool:
    try:
        float(v)
        return True
    except (ValueError, TypeError):
        return False


def load_rows(path: Path) -> tuple[list[str], list[dict]]:
    delim = ","
    with path.open("r", encoding="utf-8-sig", errors="replace") as f:
        sample = f.read(4096)
        f.seek(0)
        if sample.count("\t") > sample.count(","):
            delim = "\t"
        reader = csv.DictReader(f, delimiter=delim)
        fieldnames = reader.fieldnames or []
        rows = [dict(r) for r in reader]
    return fieldnames, rows


def describe(rows: list[dict]) -> dict:
    stats: dict[str, dict] = {}
    for col in {k for r in rows for k in r}:
        vals = [r.get(col, "") for r in rows if r.get(col, "") != ""]
        nums = [float(v) for v in vals if _is_number(v)]
        if nums:
            stats[col] = {
                "type": "numeric",
                "count": len(nums),
                "mean": round(sum(nums) / len(nums), 4),
                "min": min(nums),
                "max": max(nums),
                "sum": round(sum(nums), 4),
            }
        else:
            uniq = sorted({str(v) for v in vals})
            stats[col] = {"type": "categorical", "count": len(vals), "unique": len(uniq), "top": uniq[:10]}
    return stats


def group_agg(rows: list[dict], group_by: str, metric: str) -> list[dict]:
    groups: dict[str, list[float]] = {}
    for r in rows:
        key = str(r.get(group_by, ""))
        v = r.get(metric, "")
        if _is_number(v):
            groups.setdefault(key, []).append(float(v))
    out = []
    for key in sorted(groups):
        vals = groups[key]
        out.append(
            {
                group_by: key,
                "count": len(vals),
                "sum": round(sum(vals), 4),
                "mean": round(sum(vals) / len(vals), 4),
                "min": min(vals),
                "max": max(vals),
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="CSV/TSV 数据分析")
    parser.add_argument("--path", required=True, help="CSV/TSV 文件路径")
    parser.add_argument("--group-by", help="分组列")
    parser.add_argument("--metric", help="聚合数值列")
    parser.add_argument("--format", choices=["text", "json"], default="json")
    args = parser.parse_args()

    path = Path(args.path)
    if not path.exists():
        print(json.dumps({"status": "error", "message": f"文件不存在: {path}"}, ensure_ascii=False))
        return 1

    try:
        fieldnames, rows = load_rows(path)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "error", "message": f"读取失败: {exc}"}, ensure_ascii=False))
        return 1

    if args.group_by and args.metric:
        result = {"status": "success", "group_by": args.group_by, "metric": args.metric, "rows": group_agg(rows, args.group_by, args.metric)}
    else:
        result = {"status": "success", "columns": fieldnames, "row_count": len(rows), "stats": describe(rows)}

    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
