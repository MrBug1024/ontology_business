"""Advisor resource reads are real, bounded, isolated and safe to persist."""
import asyncio
from dataclasses import replace
from contextlib import asynccontextmanager
import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import assistant
from app.schemas import AssistantChatRequest
from app.services import assistant_resource_context_service as resources
from app.services import modeling_reference_contract as contract


def reference_document(text="Ask which business facts support each relationship."):
    return contract.freeze([contract.ModelingReference(kind="skill_method", resource_id="method", name="Discovery method",
        content=text, content_hash=hashlib.sha256(text.encode()).hexdigest())])


def selection():
    return resources.SelectedResources(
        (resources.SkillSnapshot("method", "Discovery method", "builtin", "/trusted/method"),),
        (resources.McpSnapshot("connector", "Contract catalog", "streamable_http", 3,
            "https://synthetic.example.invalid/mcp", (("Authorization", "Bearer synthetic-only-value"),),
            ("https://synthetic.example.invalid/mcp", "synthetic-only-value")),),
    )


def test_advisor_reads_skill_method_and_mcp_input_contract_without_executing_them(monkeypatch):
    calls = []
    def instructions(skill):
        calls.append(("skill", skill.id))
        return "Ask which business facts support each relationship."
    async def tools(config):
        calls.append(("tools/list", config.id))
        return [{"name": "inspect_case", "description": "Read the declared case structure", "input_schema": {
            "type": "object", "properties": {"case_key": {"type": "string"}}, "required": ["case_key"], "additionalProperties": False}}]
    monkeypatch.setattr(resources.skill_instruction_service, "read_instructions", instructions)
    monkeypatch.setattr(resources, "_list_mcp_tools", tools)
    document = resources.read(selection())
    prompt = contract.prompt(document)
    assert calls == [("skill", "method"), ("tools/list", "connector")]
    assert "business facts support each relationship" in prompt
    assert "case_key" in prompt
    assert "不是客户业务事实" in prompt
    assert "未执行技能脚本或 MCP 工具" in prompt
    assert "synthetic-only-value" not in prompt
    assert "https://synthetic.example.invalid" not in prompt
    assert document["references"][1]["revision"] == 3
    sources = resources.sources(document)
    assert [item["kind"] for item in sources] == ["skill_method", "mcp_tool_catalog"]
    assert all(item["usage_plane"] == "modeling_reference" and item["formalization_allowed"] is False for item in sources)
    assert "instructions" not in json.dumps(sources)


def test_secret_fields_never_enter_snapshot_repr():
    representation = repr(selection())
    assert "/trusted/method" not in representation
    assert "synthetic-only-value" not in representation
    assert "https://" not in representation


def test_mcp_adapter_only_lists_tools_inside_bounded_session(monkeypatch):
    calls = []
    @asynccontextmanager
    async def bounded(config, **limits):
        calls.append((config.id, limits))
        class RemoteSession:
            async def list_tools(self):
                calls.append("tools/list")
                return SimpleNamespace(nextCursor=None, tools=[SimpleNamespace(name="inspect", description="Read contract", inputSchema={"type": "object"})])
            async def call_tool(self, *_args):
                pytest.fail("catalog loading must not call tools")
        yield RemoteSession()
    monkeypatch.setattr(resources.mcp_bounded_transport_service, "bounded_session", bounded)
    result = asyncio.run(resources._list_mcp_tools(SimpleNamespace(id="connector")))
    assert result == [{"name": "inspect", "description": "Read contract", "input_schema": {"type": "object"}}]
    assert calls[1] == "tools/list"
    assert calls[0][1]["max_response_bytes"] == resources.MAX_MCP_RESPONSE_BYTES
    assert calls[0][1]["timeout_seconds"] == resources.MAX_TOTAL_READ_SECONDS


