"""Trusted parser child and bounded parent adapter; no persistent raw upload."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from zipfile import ZipFile, is_zipfile

from fastapi import HTTPException


MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENT_TEXT = 200_000
MAX_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_PARSE_SECONDS = 35
MAX_OUTPUT_BYTES = 900_000
_OCR_SETTINGS = ("ocr_base_url", "ocr_endpoint_path", "ocr_api_key", "ocr_engine", "ocr_language",
    "ocr_allowed_hosts", "ocr_private_host_allowlist", "ocr_timeout_seconds")


@dataclass(frozen=True)
class ParsedAttachment:
    media_type: str
    text: str = field(repr=False)


def _bounded_path(raw: str) -> Path:
    path = Path(raw).resolve(strict=True)
    root = Path(tempfile.gettempdir()).resolve()
    if not path.is_relative_to(root) or not path.name.startswith("ontology-upload-") or path.is_symlink():
        raise ValueError("Invalid temporary input location")
    if not 0 < path.stat().st_size <= MAX_ATTACHMENT_BYTES:
        raise ValueError("Invalid temporary input size")
    return path


def _parse_child(request: dict) -> dict:
    from ..config import get_settings
    from . import catalog_ingestion_service, doc_parser, release_service

    settings = get_settings()
    for key in _OCR_SETTINGS:
        if key in request["ocr"]:
            setattr(settings, key, request["ocr"][key])
    settings.ocr_timeout_seconds = min(float(settings.ocr_timeout_seconds), 20)
    path = _bounded_path(request["path"])
    # Tighter temporary-input expansion limits apply before either profiling or
    # parsing; no Office member is extracted onto the filesystem.
    if is_zipfile(path):
        with ZipFile(path) as archive:
            members = archive.infolist()
            expanded = sum(max(0, member.file_size) for member in members)
            compressed = sum(max(0, member.compress_size) for member in members)
            if len(members) > 2000 or expanded > MAX_EXPANDED_BYTES or expanded > max(1, compressed) * 150:
                raise ValueError("Office expansion boundary exceeded")
    media, profile = catalog_ingestion_service.build_profile_path(path, request["filename"], request["media_type"])
    extension = str(profile.get("extension") or "")
    parsed = doc_parser.parse_bytes(path.read_bytes(), "attachment" + extension)
    text = parsed.get("text")
    if parsed.get("status") != "success" or not isinstance(text, str) or not text.strip():
        return {"ok": False, "error": "文件未能提取可用文本；扫描文件或图片需要已配置的 OCR，请检查格式或转换后重试。"}
    if len(text) > MAX_ATTACHMENT_TEXT:
        return {"ok": False, "error": "附件解析文本超过 200000 字符，请拆分文件后重试。"}
    if release_service.safe_snapshot_content({"content": text}) != {"content": text}:
        return {"ok": False, "error": "附件包含疑似密码、令牌或连接凭据，请先脱敏后重新上传。"}
    return {"ok": True, "media_type": media, "text": text}


def parse_staged(path: Path, filename: str, media_type: str) -> ParsedAttachment:
    from ..config import get_settings

    settings = get_settings()
    request = {"path": str(_bounded_path(str(path))), "filename": filename, "media_type": media_type,
        "ocr": {name: getattr(settings, name) for name in _OCR_SETTINGS}}
    environment = {key: value for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "LANG", "LC_ALL"}}
    environment.update(PYTHONPATH=str(Path(__file__).resolve().parents[2]), PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1")
    try:
        completed = subprocess.run([sys.executable, "-m", "app.services.distillation_attachment_parser"],
            input=json.dumps(request, ensure_ascii=False).encode(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=MAX_PARSE_SECONDS, env=environment, cwd=Path(__file__).resolve().parents[2], check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(422, "附件解析超过时间限制，请拆分或转换文件后重试") from exc
    if completed.returncode != 0 or len(completed.stdout) > MAX_OUTPUT_BYTES:
        raise HTTPException(422, "附件格式、解压或解析资源超过安全边界，请转换或缩小文件后重试")
    try:
        result = json.loads(completed.stdout)
        if not result.get("ok"):
            raise HTTPException(422, str(result.get("error") or "附件解析失败，请检查文件后重试")[:300])
        text, detected = result["text"], result["media_type"]
        if not isinstance(text, str) or not 0 < len(text) <= MAX_ATTACHMENT_TEXT or not isinstance(detected, str):
            raise ValueError("Invalid bounded parse result")
        return ParsedAttachment(detected, text)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, "附件解析未返回有效内容，请转换文件后重试") from exc


def main() -> int:
    from .distillation_parse_limits import restrict_process

    try:
        restrict_process()
        raw = sys.stdin.buffer.read(32_769)
        if len(raw) > 32_768:
            return 1
        result = _parse_child(json.loads(raw))
        output = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
        if len(output) > MAX_OUTPUT_BYTES:
            return 1
        sys.stdout.buffer.write(output)
        return 0
    except Exception:
        # Never print parser exceptions, which can contain file content or OCR
        # configuration. The parent maps this exit to a stable safe error.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
