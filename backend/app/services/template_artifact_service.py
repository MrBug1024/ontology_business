"""Safe, format-preserving generation of business deliverables from templates.

The platform treats a source template as an immutable OOXML/Markdown artifact.
Generation only substitutes explicitly provided ``{{ path.to.value }}``
placeholders; it never converts DOCX/XLSX packages to Markdown or merely
renames an extension.  This keeps the source format, styles, formulas, images,
headers and other package parts intact while still giving Actions a typed,
auditable variable boundary.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile, ZipInfo

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import BucketFile, DataSource
from . import datasource_service, object_deletion_service


class TemplateArtifactError(ValueError):
    """The template or requested artifact cannot be generated safely."""


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MARKDOWN_MIME = "text/markdown; charset=utf-8"

_FORMAT_BY_SUFFIX = {
    ".docx": ("docx", DOCX_MIME),
    ".xlsx": ("xlsx", XLSX_MIME),
    ".md": ("markdown", MARKDOWN_MIME),
    ".markdown": ("markdown", MARKDOWN_MIME),
}
_MACRO_OR_EXECUTABLE_SUFFIXES = {
    ".docm",
    ".dotm",
    ".xlsm",
    ".xltm",
    ".xlam",
    ".xlsb",
}
_PLACEHOLDER = re.compile(r"{{\s*([^{}\r\n]+?)\s*}}")
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_MAX_VARIABLE_DEPTH = 16
_MAX_VARIABLE_ITEMS = 5_000
_MAX_VARIABLE_BYTES = 2 * 1024 * 1024
_MAX_PLACEHOLDER_PATH_LENGTH = 500
_UNSAFE_PLACEHOLDER_SEGMENTS = {"__proto__", "prototype", "constructor"}
_MAX_ZIP_ENTRIES = 5_000
_MAX_ZIP_UNCOMPRESSED_BYTES = 160 * 1024 * 1024
_MAX_SINGLE_ZIP_MEMBER_BYTES = 64 * 1024 * 1024
_DANGEROUS_MEMBER_MARKERS = (
    "vbaproject.bin",
    "vbadata.xml",
    "/activex/",
    "/embeddings/",
    "/oleobject",
)
_UNSAFE_EXTERNAL_SCHEMES = {
    "file",
    "javascript",
    "jscript",
    "vbscript",
    "ms-msdt",
    "shell",
}
_EXECUTABLE_MEMBER_SUFFIXES = {
    ".bat",
    ".cmd",
    ".com",
    ".dll",
    ".exe",
    ".hta",
    ".jar",
    ".js",
    ".msi",
    ".ps1",
    ".scr",
    ".vbs",
}
_XLSX_HEADER_FOOTER_TAGS = (
    "oddHeader",
    "oddFooter",
    "evenHeader",
    "evenFooter",
    "firstHeader",
    "firstFooter",
)


@dataclass(frozen=True)
class RenderedArtifact:
    filename: str
    content: bytes
    format: str
    mime: str
    sha256: str
    template_sha256: str
    variable_paths: tuple[str, ...]

    @property
    def size(self) -> int:
        return len(self.content)


def template_format(filename: str) -> tuple[str, str, str]:
    """Return ``(format, mime, suffix)`` after rejecting executable Office formats."""
    suffix = Path(str(filename or "")).suffix.lower()
    if suffix in _MACRO_OR_EXECUTABLE_SUFFIXES:
        raise TemplateArtifactError("不支持含宏或可执行内容的 Office 模板")
    spec = _FORMAT_BY_SUFFIX.get(suffix)
    if not spec:
        raise TemplateArtifactError("模板只支持 DOCX、XLSX 或 Markdown（.md/.markdown）格式")
    return spec[0], spec[1], suffix


def expected_mime(filename: str) -> str | None:
    """Return the canonical MIME for supported artifacts, or ``None`` for other files."""
    spec = _FORMAT_BY_SUFFIX.get(Path(str(filename or "")).suffix.lower())
    return spec[1] if spec else None


def _validate_variables(variables: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(variables, Mapping):
        raise TemplateArtifactError("模板变量必须是结构化对象")
    item_count = 0

    def visit(value: Any, depth: int) -> Any:
        nonlocal item_count
        if depth > _MAX_VARIABLE_DEPTH:
            raise TemplateArtifactError("模板变量嵌套层级过深")
        item_count += 1
        if item_count > _MAX_VARIABLE_ITEMS:
            raise TemplateArtifactError("模板变量数量超过限制")
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise TemplateArtifactError("模板变量不能包含 NaN 或无穷大")
            return value
        if isinstance(value, Mapping):
            normalized: dict[str, Any] = {}
            for key, child in value.items():
                if not isinstance(key, str) or not key or len(key) > 200:
                    raise TemplateArtifactError("模板变量字段名必须是 1 到 200 个字符的文本")
                normalized[key] = visit(child, depth + 1)
            return normalized
        if isinstance(value, (list, tuple)):
            return [visit(child, depth + 1) for child in value]
        raise TemplateArtifactError(f"模板变量包含不支持的值类型: {type(value).__name__}")

    normalized = visit(dict(variables), 0)
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_VARIABLE_BYTES:
        raise TemplateArtifactError("模板变量超过 2MB 限制")
    return normalized


_MISSING = object()


def _validated_path_parts(path: str) -> list[str]:
    normalized = str(path or "").strip()
    parts = [part.strip() for part in normalized.split(".")]
    if (
        not normalized
        or len(normalized) > _MAX_PLACEHOLDER_PATH_LENGTH
        or len(parts) > _MAX_VARIABLE_DEPTH
        or any(not part or len(part) > 200 for part in parts)
        or any(any(ord(char) < 32 for char in part) for part in parts)
        or any(part.lower() in _UNSAFE_PLACEHOLDER_SEGMENTS for part in parts)
    ):
        raise TemplateArtifactError(f"模板变量路径无效或不安全: {normalized[:120]}")
    return parts


def _resolve_path(variables: Mapping[str, Any], path: str) -> Any:
    current: Any = variables
    for segment in _validated_path_parts(path):
        if isinstance(current, Mapping):
            if segment not in current:
                return _MISSING
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit():
            index = int(segment)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _display_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def referenced_variable_paths(text: str) -> set[str]:
    """Return normalized placeholder paths from user-visible template text."""
    paths: set[str] = set()
    for match in _PLACEHOLDER.finditer(str(text or "")):
        path = match.group(1).strip()
        if path:
            _validated_path_parts(path)
            paths.add(path)
    return paths


def requested_output_suffix(text: str) -> str:
    """Inspect a filename pattern without mistaking dots inside placeholders."""
    probe = _PLACEHOLDER.sub("value", str(text or "").strip())
    return Path(probe).suffix.lower()


def merge_template_input_schema(
    schema: Mapping[str, Any] | None,
    variable_paths: Iterable[str],
) -> dict[str, Any]:
    """Make every required template path explicit in an Action JSON Schema.

    Templates fail closed when a placeholder is missing.  Persisting the same
    requirement in ``input_schema`` gives both the no-JSON editor and the Agent
    an actionable contract instead of discovering missing nested fields only
    during dry-run.
    """
    raw = copy.deepcopy(dict(schema or {}))
    if raw and not any(key in raw for key in ("type", "properties", "required")):
        raw = {
            "type": "object",
            "properties": raw,
            "required": [],
            "additionalProperties": False,
        }
    if not raw:
        raw = {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }

    def ensure_object(node: dict[str, Any], label: str) -> tuple[dict[str, Any], list[str]]:
        if node.get("type") not in (None, "object"):
            raise TemplateArtifactError(f"模板变量 {label} 与输入参数类型冲突，应为对象")
        node["type"] = "object"
        properties = node.setdefault("properties", {})
        required = node.setdefault("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise TemplateArtifactError(f"模板变量 {label} 的输入参数 Schema 无效")
        if any(not isinstance(item, str) for item in required):
            raise TemplateArtifactError(f"模板变量 {label} 的 required 必须是字段名列表")
        node.setdefault("additionalProperties", False)
        return properties, required

    def require(required: list[str], name: str) -> None:
        if name not in required:
            required.append(name)

    def ensure_array(node: dict[str, Any], label: str, minimum: int) -> dict[str, Any]:
        if node.get("type") not in (None, "array"):
            raise TemplateArtifactError(f"模板变量 {label} 与输入参数类型冲突，应为列表")
        node["type"] = "array"
        node["minItems"] = max(int(node.get("minItems") or 0), minimum)
        items = node.setdefault("items", {})
        if not isinstance(items, dict):
            raise TemplateArtifactError(f"模板变量 {label} 的列表项 Schema 无效")
        return items

    def add_path(node: dict[str, Any], parts: list[str], full_path: str) -> None:
        properties, required = ensure_object(node, full_path)
        name = parts[0]
        if not name or name.isdigit():
            raise TemplateArtifactError(f"模板变量路径无效: {full_path}")
        child = properties.setdefault(name, {})
        if not isinstance(child, dict):
            raise TemplateArtifactError(f"模板变量 {full_path} 的字段 Schema 无效")
        require(required, name)
        if len(parts) == 1:
            child.setdefault("description", f"模板变量：{full_path}")
            return

        next_part = parts[1]
        if next_part.isdigit():
            index = int(next_part)
            items = ensure_array(child, full_path, index + 1)
            if len(parts) == 2:
                items.setdefault("description", f"模板变量：{full_path}")
                return
            add_path(items, parts[2:], full_path)
            return

        if child.get("type") not in (None, "object"):
            raise TemplateArtifactError(f"模板变量 {full_path} 与输入参数类型冲突，应为对象")
        add_path(child, parts[1:], full_path)

    ensure_object(raw, "根节点")
    for path in sorted({str(item).strip() for item in variable_paths if str(item).strip()}):
        parts = _validated_path_parts(path)
        add_path(raw, parts, path)
    return raw


def _render_text(text: str, variables: Mapping[str, Any], unresolved: set[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        path = match.group(1).strip()
        value = _resolve_path(variables, path)
        if value is _MISSING:
            unresolved.add(path)
            return match.group(0)
        return _display_value(value)

    return _PLACEHOLDER.sub(replace, text)


def _render_xml_text_group(
    elements: Iterable[ET.Element], variables: Mapping[str, Any], unresolved: set[str]
) -> bool:
    """Replace placeholders even when OOXML split them across styled runs."""
    nodes = list(elements)
    if not nodes:
        return False
    texts = [node.text or "" for node in nodes]
    combined = "".join(texts)
    matches = list(_PLACEHOLDER.finditer(combined))
    if not matches:
        return False

    offsets: list[tuple[int, int]] = []
    cursor = 0
    for value in texts:
        offsets.append((cursor, cursor + len(value)))
        cursor += len(value)

    changed = False
    for match in reversed(matches):
        path = match.group(1).strip()
        value = _resolve_path(variables, path)
        if value is _MISSING:
            unresolved.add(path)
            continue
        replacement = _display_value(value)
        start_node = next(
            (i for i, (start, end) in enumerate(offsets) if start <= match.start() < end),
            None,
        )
        # A zero-length final text node cannot start a non-empty placeholder;
        # therefore ``start_node`` is always present for a regex match.
        end_node = next(
            (i for i, (start, end) in enumerate(offsets) if start < match.end() <= end),
            None,
        )
        if start_node is None or end_node is None:
            raise TemplateArtifactError("模板中的占位符结构无法安全解析")
        start_offset = match.start() - offsets[start_node][0]
        end_offset = match.end() - offsets[end_node][0]
        if start_node == end_node:
            current = nodes[start_node].text or ""
            nodes[start_node].text = current[:start_offset] + replacement + current[end_offset:]
        else:
            first = nodes[start_node].text or ""
            last = nodes[end_node].text or ""
            nodes[start_node].text = first[:start_offset] + replacement
            for index in range(start_node + 1, end_node):
                nodes[index].text = ""
            nodes[end_node].text = last[end_offset:]
        changed = True

    if changed:
        for node in nodes:
            value = node.text or ""
            if value[:1].isspace() or value[-1:].isspace():
                node.set(_XML_SPACE, "preserve")
    return changed


def _safe_zip_infos(content: bytes) -> tuple[list[ZipInfo], bytes]:
    try:
        with ZipFile(io.BytesIO(content), "r") as package:
            infos = package.infolist()
            archive_comment = package.comment
            if not infos or len(infos) > _MAX_ZIP_ENTRIES:
                raise TemplateArtifactError("Office 模板包结构异常或文件数量过多")
            seen: set[str] = set()
            total_size = 0
            for info in infos:
                name = info.filename
                normalized = name.replace("\\", "/")
                parts = normalized.split("/")
                if (
                    not name
                    or name in seen
                    or name.startswith(("/", "\\"))
                    or "\\" in name
                    or any(part in {"", ".", ".."} for part in parts if not (part == "" and name.endswith("/")))
                    or info.flag_bits & 0x1
                ):
                    raise TemplateArtifactError("Office 模板包含不安全的压缩包路径或加密成员")
                seen.add(name)
                total_size += int(info.file_size)
                if info.file_size > _MAX_SINGLE_ZIP_MEMBER_BYTES:
                    raise TemplateArtifactError("Office 模板中的单个文件过大")
            if total_size > _MAX_ZIP_UNCOMPRESSED_BYTES:
                raise TemplateArtifactError("Office 模板解压后超过安全大小限制")
            return [copy.copy(info) for info in infos], archive_comment
    except BadZipFile as exc:
        raise TemplateArtifactError("Office 模板不是有效的 OOXML 文件") from exc


def _read_zip_members(content: bytes, infos: list[ZipInfo]) -> dict[str, bytes]:
    try:
        with ZipFile(io.BytesIO(content), "r") as package:
            return {info.filename: package.read(info) for info in infos}
    except (BadZipFile, RuntimeError, OSError) as exc:
        raise TemplateArtifactError("Office 模板包读取失败") from exc


def _parse_xml(raw: bytes, member_name: str) -> ET.Element:
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise TemplateArtifactError(f"Office 模板成员 {member_name} 包含不安全的 XML 声明")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise TemplateArtifactError(f"Office 模板成员 {member_name} 的 XML 无效") from exc


def _validate_external_relationships(members: Mapping[str, bytes]) -> None:
    for name, raw in members.items():
        if not name.endswith(".rels"):
            continue
        root = _parse_xml(raw, name)
        for rel in root.findall(f".//{{{_REL_NS}}}Relationship"):
            if str(rel.get("TargetMode") or "").lower() != "external":
                continue
            target = str(rel.get("Target") or "").strip()
            parsed = urlparse(target)
            if (
                target.startswith(("\\\\", "//"))
                or parsed.scheme.lower() in _UNSAFE_EXTERNAL_SCHEMES
            ):
                raise TemplateArtifactError("Office 模板包含不安全的外部链接")


def _validate_active_content(members: Mapping[str, bytes], artifact_format: str) -> None:
    """Reject field/formula instructions that can start code or network activity.

    Macro-free OOXML can still carry legacy DDE/MACROBUTTON fields or Excel
    DDE/WEBSERVICE formulas. Those instructions are not needed for deterministic
    business templates and must not survive merely because the package suffix
    is ``.docx``/``.xlsx``.
    """
    if artifact_format == "docx":
        matcher = re.compile(
            r"^word/(?:document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$"
        )
        dangerous = re.compile(r"(?:^|\s)(?:DDEAUTO|DDE|MACROBUTTON)(?:\s|$)", re.IGNORECASE)
        for name, raw in members.items():
            if not matcher.match(name):
                continue
            root = _parse_xml(raw, name)
            instructions = [
                str(node.text or "")
                for node in root.iter(f"{{{_WORD_NS}}}instrText")
            ]
            instructions.extend(
                str(value or "")
                for node in root.iter()
                for key, value in node.attrib.items()
                if key.rsplit("}", 1)[-1] == "instr"
            )
            if dangerous.search(" ".join(instructions)):
                raise TemplateArtifactError("Office 模板包含可执行的 DDE 或宏字段")
        return

    dde = re.compile(r"^\s*[=+\-@]?\s*(?:'[^']+'|[A-Za-z0-9_. -]+)\s*\|", re.IGNORECASE)
    network = re.compile(r"(?:^|[^A-Z0-9_.])WEBSERVICE\s*\(", re.IGNORECASE)
    for name, raw in members.items():
        if not (
            name == "xl/workbook.xml"
            or (name.startswith("xl/worksheets/") and name.endswith(".xml"))
        ):
            continue
        root = _parse_xml(raw, name)
        formulas = [
            str(node.text or "")
            for node in root.iter()
            if node.tag in {
                f"{{{_SHEET_NS}}}f",
                f"{{{_SHEET_NS}}}definedName",
            }
        ]
        if any(dde.search(formula) or network.search(formula) for formula in formulas):
            raise TemplateArtifactError("Office 模板包含可执行或联网的公式指令")


def _validate_ooxml_package(
    content: bytes, artifact_format: str
) -> tuple[list[ZipInfo], dict[str, bytes], bytes]:
    infos, archive_comment = _safe_zip_infos(content)
    members = _read_zip_members(content, infos)
    names = set(members)
    required = {"[Content_Types].xml", "_rels/.rels"}
    if artifact_format == "docx":
        required.add("word/document.xml")
        main_part = "/word/document.xml"
        expected_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    else:
        required.add("xl/workbook.xml")
        if not any(name.startswith("xl/worksheets/") and name.endswith(".xml") for name in names):
            raise TemplateArtifactError("XLSX 模板缺少工作表")
        main_part = "/xl/workbook.xml"
        expected_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
    if not required.issubset(names):
        raise TemplateArtifactError("Office 模板缺少必需的 OOXML 包成员")

    lowered = {f"/{name.lower().strip('/')}" for name in names}
    if any(marker in name for name in lowered for marker in _DANGEROUS_MEMBER_MARKERS):
        raise TemplateArtifactError("Office 模板包含宏、ActiveX 或嵌入式可执行对象")
    if any(Path(name.rstrip("/")).suffix.lower() in _EXECUTABLE_MEMBER_SUFFIXES for name in names):
        raise TemplateArtifactError("Office 模板包含可执行文件成员")

    types_root = _parse_xml(members["[Content_Types].xml"], "[Content_Types].xml")
    content_type = ""
    default_types: dict[str, str] = {}
    for override in types_root:
        kind = override.tag.rsplit("}", 1)[-1]
        if kind == "Default":
            default_types[str(override.get("Extension") or "").lower()] = str(
                override.get("ContentType") or ""
            )
            continue
        if kind != "Override":
            continue
        if override.get("PartName") == main_part:
            content_type = str(override.get("ContentType") or "")
            break
    if not content_type:
        content_type = default_types.get(Path(main_part).suffix.lstrip(".").lower(), "")
    if content_type != expected_type:
        raise TemplateArtifactError("Office 模板的真实内容类型与文件扩展名不匹配，或包含宏")
    _validate_external_relationships(members)
    _validate_active_content(members, artifact_format)
    return infos, members, archive_comment


def _serialize_xml(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _render_docx_members(
    members: dict[str, bytes], variables: Mapping[str, Any], unresolved: set[str]
) -> None:
    text_parts = re.compile(
        r"^word/(?:document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$"
    )
    for name in list(members):
        if not text_parts.match(name):
            continue
        root = _parse_xml(members[name], name)
        changed = False
        for paragraph in root.iter(f"{{{_WORD_NS}}}p"):
            changed = _render_xml_text_group(
                paragraph.iter(f"{{{_WORD_NS}}}t"), variables, unresolved
            ) or changed
        if changed:
            members[name] = _serialize_xml(root)


def _render_xlsx_members(
    members: dict[str, bytes], variables: Mapping[str, Any], unresolved: set[str]
) -> None:
    shared_entries: list[str] = []
    if "xl/sharedStrings.xml" in members:
        name = "xl/sharedStrings.xml"
        root = _parse_xml(members[name], name)
        changed = False
        for item in root.iter(f"{{{_SHEET_NS}}}si"):
            shared_entries.append(
                "".join(node.text or "" for node in item.iter(f"{{{_SHEET_NS}}}t"))
            )
            changed = _render_xml_text_group(
                item.iter(f"{{{_SHEET_NS}}}t"), variables, unresolved
            ) or changed
        if changed:
            members[name] = _serialize_xml(root)

    for name in list(members):
        if not name.startswith("xl/worksheets/") or not name.endswith(".xml"):
            continue
        root = _parse_xml(members[name], name)
        changed = False
        # When a whole cell is one numeric/boolean placeholder, preserve its
        # semantic type so formulas and number formats keep working.  Text,
        # arrays and objects remain strings.  This conversion changes only the
        # cell storage type; the style id and surrounding sheet structure stay
        # untouched.
        for cell in root.iter(f"{{{_SHEET_NS}}}c"):
            cell_type = str(cell.get("t") or "")
            original = ""
            if cell_type == "s":
                value_node = cell.find(f"{{{_SHEET_NS}}}v")
                try:
                    index = int(value_node.text or "") if value_node is not None else -1
                except ValueError:
                    index = -1
                if 0 <= index < len(shared_entries):
                    original = shared_entries[index]
            elif cell_type == "inlineStr":
                inline = cell.find(f"{{{_SHEET_NS}}}is")
                if inline is not None:
                    original = "".join(
                        node.text or "" for node in inline.iter(f"{{{_SHEET_NS}}}t")
                    )
            elif cell_type == "str" and cell.find(f"{{{_SHEET_NS}}}f") is None:
                value_node = cell.find(f"{{{_SHEET_NS}}}v")
                original = (value_node.text or "") if value_node is not None else ""
            match = _PLACEHOLDER.fullmatch(original)
            if not match:
                continue
            path = match.group(1).strip()
            resolved = _resolve_path(variables, path)
            if resolved is _MISSING:
                unresolved.add(path)
                continue
            if resolved is None or isinstance(resolved, (bool, int, float)):
                _set_xlsx_typed_cell(cell, resolved)
                changed = True
        for inline in root.iter(f"{{{_SHEET_NS}}}is"):
            changed = _render_xml_text_group(
                inline.iter(f"{{{_SHEET_NS}}}t"), variables, unresolved
            ) or changed
        # Formula-string cells store their text in <v> rather than <t>.
        for cell in root.iter(f"{{{_SHEET_NS}}}c"):
            if cell.get("t") != "str" or cell.find(f"{{{_SHEET_NS}}}f") is not None:
                continue
            value = cell.find(f"{{{_SHEET_NS}}}v")
            if value is not None:
                rendered = _render_text(value.text or "", variables, unresolved)
                if rendered != (value.text or ""):
                    value.text = rendered
                    changed = True
        for tag in _XLSX_HEADER_FOOTER_TAGS:
            for header_or_footer in root.iter(f"{{{_SHEET_NS}}}{tag}"):
                rendered = _render_text(
                    header_or_footer.text or "",
                    variables,
                    unresolved,
                )
                if rendered != (header_or_footer.text or ""):
                    header_or_footer.text = rendered
                    changed = True
        # Template inputs changed, therefore every cached formula result is
        # stale.  Remove cached values/errors and ask Excel-compatible clients
        # to recalculate on open instead of displaying a pre-generation value.
        for cell in root.iter(f"{{{_SHEET_NS}}}c"):
            if cell.find(f"{{{_SHEET_NS}}}f") is None:
                continue
            value = cell.find(f"{{{_SHEET_NS}}}v")
            if value is not None:
                cell.remove(value)
                changed = True
            if cell.get("t") in {"e", "str", "s", "inlineStr"}:
                cell.attrib.pop("t", None)
                changed = True
        if changed:
            members[name] = _serialize_xml(root)

    workbook_name = "xl/workbook.xml"
    workbook_root = _parse_xml(members[workbook_name], workbook_name)
    calc_properties = workbook_root.find(f"{{{_SHEET_NS}}}calcPr")
    if calc_properties is None:
        calc_properties = ET.SubElement(workbook_root, f"{{{_SHEET_NS}}}calcPr")
    calc_properties.set("calcMode", "auto")
    calc_properties.set("fullCalcOnLoad", "1")
    calc_properties.set("forceFullCalc", "1")
    members[workbook_name] = _serialize_xml(workbook_root)


def _set_xlsx_typed_cell(cell: ET.Element, value: Any) -> None:
    for child in list(cell):
        if child.tag in {f"{{{_SHEET_NS}}}is", f"{{{_SHEET_NS}}}v"}:
            cell.remove(child)
    if value is None:
        cell.attrib.pop("t", None)
        return
    value_node = ET.Element(f"{{{_SHEET_NS}}}v")
    if isinstance(value, bool):
        cell.set("t", "b")
        value_node.text = "1" if value else "0"
    else:
        cell.set("t", "n")
        value_node.text = str(value)
    cell.append(value_node)


def _write_ooxml(
    infos: list[ZipInfo], members: Mapping[str, bytes], archive_comment: bytes
) -> bytes:
    out = io.BytesIO()
    with ZipFile(out, "w", compression=ZIP_DEFLATED, allowZip64=True) as package:
        package.comment = archive_comment
        for info in infos:
            payload = members[info.filename]
            copied = copy.copy(info)
            copied.flag_bits &= ~0x1
            package.writestr(copied, payload)
    return out.getvalue()


def _render_output_filename(
    requested: str, template_filename: str, suffix: str, variables: Mapping[str, Any]
) -> str:
    unresolved: set[str] = set()
    requested = _render_text(str(requested or "").strip(), variables, unresolved)
    if unresolved:
        raise TemplateArtifactError(
            "输出文件名缺少模板变量: " + "、".join(sorted(unresolved)[:20])
        )
    if not requested:
        requested = f"{Path(template_filename).stem}-生成{suffix}"
    elif not Path(requested).suffix:
        requested += suffix
    safe = datasource_service.validate_bucket_filename(requested)
    output_spec = _FORMAT_BY_SUFFIX.get(Path(safe).suffix.lower())
    template_spec = _FORMAT_BY_SUFFIX.get(Path(template_filename).suffix.lower())
    if not output_spec or not template_spec or output_spec[0] != template_spec[0]:
        raise TemplateArtifactError("输出附件必须与源模板保持相同文件格式")
    return safe


def inspect_template(filename: str, content: bytes) -> dict[str, Any]:
    """Validate real content and return safe template metadata."""
    artifact_format, mime, suffix = template_format(filename)
    if not isinstance(content, bytes) or not content:
        raise TemplateArtifactError("模板文件为空")
    if len(content) > get_settings().max_upload_bytes:
        raise TemplateArtifactError("模板文件超过系统上传大小限制")
    if artifact_format in {"docx", "xlsx"}:
        _validate_ooxml_package(content, artifact_format)
    else:
        try:
            content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise TemplateArtifactError("Markdown 模板必须使用 UTF-8 编码") from exc
    return {
        "format": artifact_format,
        "mime": mime,
        "suffix": suffix,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def render_template(
    template_filename: str,
    template_content: bytes,
    variables: Mapping[str, Any],
    *,
    output_filename: str = "",
) -> RenderedArtifact:
    """Fill a validated template without changing its underlying file format."""
    normalized = _validate_variables(variables)
    metadata = inspect_template(template_filename, template_content)
    unresolved: set[str] = set()
    artifact_format = str(metadata["format"])
    if artifact_format == "markdown":
        source = template_content.decode("utf-8-sig")
        rendered_content = _render_text(source, normalized, unresolved).encode("utf-8")
    else:
        infos, members, archive_comment = _validate_ooxml_package(
            template_content, artifact_format
        )
        if artifact_format == "docx":
            _render_docx_members(members, normalized, unresolved)
        else:
            _render_xlsx_members(members, normalized, unresolved)
        rendered_content = _write_ooxml(infos, members, archive_comment)
        # Validate the generated package too; a successful substitution must
        # never turn a valid source into a malformed Office attachment.
        _validate_ooxml_package(rendered_content, artifact_format)
    if unresolved:
        raise TemplateArtifactError(
            "模板缺少必填变量: " + "、".join(sorted(unresolved)[:50])
        )
    if len(rendered_content) > get_settings().max_upload_bytes:
        raise TemplateArtifactError("生成附件超过系统文件大小限制")
    filename = _render_output_filename(
        output_filename,
        template_filename,
        str(metadata["suffix"]),
        normalized,
    )
    variable_paths = tuple(sorted(_placeholder_paths(template_filename, template_content)))
    return RenderedArtifact(
        filename=filename,
        content=rendered_content,
        format=artifact_format,
        mime=str(metadata["mime"]),
        sha256=hashlib.sha256(rendered_content).hexdigest(),
        template_sha256=str(metadata["sha256"]),
        variable_paths=variable_paths,
    )


def _placeholder_paths(filename: str, content: bytes) -> set[str]:
    artifact_format, _mime, _suffix = template_format(filename)
    texts: list[str] = []
    if artifact_format == "markdown":
        texts.append(content.decode("utf-8-sig"))
    else:
        _infos, members, _comment = _validate_ooxml_package(content, artifact_format)
        if artifact_format == "docx":
            matcher = re.compile(
                r"^word/(?:document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$"
            )
            namespace = _WORD_NS
            item_tag = "p"
        else:
            matcher = re.compile(r"^xl/(?:sharedStrings|worksheets/[^/]+)\.xml$")
            namespace = _SHEET_NS
            item_tag = "si"
        for name, raw in members.items():
            if not matcher.match(name):
                continue
            root = _parse_xml(raw, name)
            if artifact_format == "docx":
                groups = root.iter(f"{{{namespace}}}{item_tag}")
            elif name == "xl/sharedStrings.xml":
                groups = root.iter(f"{{{namespace}}}si")
            else:
                groups = root.iter(f"{{{namespace}}}is")
            for group in groups:
                texts.append("".join(node.text or "" for node in group.iter(f"{{{namespace}}}t")))
            if artifact_format == "xlsx" and name.startswith("xl/worksheets/"):
                for tag in _XLSX_HEADER_FOOTER_TAGS:
                    texts.extend(
                        node.text or ""
                        for node in root.iter(f"{{{namespace}}}{tag}")
                    )
                for cell in root.iter(f"{{{namespace}}}c"):
                    if cell.get("t") == "str" and cell.find(f"{{{namespace}}}f") is None:
                        value = cell.find(f"{{{namespace}}}v")
                        if value is not None:
                            texts.append(value.text or "")
    paths: set[str] = set()
    for text in texts:
        for match in _PLACEHOLDER.finditer(text):
            path = match.group(1).strip()
            _validated_path_parts(path)
            paths.add(path)
    return paths


def load_bucket_template(template_file: BucketFile, template_source: DataSource) -> bytes:
    """Read a template only from its declared file-bucket root."""
    content, _size, _mime = datasource_service.read_bucket_file(
        template_file, template_source
    )
    inspect_template(template_file.filename, content)
    return content


def generate_bucket_artifact(
    template_file: BucketFile,
    template_source: DataSource,
    target_source: DataSource,
    variables: Mapping[str, Any],
    *,
    output_filename: str = "",
    expected_template_sha256: str = "",
    generated_by_action_log_id: str | None = None,
    origin_template_id: str | None = None,
    origin_template_version_id: str | None = None,
    origin_template_version: int | None = None,
    db: Session | None = None,
) -> tuple[BucketFile, dict[str, Any]]:
    """Generate and persist one attachment in an owned target file bucket."""
    rendered = preview_bucket_artifact(
        template_file,
        template_source,
        target_source,
        variables,
        output_filename=output_filename,
        expected_template_sha256=expected_template_sha256,
    )
    claim = None
    file_id = generated_by_action_log_id or uuid.uuid4().hex
    if datasource_service.is_managed_minio_source(target_source):
        if db is None:
            raise TemplateArtifactError("托管模板产出缺少上传事务")
        claim = object_deletion_service.prepare_bucket_file_upload(
            target_source,
            file_id,
            rendered.filename,
        )
    if claim is not None:
        with object_deletion_service.heartbeat_upload_intent(
            claim
        ) as upload_heartbeat:
            object_deletion_service.begin_upload_put(claim)
            bucket_file = datasource_service.save_bucket_file(
                target_source,
                rendered.filename,
                rendered.content,
                mime=rendered.mime,
                stable_file_id=file_id,
                upload_object_key=claim.object_key,
            )
            object_deletion_service.assert_upload_active(
                upload_heartbeat, claim, bucket_file
            )
        try:
            object_deletion_service.retain_bucket_file_upload(
                db, claim, bucket_file, target_source
            )
        except object_deletion_service.UploadIntentLeaseLostError:
            db.rollback()
            object_deletion_service.schedule_abandoned_upload_best_effort(
                claim,
                bucket_file,
            )
            raise
    else:
        bucket_file = datasource_service.save_bucket_file(
            target_source,
            rendered.filename,
            rendered.content,
            mime=rendered.mime,
            stable_file_id=generated_by_action_log_id,
        )
    bucket_file.origin_template_file_id = template_file.id
    bucket_file.origin_template_sha256 = rendered.template_sha256
    bucket_file.origin_template_id = origin_template_id
    bucket_file.origin_template_version_id = origin_template_version_id
    bucket_file.generated_by_action_log_id = generated_by_action_log_id
    result = {
        "status": "generated",
        "artifact": {
            "id": bucket_file.id,
            "filename": bucket_file.filename,
            "format": rendered.format,
            "mime": rendered.mime,
            "size": rendered.size,
            "sha256": rendered.sha256,
            "template_file_id": template_file.id,
            "template_sha256": rendered.template_sha256,
            "template_id": origin_template_id,
            "template_version_id": origin_template_version_id,
            "template_version": origin_template_version,
            "download_url": f"/api/data-sources/files/{bucket_file.id}/download",
        },
    }
    return bucket_file, result


def preview_bucket_artifact(
    template_file: BucketFile,
    template_source: DataSource,
    target_source: DataSource,
    variables: Mapping[str, Any],
    *,
    output_filename: str = "",
    expected_template_sha256: str = "",
) -> RenderedArtifact:
    """Fully validate a future attachment without writing it to disk."""
    if template_source.type != "file_bucket" or target_source.type != "file_bucket":
        raise TemplateArtifactError("模板来源和附件目标都必须是文件桶数据源")
    content = load_bucket_template(template_file, template_source)
    rendered = render_template(
        template_file.filename,
        content,
        variables,
        output_filename=output_filename,
    )
    if expected_template_sha256 and rendered.template_sha256 != expected_template_sha256:
        raise TemplateArtifactError("源模板内容在操作配置后已变化，请重新预演")
    return rendered


def pinned_template_metadata(
    template_file: BucketFile, template_source: DataSource
) -> dict[str, Any]:
    """Return the immutable fields stored in an Action executor configuration."""
    content = load_bucket_template(template_file, template_source)
    metadata = inspect_template(template_file.filename, content)
    return {
        "template_sha256": metadata["sha256"],
        "template_format": metadata["format"],
        "template_mime": metadata["mime"],
        "template_filename": template_file.filename,
        "template_variable_paths": sorted(_placeholder_paths(template_file.filename, content)),
    }