@pytest.mark.parametrize("raw", [
    [{"name": "inspect", "description": "synthetic-only-value", "input_schema": {}}],
    [{"name": "inspect", "description": "https://synthetic.example.invalid/mcp", "input_schema": {}}],
    [{"name": "inspect", "description": "safe", "input_schema": {"default": float("nan")}}],
    [{"name": "inspect", "description": "safe", "input_schema": {}}] * 41,
    [{"name": f"inspect{index}", "description": "界" * 3900, "input_schema": {}} for index in range(3)],
])
def test_external_catalog_rejects_sensitive_invalid_and_oversized_content(raw):
    with pytest.raises(resources.AssistantResourceUnavailable):
        resources._catalog(raw, selection().mcps[0])


def test_external_error_and_timeout_are_safe_and_never_fall_back_to_names(monkeypatch):
    selected = resources.SelectedResources(mcps=selection().mcps)
    async def broken(_config):
        raise RuntimeError("provider said Authorization Bearer synthetic-only-value https://secret.invalid")
    monkeypatch.setattr(resources, "_list_mcp_tools", broken)
    with pytest.raises(resources.AssistantResourceUnavailable) as error:
        resources.read(selected)
    assert "synthetic" not in str(error.value)
    assert "https" not in str(error.value)
    async def slow(_config):
        await asyncio.sleep(1)
        pytest.fail("timed out call was not cancelled")
    monkeypatch.setattr(resources, "_list_mcp_tools", slow)
    monkeypatch.setattr(resources, "MAX_TOTAL_READ_SECONDS", 0.01)
    with pytest.raises(resources.AssistantResourceUnavailable, match="超时"):
        resources.read(selected)


def test_route_releases_read_transaction_before_resources_and_rechecks_after(monkeypatch):
    events = []
    selected = selection()
    document = reference_document()
    db = SimpleNamespace(new=set(), dirty=set(), deleted=set(), in_transaction=True)
    def resolve(_db, skill_ids, mcp_ids):
        assert skill_ids == ["method"]
        events.append("resolve")
        return selected
    def rollback():
        db.in_transaction = False
        events.append("rollback")
    def read(snapshot):
        assert snapshot == selected
        assert not db.in_transaction
        events.append("read")
        return document
    db.rollback = rollback
    monkeypatch.setattr(resources, "resolve", resolve)
    monkeypatch.setattr(resources, "read", read)
    monkeypatch.setattr(resources, "revalidate", lambda *_args: events.append("revalidate"))
    result = assistant._assistant_capability_context(db, AssistantChatRequest(message="Investigate", skill_ids=["method"]))
    assert result == document
    assert events == ["resolve", "rollback", "read", "revalidate"]


def test_changed_connector_revision_fails_closed_after_read(monkeypatch):
    before = selection()
    changed = replace(before, mcps=(replace(before.mcps[0], revision=4),))
    monkeypatch.setattr(resources.permission_service, "refresh_request_authorization", lambda _db: None)
    monkeypatch.setattr(resources, "resolve", lambda *_args: changed)
    with pytest.raises(resources.AssistantResourceUnavailable, match="变化"):
        resources.revalidate(object(), before)


def test_resource_limits_reject_before_lookup():
    with pytest.raises(resources.AssistantResourceUnavailable, match="最多"):
        resources.resolve(object(), [str(index) for index in range(6)], [])
    with pytest.raises(resources.AssistantResourceUnavailable, match="重复"):
        resources.resolve(object(), ["same", "same"], [])


def test_selected_model_disappearing_during_resource_read_does_not_use_default(monkeypatch):
    monkeypatch.setattr(assistant.llm_service, "routable_configs", lambda *_args: [SimpleNamespace(id="default-model")])
    with pytest.raises(HTTPException, match="所选 AI 模型"):
        assistant._llm(SimpleNamespace(info={"assistant_llm_config_id": "removed-model"}))


def test_resource_reader_refuses_dirty_transaction(monkeypatch):
    monkeypatch.setattr(resources, "resolve", lambda *_args: selection())
    monkeypatch.setattr(resources, "read", lambda *_args: pytest.fail("must not read during a write"))
    with pytest.raises(HTTPException, match="保存变更前"):
        assistant._assistant_capability_context(SimpleNamespace(new={"pending"}, dirty=set(), deleted=set()),
            AssistantChatRequest(message="Inspect", skill_ids=["method"]))
