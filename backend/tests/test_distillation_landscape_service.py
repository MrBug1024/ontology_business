from types import SimpleNamespace

from app.distillation_schemas import DistillationDocument, Evidence, LibraryReadReference
from app.services import distillation_landscape_service as landscape
from app.services import distillation_conversation_tools
from app.services import distillation_library_service, library_database_service
from app.services.library_sample_query import catalog


class _ScalarResult:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _Db:
    def __init__(self, sources):
        self.sources = sources
        self.commits = 0

    def scalars(self, _statement):
        return _ScalarResult(self.sources)

    def commit(self):
        self.commits += 1


def _schema():
    return [
        {"name": "orders", "columns": [
            {"name": "order_id", "type": "text", "pk": True},
            {"name": "customer_id", "type": "text", "pk": False},
        ]},
        {"name": "events", "columns": [
            {"name": "event_id", "type": "text", "pk": True},
            {"name": "order_id", "type": "text", "pk": False},
        ]},
    ]


def test_discover_automatically_samples_authorized_sources_and_finds_links(monkeypatch):
    source = SimpleNamespace(id="source-a", type="postgres", name="业务资料库", created_at=1)
    db = _Db([source])
    schema = _schema()
    tables = catalog(schema)

    monkeypatch.setattr(
        landscape.distillation_library_service,
        "authorized_source_filters",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(library_database_service, "snapshot", lambda item: item)
    monkeypatch.setattr(landscape.permission_service, "refresh_request_authorization", lambda _db: None)
    monkeypatch.setattr(
        landscape,
        "capture_evidence_identity",
        lambda _db, document, _scenario_id: {"keys": [item.key for item in document.evidence]},
    )

    def read_schema(_source, *, sample=None, **_kwargs):
        if sample is None:
            return schema
        table = next(item for item in tables if item["table_key"] == sample.table_key)
        values = {
            "orders": {"order_id": ["1", "2"], "customer_id": ["c1", "c2"]},
            "events": {"event_id": ["e1", "e2"], "order_id": ["1", "2"]},
        }[table["name"]]
        rows = []
        for index in range(2):
            rows.append({
                field["field_key"]: values[field["name"]][index]
                for field in table["fields"]
            })
        return {"kind": "database_sample", "table": table, "rows": rows, "excluded_cells": [], "has_more": False}

    monkeypatch.setattr(library_database_service, "database_schema", read_schema)

    content, reads = landscape.discover(db, "scenario-a")

    assert db.commits == 1
    assert len(reads) == 1
    assert content["sources"][0]["sample_count"] == 2
    assert content["candidate_stats"]["candidate_count"] >= 1
    candidate = content["relationship_candidates"][0]
    assert candidate["relation_type"] == "possible_link"
    assert candidate["evidence"]["shared_distinct_values"] == 2


def test_multi_source_step_resolves_the_requested_receipt(monkeypatch):
    first_identity = {"source": "first"}
    second_identity = {"source": "second"}
    first_evidence = {
        "key": "first_read",
        "title": "第一份",
        "kind": "material",
        "data_source_id": "source-first",
        "summary": "",
        "coverage": "",
        "limitations": "",
        "library_read": {"turn_id": "turn", "step_id": "step", "identity_sha256": distillation_library_service.identity_hash(first_identity)},
    }
    second_evidence = {
        **first_evidence,
        "key": "second_read",
        "title": "第二份",
        "data_source_id": "source-second",
        "library_read": {"turn_id": "turn", "step_id": "step", "identity_sha256": distillation_library_service.identity_hash(second_identity)},
    }
    row = SimpleNamespace(project_id="project", context={"library_reads": [
        {"step_id": "step", "evidence": first_evidence, "identity": first_identity, "content": {"marker": "first"}},
        {"step_id": "step", "evidence": second_evidence, "identity": second_identity, "content": {"marker": "second"}},
    ]})
    db = SimpleNamespace(
        scalar=lambda _statement: row,
        get=lambda _model, _id: SimpleNamespace(id="project", scenario_id="scenario"),
    )
    requested = second_evidence.copy()
    requested["library_read"] = second_evidence["library_read"]

    monkeypatch.setattr(distillation_library_service.permission_service, "require_principal", lambda _db: SimpleNamespace(tenant_id="tenant"))
    monkeypatch.setattr(distillation_library_service.distillation_service, "authorize_scope", lambda *_args: None)
    monkeypatch.setattr(distillation_library_service, "assert_read_current", lambda *_args: None)

    from app.distillation_schemas import Evidence

    resolved = distillation_library_service.resolve_read(db, Evidence.model_validate(requested), "scenario")

    assert resolved["content"] == {"marker": "second"}


def test_lineage_tool_uses_actual_observation_receipts(monkeypatch):
    first = Evidence(
        key="read_first",
        title="第一份样本",
        kind="material",
        data_source_id="source-first",
        library_read=LibraryReadReference(
            turn_id="turn",
            step_id="step-first",
            identity_sha256="a" * 64,
        ),
    )
    second = Evidence(
        key="read_second",
        title="第二份样本",
        kind="material",
        data_source_id="source-second",
        library_read=LibraryReadReference(
            turn_id="turn",
            step_id="step-second",
            identity_sha256="b" * 64,
        ),
    )
    contents = {
        first.key: {
            "kind": "database_sample",
            "table": {"name": "orders", "fields": [{"field_key": "order_id", "name": "order_id"}]},
            "rows": [{"order_id": "1"}, {"order_id": "2"}],
        },
        second.key: {
            "kind": "database_sample",
            "table": {"name": "events", "fields": [{"field_key": "order_id", "name": "order_id"}]},
            "rows": [{"order_id": "1"}, {"order_id": "2"}],
        },
    }
    monkeypatch.setattr(
        distillation_library_service,
        "resolve_read",
        lambda _db, evidence, _scenario_id: {"content": contents[evidence.key]},
    )

    result = distillation_conversation_tools.execute(
        SimpleNamespace(),
        "infer_data_lineage",
        {"evidence_keys": [first.key, second.key]},
        DistillationDocument(),
        "scenario-a",
        observations=[first, second],
    )

    assert result.content["stats"]["candidate_count"] == 1
    assert result.content["candidates"][0]["source"]["table"] == "orders"
