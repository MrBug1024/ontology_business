"""Conversation-driven delivery of scenario stage documents to materials."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.distillation_schemas import DistillationDocument
from app.services import distillation_conversation_tools as tools
from app.services import distillation_publication_service as handoff
from app.services import distillation_service


def _complete_document(**overrides) -> DistillationDocument:
    fields = {
        "beneficiary": "审核员",
        "pain": "审批耗时过长",
        "desired_outcome": "自动分派审批路由",
        "success_metric": "审批时长下降 50%",
        "scope": "审批分派",
        "non_goals": "不做自动放行",
    }
    fields.update(overrides)
    return DistillationDocument(**fields)


def _state(document: DistillationDocument, revision: int = 4) -> SimpleNamespace:
    return SimpleNamespace(id="state", tenant_id="tenant", revision=revision, document=document.model_dump())


def _publication_stub(document: DistillationDocument, revision: int = 4) -> SimpleNamespace:
    return SimpleNamespace(
        id="pub", tenant_id="tenant", data_source_id="source", scenario_id="scene",
        project_revision=revision, document=document.model_dump(),
        artifacts=[{"key": "brief", "filename": "business-brief.md", "sha256": "0" * 64, "mime": "text/markdown", "content": ""}],
        created_at=datetime(2026, 10, 10, tzinfo=timezone.utc),
    )


def test_delivery_tool_is_always_available_and_cannot_be_deselected():
    assert "deliver_to_library" in tools.ALWAYS_AVAILABLE_TOOL_KEYS
    assert "deliver_to_library" not in tools.selectable_tool_keys()
    assert tools.tool_title("deliver_to_library") == "提交阶段产物到场景资料"
    definitions = tools.definitions(tools.effective_tool_keys(None))
    assert "deliver_to_library" in {item["function"]["name"] for item in definitions}


def test_delivery_delivers_current_baseline_without_mutating_it(monkeypatch):
    document = _complete_document()
    state = _state(document)
    monkeypatch.setattr(distillation_service, "scenario_state", lambda *args, **kwargs: state)
    saved = {}
    def create_publication(db, **kwargs):
        saved.update(kwargs)
        return _publication_stub(kwargs["document"])
    monkeypatch.setattr(handoff, "create_publication", create_publication)
    records = iter([None, SimpleNamespace(name="审批场景")])
    db = SimpleNamespace(scalar=lambda statement: next(records), flush=lambda: None)

    publication, created = handoff.deliver_scenario_documents(db, "scene", "专家确认按当前结论提交")

    assert created is True
    # The evolving baseline must stay untouched: no decision merge, no revision bump.
    assert state.document == document.model_dump()
    assert state.revision == 4
    assert saved["project_id"] is None
    assert saved["scenario_state_id"] == "state"
    assert saved["revision"] == 4
    assert saved["document"].decision == "continue"
    assert saved["document"].decision_reason == "专家确认按当前结论提交"
    assert publication is not None


def test_delivery_is_idempotent_per_baseline_revision(monkeypatch):
    document = _complete_document()
    state = _state(document, revision=6)
    monkeypatch.setattr(distillation_service, "scenario_state", lambda *args, **kwargs: state)
    existing = _publication_stub(document, revision=6)
    def create_publication(*args, **kwargs):
        pytest.fail("an existing delivery must be returned without creating another")
    monkeypatch.setattr(handoff, "create_publication", create_publication)
    db = SimpleNamespace(scalar=lambda statement: existing)

    publication, created = handoff.deliver_scenario_documents(db, "scene", "再次提交")

    assert created is False
    assert publication is existing
    assert state.revision == 6


def test_delivery_keeps_an_explicit_human_decision_and_reason(monkeypatch):
    document = _complete_document(decision="adjust", decision_reason="先调整流程再建设")
    state = _state(document)
    monkeypatch.setattr(distillation_service, "scenario_state", lambda *args, **kwargs: state)
    saved = {}
    monkeypatch.setattr(handoff, "create_publication", lambda db, **kwargs: saved.update(kwargs) or _publication_stub(kwargs["document"]))
    records = iter([None, SimpleNamespace(name="审批场景")])
    db = SimpleNamespace(scalar=lambda statement: next(records), flush=lambda: None)

    handoff.deliver_scenario_documents(db, "scene", "专家要求提交")

    assert saved["document"].decision == "adjust"
    assert saved["document"].decision_reason == "先调整流程再建设"


def test_delivery_reports_missing_fields_and_refuses_stop(monkeypatch):
    incomplete = _complete_document(beneficiary="")
    monkeypatch.setattr(distillation_service, "scenario_state", lambda *args, **kwargs: _state(incomplete))
    db = SimpleNamespace(scalar=lambda statement: None)
    with pytest.raises(HTTPException) as caught:
        handoff.deliver_scenario_documents(db, "scene", "提交")
    assert caught.value.status_code == 422
    assert "受益者" in caught.value.detail

    stopped = _complete_document(decision="stop", decision_reason="暂缓")
    monkeypatch.setattr(distillation_service, "scenario_state", lambda *args, **kwargs: _state(stopped))
    with pytest.raises(HTTPException) as caught:
        handoff.deliver_scenario_documents(db, "scene", "提交")
    assert caught.value.status_code == 422
    assert "暂缓" in caught.value.detail


def test_delivery_requires_a_scenario_bound_conversation():
    result = tools.execute(object(), "deliver_to_library", {"note": "提交当前产物"},
                           DistillationDocument(), None, observations=[])
    assert result.content["status"] == "blocked"
    assert "未关联业务场景" in result.content["reason"]
    assert result.delivery is None


def test_delivery_tool_returns_real_receipt(monkeypatch):
    delivered = _complete_document(decision="continue", decision_reason="专家确认")
    publication = _publication_stub(delivered)
    monkeypatch.setattr("app.services.distillation_publication_service.deliver_scenario_documents",
                        lambda db, scenario_id, note: (publication, True))
    source = SimpleNamespace(name="审批场景 · 业务蒸馏 v4")
    db = SimpleNamespace(scalar=lambda statement: source)

    result = tools.execute(db, "deliver_to_library", {"note": "专家确认"}, DistillationDocument(), "scene", observations=[])

    assert result.content["delivered"] is True
    assert result.content["data_source_id"] == "source"
    assert result.summary.startswith("已提交到场景资料")
    assert result.delivery is not None
    assert result.delivery.name == "审批场景 · 业务蒸馏 v4"
    assert result.delivery.already_delivered is False
    assert result.delivery.artifacts[0].filename == "business-brief.md"
    assert result.delivery.decision == "continue"
    # The tool message is embedded into the model conversation as JSON.
    assert json.dumps(result.content, ensure_ascii=False)


def test_delivery_tool_maps_handoff_rejection_to_blocked_result(monkeypatch):
    def refused(db, scenario_id, note):
        raise HTTPException(422, "提交前请先补齐：成功标准")
    monkeypatch.setattr("app.services.distillation_publication_service.deliver_scenario_documents", refused)

    result = tools.execute(object(), "deliver_to_library", {"note": "提交"}, DistillationDocument(), "scene", observations=[])

    assert result.content["status"] == "blocked"
    assert result.content["reason"] == "提交前请先补齐：成功标准"
    assert result.delivery is None
