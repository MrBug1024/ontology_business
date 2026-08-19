"""Skill 服务：扫描、安装、启用/禁用、执行技能包。

Skill 是一个目录，包含 SKILL.md（说明）与实现脚本（scripts/）。
执行时以子进程方式运行技能入口脚本，捕获 stdout/stderr 返回给 Agent。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from ..config import SKILLS_DIR, get_settings
from ..models import Skill


def _parse_frontmatter(text: str) -> dict[str, Any]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1))
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError:
        return {}


def _find_entry(skill_dir: Path) -> str | None:
    """定位技能入口脚本。"""
    scripts = skill_dir / "scripts"
    if scripts.is_dir():
        for cand in ("parse.py", "main.py", "run.py", "cli.py"):
            if (scripts / cand).exists():
                return str(scripts / cand)
        pys = sorted(scripts.glob("*.py"))
        if pys:
            return str(pys[0])
    for cand in ("main.py", "run.py", "skill.py"):
        if (skill_dir / cand).exists():
            return str(skill_dir / cand)
    return None


def scan_skills() -> list[dict[str, Any]]:
    """扫描 SKILLS_DIR 下所有技能，返回元信息列表。"""
    out: list[dict[str, Any]] = []
    if not SKILLS_DIR.exists():
        return out
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        skill_md = d / "SKILL.md"
        meta: dict[str, Any] = {}
        desc = ""
        if skill_md.exists():
            text = skill_md.read_text(encoding="utf-8", errors="replace")
            fm = _parse_frontmatter(text)
            meta = fm
            desc = str(fm.get("description", "")).strip()
        entry = _find_entry(d)
        out.append(
            {
                "name": d.name,
                "description": desc,
                "path": str(d),
                "entry": entry,
                "metadata": meta,
            }
        )
    return out


def sync_skills_to_db(db) -> None:
    """将磁盘上的技能同步到数据库（新增/更新，不删除已禁用的）。"""
    from sqlalchemy import select

    disk = scan_skills()
    existing = {s.name: s for s in db.execute(select(Skill)).scalars().all()}
    for info in disk:
        if info["name"] in existing:
            s = existing[info["name"]]
            s.description = info["description"]
            s.path = info["path"]
            s.meta = info["metadata"]
        else:
            db.add(
                Skill(
                    name=info["name"],
                    description=info["description"],
                    path=info["path"],
                    source="builtin",
                    enabled=True,
                    meta=info["metadata"],
                )
            )
    db.commit()


def install_skill_from_dir(src: Path, name: str) -> Path:
    """把外部技能目录复制到 SKILLS_DIR。"""
    dst = SKILLS_DIR / name
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return dst


def execute_skill(skill: Skill, args: list[str], timeout: int = 600) -> dict[str, Any]:
    """执行技能入口脚本，返回 {"status", "stdout", "stderr", "exit_code"}。"""
    entry = _find_entry(Path(skill.path))
    if not entry:
        return {"status": "error", "stdout": "", "stderr": "技能未找到入口脚本", "exit_code": -1}
    env = dict(os.environ)
    s = get_settings()
    if s.ocr_api_key:
        env.setdefault("OCR_API_KEY", s.ocr_api_key)
    if s.ocr_base_url:
        env.setdefault("OCR_BASE_URL", s.ocr_base_url)
    cmd = [sys.executable, entry, *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(Path(skill.path).parent),
            env=env,
        )
        return {
            "status": "success" if proc.returncode == 0 else "error",
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "exit_code": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "stdout": "", "stderr": f"技能执行超时（{timeout}s）", "exit_code": -1}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "stdout": "", "stderr": str(exc), "exit_code": -1}
