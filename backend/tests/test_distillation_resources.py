"""Selected resources must perform bounded reads rather than name-only claims."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models import MCPConfig, Skill
from app.services import distillation_resource_service as resources
from app.services import mcp_resource_service as transport
from app.services import skill_instruction_service
from app.services import distillation_mcp_evidence_service as mcp_evidence
from app.distillation_conversation_schemas import MCPMaterialRead
from app.distillation_schemas import Evidence
from app.distillation_schemas import DistillationDocument
from app.services import distillation_conversation_worker, distillation_evidence_service, distillation_service


def test_selected_skill_reads_exact_trusted_package_and_detects_content_changes(tmp_path, monkeypatch):
    root = tmp_path / "trusted"
    package = root / "method"
    package.mkdir(parents=True)
    path = package / "SKILL.md"
    path.write_text("Ask who benefits and cite the original evidence.", encoding="utf-8")
    monkeypatch.setattr(skill_instruction_service, "SKILLS_DIR", root)
    skill = Skill(source="builtin", path=str(package))
    before = resources.skill_content_fingerprint(skill)
    assert resources.skill_instructions(skill) == "Ask who benefits and cite the original evidence."
    path.write_text("Ask the human to resolve contradictory evidence.", encoding="utf-8")
    assert resources.skill_content_fingerprint(skill) != before
    with pytest.raises(ValueError):
        resources.skill_instructions(Skill(source="uploaded", path=str(package)))
    with pytest.raises(ValueError):
        resources.skill_instructions(Skill(source="builtin", path=str(tmp_path)))
    path.write_text("x" * (skill_instruction_service.MAX_SKILL_BYTES + 1), encoding="utf-8")
    with pytest.raises(ValueError):
        resources.skill_instructions(skill)


def test_resource_tools_are_present_only_for_selected_v3_resources_and_have_closed_arguments():
    snapshot = {"version": 3, "skills": [{"id": "method"}], "mcps": [{"id": "reference"}]}
    definitions = resources.definitions(snapshot)
    assert {item["function"]["name"] for item in definitions} == resources.TOOL_KEYS
    for item in definitions:
        parameters = item["function"]["parameters"]
        assert parameters["additionalProperties"] is False
        reference = "skill_id" if item["function"]["name"] == "read_selected_skill" else "mcp_id"
        assert parameters["properties"][reference]["enum"] == ["method" if reference == "skill_id" else "reference"]
    assert resources.definitions({"version": 3, "skills": [], "mcps": []}) == []
    assert resources.definitions({**snapshot, "version": 2}) == []
    with pytest.raises(ValueError):
        resources.validate_selected([], [MCPConfig(transport="stdio")])


def test_mcp_resource_adapter_uses_only_resource_protocol_and_bounds_output(monkeypatch):
    calls = []

    class Session:
        async def list_resources(self):
            calls.append("resources/list")
            return SimpleNamespace(resources=[SimpleNamespace(uri="memo://one", name="Overview", description="A source")], nextCursor=None)

        async def read_resource(self, uri):
            calls.append(("resources/read", str(uri)))
            return SimpleNamespace(contents=[SimpleNamespace(text="Observed evidence")])

    @asynccontextmanager
    async def session(_cfg):
        yield Session()

    monkeypatch.setattr(transport, "_session", session)
    cfg = MCPConfig(transport="streamable_http")
    assert asyncio.run(transport._list(cfg))["resources"][0]["uri"] == "memo://one"
    assert asyncio.run(transport._read(cfg, "memo://one")) == {"text": "Observed evidence", "read_only": True}
    assert calls == ["resources/list", ("resources/read", "memo://one")]
    monkeypatch.setattr(transport, "MAX_RESOURCE_TEXT", 5)
    with pytest.raises(ValueError, match="上限"):
        asyncio.run(transport._read(cfg, "memo://one"))


def test_remote_resource_failures_never_return_provider_errors_or_secret_content():
    async def failing():
        raise RuntimeError("provider error Authorization: Bearer synthetic-secret")

    with pytest.raises(transport.MCPResourceError) as failure:
        transport._run(failing())
    assert "synthetic-secret" not in str(failure.value)
    assert "只读资源协议" in str(failure.value)


def test_resource_fingerprint_changes_with_connector_revision_and_does_not_expose_uri():
    cfg = MCPConfig(id="connector", connector_revision=1)
    item = {"uri": "memo://private-record", "name": "Source", "description": ""}
    before = resources._resource_key(cfg, item)
    assert len(before) == 64 and "private-record" not in before
    cfg.connector_revision = 2
    assert resources._resource_key(cfg, item) != before


@pytest.mark.parametrize("prefix", ["", "Bearer ", "Basic "])
def test_echoed_opaque_mcp_credential_is_rejected_even_without_a_secret_label(prefix):
    cfg = MCPConfig(headers={"X-Connection-Key": prefix + "ephemeral-test-value"}, env={})
    with pytest.raises(ValueError, match="凭据"):
        resources._safe_mcp_result({"text": "Found ephemeral-test-value in server output"}, cfg)


def test_mcp_evidence_receipt_preserves_exact_text_and_rejects_tampering():
    text = "  A bounded observed result.\n"
    observation = MCPMaterialRead(mcp_id="connector", connector_revision=1, resource_key="a" * 64,
        title="Prior result", text=text, content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        retrieved_at=datetime.now(timezone.utc))
    record, receipt = mcp_evidence.record_read("turn", "step", observation)
    assert record["observation"]["text"] == text
    assert record["evidence"]["mcp_read"]["identity_sha256"] == receipt["identity_sha256"]
    assert receipt["evidence_key"] == "mcp_step" and receipt["read_only"] is True
    assert "text" not in receipt and len(receipt["summary"]) <= 1000
    with pytest.raises(HTTPException, match="内容与回执不一致"):
        mcp_evidence.identity_hash(observation.model_copy(update={"text": "Changed content"}))
    with pytest.raises(ValueError, match="不能混用"):
        Evidence.model_validate({**record["evidence"], "data_source_id": "unrelated"})


def test_mcp_resource_session_uses_bounded_transport_and_rejects_stdio(monkeypatch):
    calls = []
    sentinel = object()

    @asynccontextmanager
    async def bounded(cfg):
        calls.append(cfg.id)
        yield sentinel

    async def connect(cfg):
        async with transport._session(cfg) as session:
            assert session is sentinel

    monkeypatch.setattr(transport, "bounded_session", bounded)
    asyncio.run(connect(MCPConfig(id="connector", transport="streamable_http")))
    assert calls == ["connector"]
    with pytest.raises(ValueError, match="远程"):
        asyncio.run(connect(MCPConfig(id="stdio", transport="stdio")))
    assert calls == ["connector"]


def test_all_mcp_observations_that_fit_document_limit_remain_available_for_citations():
    records, steps = [], []
    for index in range(8):
        text = f"Observed result {index}"
        observation = MCPMaterialRead(mcp_id="connector", connector_revision=1, resource_key="a" * 64,
            title=f"Result {index}", text=text, content_sha256=hashlib.sha256(text.encode()).hexdigest(),
            retrieved_at=datetime.now(timezone.utc))
        step_id = f"step_{index}"
        record, receipt = mcp_evidence.record_read("turn", step_id, observation)
        records.append(record)
        steps.append({"id": step_id, "status": "succeeded", "mcp": receipt})
    row = SimpleNamespace(id="turn", project_id="project", turn_number=1, steps=steps,
        context={"document": DistillationDocument().model_dump(), "mcp_reads": records})
    db = SimpleNamespace(execute=lambda _query: SimpleNamespace(all=lambda: []))
    observed = distillation_conversation_worker._observations(db, row)
    assert {item.key for item in observed} == {item["evidence"]["key"] for item in records}


def test_legacy_documents_keep_evidence_identity_and_original_publication_bytes():
    legacy = DistillationDocument(evidence=[{"key": "interview", "title": "Human interview"}]).model_dump()
    for item in legacy["evidence"]:
        item.pop("mcp_read")
    legacy_bytes = json.dumps(legacy, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(legacy_bytes.encode()).hexdigest()
    parsed = DistillationDocument.model_validate(legacy)
    assert parsed.evidence[0].mcp_read is None
    expected_identity = {"identity_version": "business-distillation-evidence:v1", "evidence": [
        {"evidence_key": "interview", "kind": "observation", "role": "reference", "basis": "human_recorded_observation"}]}
    assert distillation_evidence_service.capture_evidence_identity(None, parsed, None) == expected_identity
    publication = SimpleNamespace(artifacts=[{"key": "contract", "content": legacy_bytes, "sha256": digest}])
    returned = distillation_service.artifact_content(publication, "contract")
    assert returned["content"] == legacy_bytes and returned["sha256"] == digest
    assert "mcp_read" not in publication.artifacts[0]["content"]
