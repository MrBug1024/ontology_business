from types import SimpleNamespace

from app.services import document_job_parsing as parsing


def test_large_modeling_table_uses_bounded_profile_not_whole_document_bytes(monkeypatch):
    source = SimpleNamespace(resource_scope="modeling")
    file = SimpleNamespace(filename="large.xlsx")
    profile = SimpleNamespace(profile={"tables": []}, content_sha256="a" * 64)
    monkeypatch.setattr(parsing.modeling_contract_source_service, "profile_existing_tabular_file", lambda *args: profile)
    monkeypatch.setattr(parsing.catalog_ingestion_service, "profile_summary_text", lambda *args: "Column contract")
    monkeypatch.setattr(parsing.datasource_service, "read_bucket_file", lambda *args: (_ for _ in ()).throw(AssertionError("Unbounded read")))
    result = parsing.parse_document(source, file)
    assert result["status"] == "success"
    assert result["text"] == "Column contract"
    assert result["tabular_profile"] is profile


def test_runtime_attachment_does_not_create_modeling_contract(monkeypatch):
    monkeypatch.setattr(parsing.modeling_contract_source_service, "profile_existing_tabular_file",
                        lambda *args: (_ for _ in ()).throw(AssertionError("Wrong usage plane")))
    monkeypatch.setattr(parsing.datasource_service, "read_bucket_file", lambda *args: (b"data", 4, "text/plain"))
    monkeypatch.setattr(parsing.doc_parser, "parse_bytes", lambda *args: {"status": "success", "text": "Runtime text"})
    result = parsing.parse_document(SimpleNamespace(resource_scope="agent_runtime"), SimpleNamespace(filename="data.csv"))
    assert "tabular_profile" not in result
