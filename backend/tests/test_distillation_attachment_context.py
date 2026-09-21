from types import SimpleNamespace

from app.services import distillation_conversation_service as conversation
from app.services.distillation_conversation_worker import _attachment_context_message


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
