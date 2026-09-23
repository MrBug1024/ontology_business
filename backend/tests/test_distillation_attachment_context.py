from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.services import distillation_conversation_service as conversation
from app.services import distillation_attachment_service as attachments
from app.services.distillation_conversation_worker import _attachment_context_message, _failure_message


def test_attachment_turn_keeps_bounded_attachment_tools_when_selection_is_narrowed():
    row = SimpleNamespace(context={
        "attachment_ids": ["attachment-id"],
        "resource_selection": {
            "version": 2,
            "requested_llm_config_id": None,
            "llm": {"id": "model-id"},
            "skills": [],
            "mcps": [],
            "investigation_tools": {
                "mode": "selected",
                "selected_tool_keys": [],
                "effective_tool_keys": ["ask_human", "propose_document"],
            },
        },
    })

    keys = conversation.effective_turn_tool_keys(row)

    assert "list_evidence" in keys
    assert "read_attachment" in keys


def test_attachment_context_contains_metadata_without_parsed_body():
    row = SimpleNamespace(context={"attachments": [{
        "id": "attachment-id",
        "filename": "report.txt",
        "media_type": "text/plain",
        "byte_size": 12,
        "content_sha256": "a" * 64,
        "parsed_text": "secret body that must stay behind the tool",
    }]})

    content = _attachment_context_message(row)

    assert "report.txt" in content
    assert "attachment-id" in content
    assert "secret body that must stay behind the tool" not in content


def test_worker_explains_attachment_context_conflict_without_exposing_boundary_details():
    error = _failure_message(HTTPException(409, "本轮临时附件已移除或过期，请重新上传后发送"))

    assert error == "本轮使用的临时附件已被移除或已过期，请重新上传后再发送。"
    assert "项目、权限或资料已变化" not in error


def test_worker_explains_frozen_resource_conflict():
    error = _failure_message(HTTPException(409, conversation.RESOURCE_UNAVAILABLE_MESSAGE))

    assert error == "本轮选择的模型、技能方法、MCP资料连接或调查工具已不可用，请刷新后重新选择。"


def test_binding_adopts_owned_preproject_attachment(monkeypatch):
    row = SimpleNamespace(
        id="attachment-id", project_id=None, scenario_id=None, tenant_id="tenant-id",
        created_by="user-id", status="ready",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    class _Db:
        def __init__(self):
            self.added = []

        def scalars(self, _statement):
            return iter([row])

        def add(self, value):
            self.added.append(value)

        def flush(self):
            return None

    class _Public:
        def model_dump(self, mode="json"):
            return {"id": "attachment-id"}

    monkeypatch.setattr(attachments, "public_attachment", lambda _row: _Public())
    turn = SimpleNamespace(
        id="turn-id", project_id="project-id", tenant_id="tenant-id", created_by="user-id",
        context={"scenario_id": "scenario-id"},
    )

    result = attachments.bind(_Db(), turn, ["attachment-id"])

    assert result == [{"id": "attachment-id"}]
    assert row.project_id == "project-id"
    assert row.scenario_id == "scenario-id"
    assert row.status == "bound"
