"""Create local visual-QA outputs from the checked-in artifact templates."""
from __future__ import annotations

import argparse
from pathlib import Path

from app.services.template_artifact_service import render_template


parser = argparse.ArgumentParser()
parser.add_argument("output_dir", type=Path)
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)

root = Path(__file__).parent
variables = {
    "project": {"name": "星河中心", "code": "PRJ-001"},
    "manager": {"name": "张三"},
    "report": {
        "date": "2026-08-22",
        "status": "正常",
        "summary": "结构施工按计划完成。",
        "risk": "连续降雨可能影响后续进度。",
    },
    "metrics": {
        "completion": 85,
        "contract_amount": 12_345_678.9,
        "budget": 1_000_000,
        "actual": 235_000,
    },
}
for source_name, output_name in (
    ("项目报告模板.docx", "星河中心项目报告.docx"),
    ("项目预算模板.xlsx", "星河中心项目预算.xlsx"),
    ("项目周报模板.md", "星河中心项目周报.md"),
):
    source = root / source_name
    rendered = render_template(
        source.name,
        source.read_bytes(),
        variables,
        output_filename=output_name,
    )
    (args.output_dir / rendered.filename).write_bytes(rendered.content)

