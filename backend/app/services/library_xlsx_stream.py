"""Read bounded worksheet rows without loading the entire shared-string table."""
from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
import posixpath
from xml.etree import ElementTree as ET


NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
MAX_XML_BYTES = 128 * 1024 * 1024
MAX_COLUMNS = 512


class BoundedXML:
    def __init__(self, stream, limit=MAX_XML_BYTES):
        self.stream, self.remaining, self.tail = stream, limit, b""

    def read(self, size=-1):
        chunk = self.stream.read(min(65536 if size < 0 else size, self.remaining + 1))
        self.remaining -= len(chunk)
        probe = (self.tail + chunk).upper()
        if self.remaining < 0 or b"<!DOCTYPE" in probe or b"<!ENTITY" in probe or b"\x00" in probe:
            raise ValueError("XML 内容或读取额度超过边界")
        self.tail = probe[-16:]
        return chunk


def _small_xml(archive, member):
    with archive.open(member) as stream:
        return ET.parse(BoundedXML(stream, 1024 * 1024)).getroot()


def worksheets(archive):
    workbook = _small_xml(archive, "xl/workbook.xml")
    relationships = _small_xml(archive, "xl/_rels/workbook.xml.rels")
    targets = {}
    for item in relationships:
        if item.get("TargetMode") == "External":
            continue
        target = item.get("Target", "")
        path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)
        if not path.startswith("xl/") or ".." in PurePosixPath(path).parts:
            raise ValueError("工作表引用无效")
        targets[item.get("Id")] = path
    sheets = [{"name": item.get("name"), "path": targets.get(item.get(REL + "id"))}
        for item in workbook.findall(NS + "sheets/" + NS + "sheet")]
    if not sheets or len(sheets) > 40 or any(not item["path"] for item in sheets):
        raise ValueError("工作表数量或引用无效")
    properties = workbook.find(NS + "workbookPr")
    epoch = datetime(1904, 1, 1) if properties is not None and properties.get("date1904") in {"1", "true"} else datetime(1899, 12, 30)
    return sheets, epoch


def date_styles(archive):
    from openpyxl.styles.numbers import BUILTIN_FORMATS, is_date_format

    if "xl/styles.xml" not in archive.namelist():
        return set()
    styles = _small_xml(archive, "xl/styles.xml")
    formats = dict(BUILTIN_FORMATS)
    formats.update({int(item.get("numFmtId")): item.get("formatCode", "")
        for item in styles.findall(NS + "numFmts/" + NS + "numFmt")})
    return {index for index, item in enumerate(styles.findall(NS + "cellXfs/" + NS + "xf"))
        if is_date_format(formats.get(int(item.get("numFmtId", "0")), ""))}


def _text(element):
    return "".join(item.text or "" for item in element.iter(NS + "t"))[:1001]


def _column(reference):
    index = 0
    for char in reference:
        if not "A" <= char <= "Z":
            break
        index = index * 26 + ord(char) - 64
    if not 1 <= index <= MAX_COLUMNS:
        raise ValueError("工作表超过 512 列或单元格位置无效")
    return index - 1


def raw_rows(archive, member, max_rows, date_fields, epoch):
    from openpyxl.utils.datetime import from_excel

    rows, wanted = [], set()
    with archive.open(member) as stream:
        events = ET.iterparse(BoundedXML(stream), events=("start", "end"))
        root = None
        for event, element in events:
            if root is None:
                root = element
            if event != "end" or element.tag != NS + "row":
                continue
            number = int(element.get("r", "0"))
            values = {}
            for cell in element.findall(NS + "c"):
                kind, value = cell.get("t"), cell.findtext(NS + "v")
                if value is None and kind != "inlineStr":
                    continue
                column = _column(cell.get("r", ""))
                if kind == "s" and value is not None:
                    index = int(value)
                    if not 0 <= index <= 10_000_000:
                        raise ValueError("共享文本引用无效")
                    wanted.add(index)
                    if len(wanted) > 50_000:
                        raise ValueError("样本涉及文本过多，请缩小筛选范围")
                    values[column] = (index,)
                elif kind == "inlineStr":
                    values[column] = _text(cell)
                elif value is not None and int(cell.get("s", "0")) in date_fields and kind in {None, "n"}:
                    values[column] = str(from_excel(float(value), epoch=epoch))
                else:
                    values[column] = value[:1001] if value is not None else None
            if values:
                rows.append((number, values))
            element.clear()
            root.clear()
            if len(rows) >= max_rows:
                break
    return rows, wanted


def shared_strings(archive, wanted):
    if not wanted:
        return {}
    values = {}
    with archive.open("xl/sharedStrings.xml") as stream:
        events = ET.iterparse(BoundedXML(stream), events=("start", "end"))
        root, index = None, 0
        for event, element in events:
            if root is None:
                root = element
            if event != "end" or element.tag != NS + "si":
                continue
            if index in wanted:
                values[index] = _text(element)
            index += 1
            element.clear()
            root.clear()
            if len(values) == len(wanted):
                break
    if len(values) != len(wanted):
        raise ValueError("共享文本引用缺失")
    return values


def rows(archive, member, max_rows, date_fields, epoch):
    records, wanted = raw_rows(archive, member, max_rows, date_fields, epoch)
    strings = shared_strings(archive, wanted)
    return [(number, [strings[value[0]] if isinstance(value, tuple) else value
        for value in (record.get(index) for index in range(max(record) + 1))]) for number, record in records]
