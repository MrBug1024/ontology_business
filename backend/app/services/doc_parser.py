"""文档解析服务：将业务文件（excel/word/md/pdf/图片/txt/csv/pptx/json）解析为文本。

PDF 与图片优先调用已部署的 OCR 服务（复用 ocr-parser skill 的客户端逻辑），
失败时回退到本地 pypdf 提取。
"""
from __future__ import annotations

import base64
import json
from io import BytesIO
from pathlib import Path
from typing import Any

from ..config import get_settings

_IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif", "webp"}
_TEXT_EXTS = {"txt", "md", "markdown", "csv", "tsv", "json", "yaml", "yml", "xml", "log"}


def parse_file(path: str | Path, filename: str = "") -> dict[str, Any]:
    """Compatibility wrapper for callers that still own a local path."""
    p = Path(path)
    name = filename or p.name
    try:
        return parse_bytes(p.read_bytes(), name)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "text": "", "message": f"解析失败: {exc}"}


def parse_bytes(content: bytes, filename: str) -> dict[str, Any]:
    """Parse one document directly from durable object bytes."""
    if not isinstance(content, bytes):
        return {"status": "error", "text": "", "message": "文件内容必须是字节数据"}
    name = Path(str(filename or "")).name
    ext = Path(name).suffix.lstrip(".").lower()
    try:
        if ext in _IMAGE_EXTS:
            return _parse_image(content, name)
        if ext == "pdf":
            return _parse_pdf(content, name)
        if ext in ("xlsx", "xlsm"):
            return _parse_excel(content)
        if ext == "xls":
            return _parse_xls(content)
        if ext in ("docx",):
            return _parse_docx(content)
        if ext == "pptx":
            return _parse_pptx(content)
        if ext in _TEXT_EXTS:
            return _parse_text(content)
        return {"status": "error", "text": "", "message": f"暂不支持的文件类型: .{ext}"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "text": "", "message": f"解析失败: {exc}"}


# ──────────────────────────────────────────────
# OCR（PDF / 图片）
# ──────────────────────────────────────────────
def _ocr_available() -> bool:
    s = get_settings()
    return bool(s.ocr_base_url and s.ocr_api_key)


def _ocr_parse(content: bytes, filename: str, is_image: bool) -> dict[str, Any]:
    import httpx

    s = get_settings()
    table = True if not is_image else False
    rotate = False if not is_image else True
    try:
        resp = httpx.post(
            f"{s.ocr_base_url.rstrip('/')}/api/parse/sync",
            files={"file": (filename, content, _mime(filename))},
            data={
                "backend": "hybrid-auto-engine",
                "lang_list": "ch",
                "table_enable": str(table).lower(),
                "auto_rotate": str(rotate).lower(),
            },
            headers={"Authorization": s.ocr_api_key},
            timeout=600.0,
            verify=False,
        )
        if resp.status_code >= 400:
            return {"status": "error", "text": "", "message": f"OCR 服务返回 HTTP {resp.status_code}"}
        data = resp.json()
        if not isinstance(data, dict) or data.get("status") == "failed":
            return {"status": "error", "text": "", "message": "OCR 服务解析失败"}
        text = str(data.get("markdown") or data.get("text") or "")
        if not text.strip():
            return {"status": "error", "text": "", "message": "OCR 未提取到文本"}
        return {"status": "success", "text": text, "message": "OCR 解析完成"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "text": "", "message": f"OCR 请求失败: {exc}"}


def _mime(name: str) -> str:
    import mimetypes

    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _parse_pdf(content: bytes, name: str) -> dict[str, Any]:
    if _ocr_available():
        r = _ocr_parse(content, name, is_image=False)
        if r["status"] == "success":
            return r
        ocr_msg = r["message"]
    else:
        ocr_msg = "未配置 OCR 服务"
    # 回退：本地 pypdf
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content))
        pages = []
        for i, page in enumerate(reader.pages):
            t = page.extract_text() or ""
            pages.append(f"--- 第 {i + 1} 页 ---\n{t}")
        text = "\n\n".join(pages)
        if text.strip():
            return {"status": "success", "text": text, "message": f"本地 pypdf 提取（{ocr_msg}）"}
    except Exception:  # noqa: BLE001
        pass
    return {"status": "error", "text": "", "message": f"PDF 解析失败（{ocr_msg}）"}


def _parse_image(content: bytes, name: str) -> dict[str, Any]:
    if _ocr_available():
        r = _ocr_parse(content, name, is_image=True)
        if r["status"] == "success":
            return r
        return {"status": "error", "text": "", "message": f"图片 OCR 失败: {r['message']}"}
    return {"status": "error", "text": "", "message": "未配置 OCR 服务，无法解析图片"}


# ──────────────────────────────────────────────
# 表格 / 文档
# ──────────────────────────────────────────────
def _parse_excel(content: bytes) -> dict[str, Any]:
    from openpyxl import load_workbook

    wb = load_workbook(BytesIO(content), read_only=True, data_only=True)
    parts: list[str] = []
    for ws in wb.worksheets:
        lines = [f"### 工作表: {ws.title}"]
        for row in ws.iter_rows(values_only=True):
            if all(v is None for v in row):
                continue
            lines.append(" | ".join("" if v is None else str(v) for v in row))
        parts.append("\n".join(lines))
    wb.close()
    return {"status": "success", "text": "\n\n".join(parts), "message": "Excel 解析完成"}


def _parse_xls(content: bytes) -> dict[str, Any]:
    try:
        import xlrd  # type: ignore

        book = xlrd.open_workbook(file_contents=content)
        parts = []
        for sh in book.sheets():
            lines = [f"### 工作表: {sh.name}"]
            for r in range(sh.nrows):
                lines.append(" | ".join(str(sh.cell_value(r, c)) for c in range(sh.ncols)))
            parts.append("\n".join(lines))
        return {"status": "success", "text": "\n\n".join(parts), "message": "Excel 解析完成"}
    except ImportError:
        return {"status": "error", "text": "", "message": "缺少 xlrd 依赖，无法解析 .xls（请转换为 .xlsx）"}


def _parse_docx(content: bytes) -> dict[str, Any]:
    from docx import Document

    doc = Document(BytesIO(content))
    parts: list[str] = []
    for para in doc.paragraphs:
        if para.text.strip():
            parts.append(para.text)
    for table in doc.tables:
        parts.append("### 表格")
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return {"status": "success", "text": "\n".join(parts), "message": "Word 解析完成"}


def _parse_pptx(content: bytes) -> dict[str, Any]:
    from pptx import Presentation

    prs = Presentation(BytesIO(content))
    parts: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        parts.append(f"### 幻灯片 {i}")
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    t = "".join(run.text for run in para.runs)
                    if t.strip():
                        parts.append(t)
    return {"status": "success", "text": "\n".join(parts), "message": "PPT 解析完成"}


def _parse_text(raw: bytes) -> dict[str, Any]:
    for enc in ("utf-8", "gbk", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        import chardet

        enc = chardet.detect(raw).get("encoding") or "utf-8"
        text = raw.decode(enc, errors="replace")
    return {"status": "success", "text": text, "message": "文本解析完成"}


def parse_base64(b64: str, filename: str) -> dict[str, Any]:
    """解析 Base64 内容（供 skill 调用）。"""
    content = base64.b64decode(b64)
    return parse_bytes(content, filename)
