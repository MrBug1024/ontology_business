"""Prepare modeling document text without materializing large table rows."""
from __future__ import annotations

from . import catalog_ingestion_service, datasource_service, doc_parser, modeling_contract_source_service


def parse_document(source, file):
    if source.resource_scope == "modeling" and catalog_ingestion_service.is_tabular_candidate_filename(file.filename):
        profiled = modeling_contract_source_service.profile_existing_tabular_file(source, file)
        if profiled is not None:
            return {
                "status": "success",
                "text": catalog_ingestion_service.profile_summary_text(profiled.profile, file.filename),
                "tabular_profile": profiled,
            }
    content, _size, _mime = datasource_service.read_bucket_file(file, source)
    return doc_parser.parse_bytes(content, file.filename)


def materialize_profile(db, source, file, parsed):
    profiled = parsed.get("tabular_profile") if parsed else None
    if profiled is None:
        return
    ref = modeling_contract_source_service.materialize_tabular_contract_source(
        db, source=source, bucket_file=file, profile=profiled.profile,
        content_sha256=profiled.content_sha256,
    )
    if ref is None:
        raise ValueError("表格未生成可验证的建模契约来源")
    file.content_sha256 = profiled.content_sha256
    file.modeling_contract_dataset_id = ref.dataset_id
    file.modeling_contract_schema_id = ref.schema_id
