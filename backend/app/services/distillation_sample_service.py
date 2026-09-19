"""Sample discovery and exact-key comparisons with frozen, scoped source receipts."""
from __future__ import annotations

from collections import Counter, defaultdict
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from ..distillation_sample_schemas import CompareSamplesArguments, DatabaseSampleArguments
from ..distillation_schemas import DistillationDocument, Evidence
from ..models import BucketFile
from . import distillation_library_service as library, distillation_service, library_database_service, permission_service
from .distillation_evidence_service import capture_evidence_identity
from .library_sample_query import catalog


class SampleSelectionError(ValueError):
    """Safe actionable selection error, without connector details."""


def read_sample(db, scenario_id, args: DatabaseSampleArguments) -> library.LibraryRead:
    source = distillation_service.evidence_source(db, args.data_source_id, scenario_id)
    if source.type == "file_bucket":
        return _read_file(db, source, scenario_id, args)
    if args.bucket_file_id:
        raise ValueError("数据库资料不接受文件引用")
    if source.type not in library_database_service.DATABASE_TYPES:
        raise ValueError("该资料不是数据库；文档或表格文件请使用资料阅读工具")
    evidence = Evidence(key="library_" + uuid.uuid4().hex[:20], title=library._safe_label(source.name) + " · 样本调查",
        kind="material", data_source_id=source.id, coverage="授权资料库中的当前结构或有界历史样本",
        limitations="只证明读取时的样本内容，不代表全量业务事实。")
    document = DistillationDocument(evidence=[evidence])
    identity = capture_evidence_identity(db, document, scenario_id)
    snapshot = library_database_service.snapshot(source)
    db.commit()
    result = library_database_service.database_schema(snapshot, sample=args if args.table_key else None)
    if args.table_key is None:
        if not isinstance(result, list):
            raise ValueError("资料结构读取结果无效")
        content = {"kind": "database_catalog", "tables": catalog(result),
            "limitations": ["这里只读取资料结构。选择返回的 table_key 和 field_key 后再次调用读取实际样本。"]}
    else:
        if not isinstance(result, dict):
            raise ValueError("资料样本读取结果无效")
        content = result
    content["evidence_key"] = evidence.key
    permission_service.refresh_request_authorization(db)
    if capture_evidence_identity(db, document, scenario_id) != identity:
        raise HTTPException(409, "读取期间资料配置或权限已变化，请重新调查")
    if len(json.dumps(content, ensure_ascii=False, allow_nan=False).encode()) > 180_000:
        raise ValueError("样本过大，请缩小字段和行数")
    return library.LibraryRead(evidence, identity, content)


def _read_file(db, source, scenario_id, args):
    from . import datasource_service, library_spreadsheet_reader

    if not args.bucket_file_id:
        raise SampleSelectionError("这是文件资料库。调用 list_library_files 后，将返回的具体文件 bucket_file_id 传入本工具；不能传 null。无需 table_key 即可读取单工作表。")
    file = db.scalar(select(BucketFile).where(BucketFile.id == args.bucket_file_id,
        BucketFile.data_source_id == source.id))
    if file is None:
        raise HTTPException(404, "资料文件不存在")
    bucket, key, filename = datasource_service.minio_file_identity(file, source)
    snapshot = {"bucket": bucket, "key": key, "filename": filename, "size": file.size,
        "sha256": file.content_sha256, "version": file.object_version_id or ""}
    evidence = Evidence(key="library_" + uuid.uuid4().hex[:20], title=library._safe_label(filename) + " · 真实样本",
        kind="material", data_source_id=source.id, bucket_file_id=file.id,
        coverage="已核验原始受管文件的有界单元格样本及实际行号", limitations="抽样不代表全量，模板不等于已发生结果。")
    document = DistillationDocument(evidence=[evidence])
    identity = capture_evidence_identity(db, document, scenario_id)
    db.commit()
    content = library_spreadsheet_reader.read_snapshot(snapshot, args)
    permission_service.refresh_request_authorization(db)
    if capture_evidence_identity(db, document, scenario_id) != identity:
        raise HTTPException(409, "读取期间资料或权限已变化，请重新调查")
    content["evidence_key"] = evidence.key
    return library.LibraryRead(evidence, identity, content)


def compare_rows(left: dict, right: dict, args: CompareSamplesArguments) -> dict:
    if left.get("kind") != "database_sample" or right.get("kind") != "database_sample":
        raise ValueError("请先实际读取两份数据库样本")
    left_fields = {item["field_key"] for item in left["table"]["fields"]}
    right_fields = {item["field_key"] for item in right["table"]["fields"]}
    if any(pair.left not in left_fields or pair.right not in right_fields for pair in args.fields):
        raise ValueError("关联字段不属于选定的实际样本")
    if len({pair.left for pair in args.fields}) != len(args.fields) or len({pair.right for pair in args.fields}) != len(args.fields):
        raise ValueError("复合关联字段不能重复")
    indexes = defaultdict(list)
    for index, row in enumerate(right["rows"]):
        key = tuple(row.get(pair.right) for pair in args.fields)
        if all(value is not None for value in key):
            indexes[key].append(index)
    unique, unmatched, ambiguous, excluded = [], [], [], []
    counts = Counter()
    for index, row in enumerate(left["rows"]):
        key = tuple(row.get(pair.left) for pair in args.fields)
        if any(value is None for value in key):
            excluded.append(index)
            continue
        counts[key] += 1
        matches = indexes.get(key, [])
        if len(matches) == 1:
            unique.append({"left_row": index, "right_row": matches[0]})
        elif matches:
            ambiguous.append({"left_row": index, "right_rows": matches})
        else:
            unmatched.append(index)
    return {"kind": "sample_comparison", "left_evidence_key": args.left_evidence_key, "right_evidence_key": args.right_evidence_key,
        "fields": [pair.model_dump() for pair in args.fields], "left_rows": len(left["rows"]), "right_rows": len(right["rows"]),
        "unique_matches": unique, "ambiguous_matches": ambiguous, "unmatched_left_rows": unmatched, "excluded_left_rows": excluded,
        "left_duplicate_key_groups": sum(count > 1 for count in counts.values()),
        "right_duplicate_key_groups": sum(len(rows) > 1 for rows in indexes.values()),
        "limitations": ["仅对两份已读取样本作精确文本匹配；空值、被排除单元格不匹配，不作模糊匹配或隐式类型转换。",
            "样本匹配唯一不代表总体唯一，也不能单独证明因果、流程先后或业务关系；需要专家解释并核对反例。"]}


def compare_samples(db, scenario_id, args: CompareSamplesArguments, evidence: list[Evidence]) -> dict:
    contents = []
    for key in (args.left_evidence_key, args.right_evidence_key):
        selected = next((item for item in evidence if item.key == key and item.library_read), None)
        if selected is None:
            raise ValueError("只能比较本项目实际读取并有回执的样本")
        contents.append(library.resolve_read(db, selected, scenario_id)["content"])
    return compare_rows(*contents, args)
