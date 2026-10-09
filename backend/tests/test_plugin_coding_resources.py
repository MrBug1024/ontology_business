from copy import deepcopy
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.config import SKILLS_DIR
from app.plugin_coding_schemas import CodingResourceSelection, PluginCodingSettings, PluginCodingUpdate
from app.services import plugin_coding_resources as resources, plugin_coding_resource_runtime as runtime
from app.services import plugin_coding_tools as tools, plugin_coding_workspace as workspace
from app.services.plugin_native_coding import native_coding_steps
from app.services.plugin_coding_worker import CodingStep, apply_step, finish_document, parse_tool_steps, log_boundary_failure, tool_diagnostic, record_failure_diagnostic
from app.services.plugin_coding_validation import files_hash
from app.services.plugin_coding_repair import discuss_project


def trusted_skill(name="plugin-authoring"):
    return SimpleNamespace(id="skill", name=name, path=str(SKILLS_DIR / name), source="builtin",
        meta={"version": "1.0.0"}, enabled=True, description="Authoring method")


def model(tools_enabled=True):
    return SimpleNamespace(id="model", enabled=True, capabilities=["chat", "tool"] if tools_enabled else ["chat"])


class DB:
    def __init__(self, row=None, run=None):
        self.row, self.run = row, run
        self.info = {"tenant_id": "tenant", "user_id": "user"}
        self.commits = 0
        self.rollbacks = 0

    def scalar(self, statement):
        return self.row

    def get(self, kind, identity):
        return self.run

    def expunge(self, row):
        assert row is self.row

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        self.commits += 1


@pytest.fixture
def authorized(monkeypatch):
    monkeypatch.setattr(resources.permission_service, "require_principal", lambda db: SimpleNamespace(tenant_id="tenant"))
    monkeypatch.setattr(resources.tenant_service, "get_visible", lambda *args: model())
    monkeypatch.setattr(resources.tenant_service, "visible_clause", lambda *args: True)


def test_authoring_skills_are_trusted_methods_and_freeze_exact_content(authorized):
    skill = trusted_skill()
    value, snapshot = resources.freeze_selection(DB(skill), {"llm_config_id": "model", "skill_ids": ["skill"]})
    context = resources.prepare_context(DB(skill), {"llm_config_id": "model", "resource_selection": value,
                                                "resource_snapshot": snapshot})
    assert context["authoring_skills"][0]["version"] == "1.0.0"
    assert context["authoring_skills"][0]["instructions"] == resources.read_instructions(skill)
    assert len(snapshot["skills"][0]["content_sha256"]) == 64
    assert "instructions" not in str(snapshot)
    skill.meta = {"version": "2.0.0"}
    with pytest.raises(ValueError, match="失效"):
        resources.prepare_context(DB(skill), {"llm_config_id": "model", "resource_selection": value,
                                            "resource_snapshot": snapshot})


def test_codex_authoring_method_freezes_independently_without_replacing_legacy_skills(authorized):
    from app.services.skill_service import scan_skills

    item = next(item for item in scan_skills() if item['name'] == 'plugin-codex-authoring')
    skill = trusted_skill('plugin-codex-authoring')
    skill.meta = item['metadata']
    value, snapshot = resources.freeze_selection(DB(skill), {'llm_config_id': 'model', 'skill_ids': ['skill']})
    context = resources.prepare_context(DB(skill), {'llm_config_id': 'model', 'resource_selection': value,
                                                   'resource_snapshot': snapshot})
    method = context['authoring_skills'][0]
    assert method['instructions'] == resources.read_instructions(skill)
    assert method['version'] == '1.0.0'
    assert resources.AUTHORING_SKILL_VERSIONS['plugin-authoring'] == '1.0.0'
    assert resources.AUTHORING_SKILL_VERSIONS['plugin-contract-review'] == '1.0.0'
    stale = deepcopy(snapshot)
    stale['skills'][0]['content_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='失效'):
        resources.prepare_context(DB(skill), {'llm_config_id': 'model', 'resource_selection': value,
                                             'resource_snapshot': stale})


def test_scanned_standard_skill_metadata_version_installs_and_old_content_snapshot_is_rejected(authorized):
    from app.services.skill_service import scan_skills

    item = next(item for item in scan_skills() if item['name'] == 'plugin-authoring')
    assert item['metadata']['metadata']['version'] == '1.0.0'
    skill = trusted_skill()
    skill.meta = item['metadata']
    value, frozen = resources.freeze_selection(DB(skill), {'llm_config_id': 'model', 'skill_ids': ['skill']})
    context = resources.prepare_context(DB(skill), {'llm_config_id': 'model', 'resource_selection': value,
                                                   'resource_snapshot': frozen})
    assert context['authoring_skills'][0]['version'] == '1.0.0'
    stale = deepcopy(frozen)
    stale['skills'][0]['content_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='失效'):
        resources.prepare_context(DB(skill), {'llm_config_id': 'model', 'resource_selection': value,
                                             'resource_snapshot': stale})
    skill.meta['version'] = '1.0.0'
    skill.meta['metadata']['version'] = '2.0.0'
    with pytest.raises(ValueError, match='失效'):
        resources.freeze_selection(DB(skill), {'llm_config_id': 'model', 'skill_ids': ['skill']})


@pytest.mark.parametrize("name", ["ocr-parser", "data-analyzer", "business-discovery"])
def test_unregistered_skills_cannot_be_installed_into_coding(name, authorized):
    with pytest.raises(ValueError, match="失效"):
        resources.freeze_selection(DB(trusted_skill(name)), {"llm_config_id": "model", "skill_ids": ["skill"]})


def test_skill_path_cannot_swap_to_another_trusted_package(authorized):
    skill = trusted_skill()
    skill.path = str(SKILLS_DIR / "plugin-contract-review")
    with pytest.raises(ValueError, match="失效"):
        resources.freeze_selection(DB(skill), {"llm_config_id": "model", "skill_ids": ["skill"]})


def test_legacy_model_only_project_works_but_extension_snapshots_fail_closed(authorized):
    assert resources.prepare_context(DB(), {"llm_config_id": "model"}) == {"authoring_skills": [], "authoring_mcps": []}
    document = {"llm_config_id": "model", "resource_selection": {"llm_config_id": "model", "skill_ids": ["skill"]}}
    with pytest.raises(ValueError, match="失效"):
        resources.prepare_context(DB(trusted_skill()), document)
    with pytest.raises(ValueError, match="失效"):
        resources.prepare_context(DB(), {"llm_config_id": "model", "resource_snapshot": {"version": 2, "skills": [], "mcps": []}})
    with pytest.raises(ValueError, match="失效"):
        resources.selection({"llm_config_id": "model", "resource_selection": {"llm_config_id": "other"}})


def test_readonly_mcp_requires_native_tool_model(monkeypatch, authorized):
    monkeypatch.setattr(resources.tenant_service, "get_visible", lambda *args: model(False))
    with pytest.raises(HTTPException) as error:
        resources.freeze_selection(DB(), {"llm_config_id": "model", "mcp_ids": ["mcp"]})
    assert error.value.status_code == 409
    assert "工具调用" in error.value.detail


def test_selection_and_settings_are_closed_and_reject_duplicates():
    with pytest.raises(ValidationError):
        CodingResourceSelection(llm_config_id="model", skill_ids=["skill", "skill"])
    with pytest.raises(ValidationError):
        PluginCodingSettings(expected_revision=1, request_id="request-id", llm_config_id="model", command="shell")
    with pytest.raises(ValidationError):
        CodingResourceSelection(llm_config_id="model", mcp_ids=["mcp"] * 11)


def test_settings_preserve_reviewed_source_and_retries_are_idempotent(monkeypatch, authorized):
    original = {"manifest": {"scenario": {"id": "scenario"}}, "llm_config_id": "model",
        "revision": 4, "phase": "released", "active_run_id": None, "files": {"README.md": "source"}, "validation": []}
    row = SimpleNamespace(proposal=deepcopy(original))
    monkeypatch.setattr(workspace, "owned_root", lambda *args, **kwargs: row)
    monkeypatch.setattr(workspace, "public_workspace", lambda db, root: deepcopy(root.proposal))
    monkeypatch.setattr(resources.release_service, "_scenario_for_manage", lambda *args: None)
    db = DB()
    request = PluginCodingSettings(expected_revision=4, request_id="request-id", llm_config_id="model")
    saved = resources.update_settings(db, "workspace", request)
    assert saved["phase"] == "released" and saved["files"] == original["files"]
    assert saved["revision"] == 5 and db.commits == 1
    assert resources.update_settings(db, "workspace", request) == saved
    assert db.commits == 1
    with pytest.raises(HTTPException) as changed:
        resources.update_settings(db, "workspace", request.model_copy(update={"llm_config_id": "other"}))
    assert changed.value.status_code == 409
    with pytest.raises(HTTPException) as stale:
        resources.update_settings(db, "workspace", request.model_copy(update={"request_id": "new-request"}))
    assert stale.value.status_code == 409


def test_running_round_blocks_settings_without_mutating_source(monkeypatch, authorized):
    original = {"manifest": {"scenario": {"id": "scenario"}}, "llm_config_id": "model",
                "revision": 4, "phase": "generating", "active_run_id": "run", "files": {}, "events": []}
    row = SimpleNamespace(proposal=deepcopy(original))
    monkeypatch.setattr(workspace, "owned_root", lambda *args, **kwargs: row)
    monkeypatch.setattr(resources.release_service, "_scenario_for_manage", lambda *args: None)
    with pytest.raises(HTTPException) as busy:
        resources.update_settings(DB(run=SimpleNamespace(status="running")), "workspace",
            PluginCodingSettings(expected_revision=4, request_id="request-id", llm_config_id="model"))
    assert busy.value.status_code == 409 and "先停止" in busy.value.detail
    assert row.proposal == original


def test_native_model_can_inspect_then_submit_using_actual_tool_acknowledgements():
    conversations = []
    def stream(messages):
        conversations.append(deepcopy(messages))
        function = {"name": "inspect_plugin_files", "arguments": {}} if len(conversations) == 1 else {
            "name": "submit_plugin_step", "arguments": {"kind": "summary", "message": "Explained"}}
        yield {"type": "tool_calls", "tool_calls": [{"id": "call-" + str(len(conversations)), "function": function}]}
    steps = list(native_coding_steps([], stream=stream, parse=parse_tool_steps,
        execute_tool=lambda name, args: tools.execute({"files": {"README.md": "source"}, "plugin_version": "1.0.0",
            "manifest": {"package_name": "scenario-fixture", "capabilities": []}}, name, args)))
    assert [step.kind for step in steps] == ["summary"]
    assert '"path": "README.md"' in conversations[1][-1]["content"]
    assert conversations[1][-1]["tool_call_id"] == "call-1"


def test_native_discussion_acknowledgement_does_not_request_file_delivery():
    conversations = []
    def stream(messages):
        conversations.append(deepcopy(messages))
        yield {"type": "tool_calls", "tool_calls": [{"id": str(len(conversations)), "function": {
            "name": "submit_plugin_step", "arguments": {"kind": "plan" if len(conversations) == 1 else "summary", "message": "Discuss"}}}]}
    list(native_coding_steps([], stream=stream, parse=parse_tool_steps, mode="discuss"))
    assert "Do not submit files" in conversations[1][-1]["content"]
    assert "Continue required files" not in conversations[1][-1]["content"]


def test_file_inspection_search_and_validation_are_bounded_without_execution():
    document = {"files": {"README.md": "match\n" * 35, "references/large.md": "a" * 60_000},
                "manifest": {"package_name": "scenario-fixture", "capabilities": []}, "plugin_version": "1.0.0",
                "coding_contract": {"capabilities": []}}
    inspected = tools.execute(document, "inspect_plugin_files", {"paths": ["references/large.md"]})
    assert len(inspected["files"][0]["content"]) == tools.MAX_INSPECTION_CHARS
    assert inspected["files"][0]["truncated"] is True
    result = tools.execute(document, "search_plugin_files", {"query": "match"})
    assert len(result["matches"]) == 20 and result["has_more"] is True
    with pytest.raises(ValueError):
        tools.execute(document, "inspect_plugin_files", {"paths": ["../escape"]})
    result = tools.execute(document, "validate_plugin_project", {})
    assert not result["valid"] and result["code_executed"] is False


def test_current_project_inspection_includes_readonly_adapter_and_frozen_manifest():
    document = {"manifest": {"package_name": "scenario-fixture", "scenario": {"id": "scenario"},
                            "deployment": {"release_id": "release"}, "capabilities": []},
                "plugin_version": "1.0.0", "files": {"README.md": "Current edited description"}}
    inspected = tools.execute(document, "inspect_plugin_files", {"paths": ["server.py", "references/scenario.json"]})
    assert "invoke_scenario_capability" in inspected["files"][0]["content"]
    assert '"release_id": "release"' in inspected["files"][1]["content"]
    directory = tools.execute(document, "inspect_plugin_files", {})["files"]
    by_path = {item["path"]: item for item in directory}
    assert by_path["server.py"]["editable"] is False
    assert by_path["references/scenario.json"]["editable"] is False
    assert "checksums.json" not in by_path
    edited = tools.execute(document, "inspect_plugin_files", {"paths": ["README.md"]})
    assert edited["files"][0]["content"] == "Current edited description"
    matches = tools.execute(document, "search_plugin_files", {"query": "invoke_scenario_capability"})
    assert any(item["path"] == "server.py" for item in matches["matches"])


def test_mcp_actual_reads_require_listed_opaque_key_and_never_expose_locators(monkeypatch, authorized):
    cfg = SimpleNamespace(id="mcp", name="Docs", transport="streamable_http", connector_revision=2, headers={}, env={})
    db = DB(cfg)
    document = {"resource_snapshot": {"mcps": [{"id": "mcp", "connector_revision": 2}]}}
    calls = []
    monkeypatch.setattr(runtime.mcp_resource_service, "list_resources", lambda cfg, **kwargs: {
        "resources": [{"uri": "https://fixture.invalid/private/document", "name": "Guide", "description": "Reference"}], "has_more": False})
    def read(cfg, uri, **kwargs):
        assert db.rollbacks > 0
        calls.append(uri)
        return {"text": "Public programming reference", "read_only": True}
    monkeypatch.setattr(runtime.mcp_resource_service, "read_resource", read)
    listed, record = runtime.execute(db, document, "list_coding_mcp_resources", {"mcp_id": "mcp"})
    assert "https://" not in str(listed) and "uri" not in str(listed)
    key = listed["resources"][0]["resource_key"]
    with pytest.raises(ValueError, match="先查找"):
        runtime.execute(db, document, "read_coding_mcp_resource", {"mcp_id": "mcp", "resource_key": key})
    document["coding_resource_keys"] = record["resource_keys"]
    result, receipt = runtime.execute(db, document, "read_coding_mcp_resource", {"mcp_id": "mcp", "resource_key": key})
    assert result["text"] == "Public programming reference" and len(receipt["content_sha256"]) == 64
    assert receipt["title"] == runtime.MCP_TOOLS["read_coding_mcp_resource"][1]
    assert calls == ["https://fixture.invalid/private/document"]
    with pytest.raises(ValidationError):
        runtime.execute(db, document, "read_coding_mcp_resource", {"mcp_id": "mcp", "resource_key": key, "uri": "other"})
    cfg.connector_revision = 3
    with pytest.raises(ValueError, match="失效"):
        runtime.execute(db, document, "list_coding_mcp_resources", {"mcp_id": "mcp"})


@pytest.mark.parametrize("content", ["opaque-fixture-value", "credential: opaque-fixture-value", "sk-synthetic_forbidden_value"])
def test_mcp_credential_echo_is_rejected(content):
    cfg = SimpleNamespace(headers={"X-Private": "opaque-fixture-value"}, env={})
    with pytest.raises(ValueError):
        runtime._safe_mcp_result({"text": content}, cfg)


def test_read_budgets_and_receipts_survive_retries_and_reject_late_run():
    document = {"active_run_id": "run", "phase": "generating", "events": []}
    for _ in range(runtime.MAX_MCP_READS):
        runtime.reserve_operation(document, "read_coding_mcp_resource", "run")
    with pytest.raises(ValueError, match="预算"):
        runtime.reserve_operation(document, "read_coding_mcp_resource", "run")
    result = {"text": "Ephemeral reference"}
    runtime.record_result(document, name="read_coding_mcp_resource", content=result,
                          record={"title": "Read guide", "content_sha256": "a" * 64}, run_id="run")
    assert "Ephemeral reference" not in str(document)
    assert document["resource_receipts"][0]["read_only"] is True
    with pytest.raises(ValueError, match="失效"):
        runtime.record_result(document, name="read_coding_mcp_resource", content=result, record={"title": "Late"}, run_id="old")
    document["coding_tool_budget"]["result_bytes"] = runtime.MAX_TOOL_RESULT_BYTES
    with pytest.raises(ValueError, match="预算"):
        runtime.record_result(document, name="search_plugin_files", content=result, record={"title": "Search"}, run_id="run")


def test_remote_mcp_read_respects_remaining_worker_deadline(monkeypatch):
    from app.services import mcp_resource_service
    monkeypatch.setattr(mcp_resource_service, "get_settings", lambda: SimpleNamespace(mcp_operation_timeout_seconds=30))
    monkeypatch.setattr(mcp_resource_service.mcp_service, "_run", asyncio.run)
    with pytest.raises(mcp_resource_service.MCPResourceError) as timed_out:
        mcp_resource_service._run(asyncio.sleep(0.2), timeout_seconds=0.01)
    assert str(timed_out.value) == "MCP 资料读取失败；请确认服务支持只读资源协议并检查连接配置"


def test_discussion_cannot_write_files_or_claim_revalidation():
    document = {"active_run_id": "run", "phase": "generating", "round_mode": "discuss", "round_base_phase": "released",
                "revision": 3, "events": [], "files": {"README.md": "Reviewed source"}, "validation": ["Existing issue"]}
    with pytest.raises(ValueError, match="讨论"):
        apply_step(document, CodingStep(kind="file", path="README.md", content="Changed"), "run")
    finish_document(document)
    assert document["phase"] == "released" and document["validation"] == ["Existing issue"]
    assert document["files"] == {"README.md": "Reviewed source"}
    assert document["round_failed"] is False
    with pytest.raises(ValidationError):
        PluginCodingUpdate(expected_revision=3, request_id="request-id", action="stop", base_files_hash="a" * 64, instruction="write")


def test_legacy_project_discussion_refreshes_exact_release_context_without_changing_files(monkeypatch):
    document = {"manifest": {"scenario": {"id": "scenario"}, "deployment": {"release_id": "fixed-release"}},
        "revision": 5, "files": {"README.md": "Reviewed source"}, "previous": {}, "phase": "released", "events": [],
        "active_run_id": None, "required_paths": ["scripts/existing.py"], "coding_contract": {"legacy": True},
        "validation": [], "llm_config_id": "model"}
    row = SimpleNamespace(proposal=deepcopy(document))
    calls = []
    monkeypatch.setattr(workspace, "owned_root", lambda *args, **kwargs: row)
    monkeypatch.setattr(workspace, "assert_adapter_identity", lambda *args: None)
    monkeypatch.setattr(workspace, "public_workspace", lambda db, root: deepcopy(root.proposal))
    monkeypatch.setattr(resources.release_service, "_scenario_for_manage", lambda *args: None)
    monkeypatch.setattr(resources, "prepare_context", lambda *args: {})
    def contract(db, manifest):
        calls.append(manifest["deployment"]["release_id"])
        return {"scenario": {"description": "Fixed release goal"}}
    monkeypatch.setattr(workspace, "authoring_contract", contract)
    monkeypatch.setattr(workspace, "enqueue_round", lambda *args, **kwargs: None)
    result = workspace.update_workspace(DB(), "workspace", PluginCodingUpdate(expected_revision=5,
        request_id="request-id", action="discuss", base_files_hash=files_hash(document["files"]), instruction="Explain the scenario goal"))
    assert calls == ["fixed-release"]
    assert result["coding_contract"]["scenario"]["description"] == "Fixed release goal"
    assert result["files"] == document["files"] and result["required_paths"] == document["required_paths"]
    assert result["phase"] == "released"


def test_discussion_queue_and_read_progress_describe_the_current_mode(monkeypatch):
    row = SimpleNamespace(thread_id="workspace", proposal={"llm_config_id": "model", "revision": 5,
        "phase": "released", "files": {}, "events": [], "coding_failure_diagnostic": {'run_id': 'old'}})
    monkeypatch.setattr(workspace.permission_service, "require_principal",
                        lambda db: SimpleNamespace(tenant_id="tenant", user_id="user"))
    monkeypatch.setattr(workspace.jobs, "request_message_id", lambda kind, **kwargs: kind)
    monkeypatch.setattr(workspace.jobs, "enqueue_request", lambda *args, **kwargs: None)
    workspace.enqueue_round(SimpleNamespace(add_all=lambda values: None), row,
                            instruction="Explain the existing plugin", request_id="request-id", mode="discuss")
    assert row.proposal["events"][-1]["message"] == "讨论问题已保存，等待后台分析"
    assert row.proposal['coding_failure_diagnostic'] is None
    runtime.reserve_operation(row.proposal, "inspect_plugin_files", "run")
    runtime.record_result(row.proposal, name="inspect_plugin_files", content={"files": []},
                          record={"title": "读取项目文件"}, run_id="run")
    assert row.proposal["events"][-1]["message"] == "读取项目文件已完成，只读结果供本轮讨论参考"


def test_coding_failure_diagnostics_log_code_locations_and_shapes_without_values(caplog):
    shape = tool_diagnostic('inspect_plugin_files', {'paths': ['private-reference-path'], 'private-extra-field': 'private-value'})
    try:
        raise ValueError('private-provider-message')
    except ValueError as error:
        log_boundary_failure(error, shape)
    assert 'ValueError' in caplog.text and 'inspect_plugin_files' in caplog.text
    assert "'paths_count': 1" in caplog.text and "'extra_field_count': 1" in caplog.text
    assert 'private-reference-path' not in caplog.text
    assert 'private-extra-field' not in caplog.text and 'private-value' not in caplog.text
    assert 'private-provider-message' not in caplog.text


def test_coding_failure_persistence_contains_only_bounded_safe_diagnostic_fields():
    document = {'active_run_id': 'run'}
    shape = tool_diagnostic('inspect_plugin_files', {'paths': ['private-reference-path'], 'private-extra-field': 'private-value'})
    try:
        raise ValueError('private-provider-message')
    except ValueError as error:
        record_failure_diagnostic(document, error, run_id='run', diagnostic=shape)
        with pytest.raises(ValueError, match='失效'):
            record_failure_diagnostic(document, error, run_id='old', diagnostic=shape)
    saved = document['coding_failure_diagnostic']
    assert set(saved) == {'run_id', 'error_type', 'local_functions', 'tool_shape'}
    assert saved['error_type'] == 'ValueError' and saved['run_id'] == 'run'
    assert saved['tool_shape']['fields'] == ['paths'] and saved['tool_shape']['paths_count'] == 1
    assert all(private not in str(document) for private in
               ['private-reference-path', 'private-extra-field', 'private-value', 'private-provider-message'])


def test_public_discussion_source_phase_preserves_the_reviewed_source_state(monkeypatch):
    document = {'manifest': {'package_name': 'scenario-fixture', 'capabilities': []},
        'plugin_version': '1.0.0', 'revision': 2, 'phase': 'generating', 'round_mode': 'discuss',
        'round_base_phase': 'released', 'release_id': 'release', 'llm_config_id': 'model',
        'files': {'README.md': 'Reviewed source'}, 'events': [], 'validation': [],
        'active_run_id': None, 'coding_contract': {'capabilities': []}}
    monkeypatch.setattr(workspace, 'assert_adapter_identity', lambda *args: None)
    monkeypatch.setattr(workspace, 'coding_turns', lambda *args: [])
    result = workspace.public_workspace(DB(), SimpleNamespace(thread_id='workspace', proposal=document))
    assert result['phase'] == 'generating' and result['source_phase'] == 'released'


@pytest.mark.parametrize('missing_path', ['.', 'server-missing.py'])
def test_rejected_read_path_can_be_corrected_in_a_bounded_native_discussion(missing_path):
    document = {'manifest': {'package_name': 'scenario-fixture', 'capabilities': []}, 'plugin_version': '1.0.0',
        'files': {}, 'events': [], 'active_run_id': 'run', 'phase': 'generating', 'round_mode': 'discuss'}
    conversations = []
    functions = [
        {'name': 'inspect_plugin_files', 'arguments': {'paths': [missing_path]}},
        {'name': 'inspect_plugin_files', 'arguments': {}},
        {'name': 'inspect_plugin_files', 'arguments': {'paths': ['server.py']}},
        {'name': 'submit_plugin_step', 'arguments': {'kind': 'summary', 'message': 'Reviewed the trusted adapter'}},
    ]
    def stream(messages):
        conversations.append(deepcopy(messages))
        yield {'type': 'tool_calls', 'tool_calls': [{'id': str(len(conversations)), 'function': functions[len(conversations) - 1]}]}
    def execute_tool(name, arguments):
        runtime.reserve_operation(document, name, 'run')
        content, record = runtime.execute(DB(), document, name, arguments)
        return runtime.record_result(document, name=name, content=content, record=record, run_id='run')
    steps = list(native_coding_steps([], stream=stream, parse=parse_tool_steps, execute_tool=execute_tool, mode='discuss'))
    assert [step.kind for step in steps] == ['summary']
    rejection = conversations[1][-1]['content']
    assert '"ok": false' in rejection and 'unknown_project_path' in rejection
    assert '"path": "."' not in rejection
    if missing_path != '.':
        assert missing_path not in rejection
    assert '"path": "server.py"' in conversations[2][-1]['content']
    assert 'invoke_scenario_capability' in conversations[3][-1]['content']
    assert document['coding_tool_budget']['operations'] == 3
    assert len(document['resource_receipts']) == 2
    assert [event['kind'] for event in document['events']] == ['tool_rejected', 'tool', 'tool']


@pytest.mark.parametrize('arguments, code', [
    ({'paths': ['server.py'] * 6}, 'invalid_tool_arguments'),
    ({'paths': ['server.py', 'server.py']}, 'duplicate_project_paths'),
    ({'paths': [], 'private-extra': 'private-value'}, 'invalid_tool_arguments'),
])
def test_recoverable_base_tool_rejections_preserve_schema_and_record_no_read_receipt(arguments, code):
    document = {'manifest': {'package_name': 'scenario-fixture', 'capabilities': []}, 'plugin_version': '1.0.0',
                'files': {}, 'events': [], 'active_run_id': 'run', 'phase': 'generating'}
    runtime.reserve_operation(document, 'inspect_plugin_files', 'run')
    content, record = runtime.execute(DB(), document, 'inspect_plugin_files', arguments)
    runtime.record_result(document, name='inspect_plugin_files', content=content, record=record, run_id='run')
    assert content['error']['code'] == code and content['operation_performed'] is False
    assert document.get('resource_receipts', []) == []
    assert document['events'][-1]['kind'] == 'tool_rejected'
    assert 'inspect_plugin_files' not in document['events'][-1]['message'] and 'paths' not in document['events'][-1]['message']
    assert document['coding_tool_budget']['operations'] == 1
    assert 'private-extra' not in str(document) and 'private-value' not in str(document)
    with pytest.raises(ValueError):
        runtime.reserve_operation(document, 'unknown_tool', 'run')


def test_native_overlong_summary_is_rejected_before_persistence_and_can_be_corrected():
    conversations = []
    def stream(messages):
        conversations.append(deepcopy(messages))
        message = 's' * 1001 if len(conversations) == 1 else 'A concise validated answer'
        yield {'type': 'tool_calls', 'tool_calls': [{'id': str(len(conversations)), 'function': {
            'name': 'submit_plugin_step', 'arguments': {'kind': 'summary', 'message': message}}}]}
    steps = list(native_coding_steps([], stream=stream, parse=parse_tool_steps, mode='discuss'))
    assert [step.message for step in steps] == ['A concise validated answer']
    reply = conversations[1][-1]['content']
    assert '"ok": false' in reply and 'invalid_coding_step' in reply
    assert '"persisted": false' in reply and 's' * 1001 not in reply


def test_repeated_invalid_native_submissions_exhaust_the_existing_turn_budget():
    calls = []
    def stream(messages):
        calls.append(None)
        yield {'type': 'tool_calls', 'tool_calls': [{'id': str(len(calls)), 'function': {
            'name': 'submit_plugin_step', 'arguments': {'kind': 'summary', 'message': 's' * 1001}}}]}
    with pytest.raises(ValueError, match='轮数预算'):
        list(native_coding_steps([], stream=stream, parse=parse_tool_steps, mode='discuss'))
    assert len(calls) == 8


def test_native_discussion_accepts_final_public_text_after_a_plan_without_changing_source():
    conversations = []
    def stream(messages):
        conversations.append(deepcopy(messages))
        if len(conversations) == 1:
            yield {'type': 'tool_calls', 'tool_calls': [{'id': 'plan', 'function': {
                'name': 'submit_plugin_step', 'arguments': {'kind': 'plan', 'message': 'Read the saved project'}}}]}
        else:
            yield {'type': 'reasoning', 'content': 'private separate channel'}
            yield {'type': 'token', 'content': '<thi'}
            yield {'type': 'token', 'content': 'nk>private thought</think>\n' + 'Public explanation ' * 100}
    steps = list(native_coding_steps([], stream=stream, parse=parse_tool_steps, mode='discuss'))
    assert steps[0].kind == 'plan'
    answer = ''.join(step.message for step in steps if step.kind == 'summary')
    assert answer == ('Public explanation ' * 100).strip()
    assert all(len(step.message) <= 1000 for step in steps)
    assert 'private' not in str(steps)


def test_native_thought_only_discussion_remains_unanswered():
    def generate(document, instruction):
        yield from native_coding_steps([], stream=lambda _: iter([{'type': 'token', 'content': '<think>private thought</think>'}]),
                                       parse=parse_tool_steps, mode='discuss')
    with pytest.raises(ValueError, match='没有提供讨论答复'):
        discuss_project({}, 'Question', generate=generate, checkpoint=lambda step=None, **kwargs: {})


def test_native_generate_plain_text_and_tool_preamble_are_not_final_discussion_answers():
    plaintext = lambda _: iter([{'type': 'token', 'content': 'Public answer'}])
    assert list(native_coding_steps([], stream=plaintext, parse=parse_tool_steps, mode='generate')) == []
    calls = []
    def preamble(messages):
        calls.append(None)
        if len(calls) == 1:
            yield {'type': 'token', 'content': 'Not a final answer'}
            yield {'type': 'tool_calls', 'tool_calls': [{'id': 'plan', 'function': {
                'name': 'submit_plugin_step', 'arguments': {'kind': 'plan', 'message': 'Inspect first'}}}]}
    steps = list(native_coding_steps([], stream=preamble, parse=parse_tool_steps, mode='discuss'))
    assert [step.kind for step in steps] == ['plan']


def test_public_discussion_keeps_output_budgets_and_credential_checkpoint():
    too_long = lambda _: iter([{'type': 'token', 'content': 'x' * 20_001}])
    with pytest.raises(ValueError, match='分段预算'):
        list(native_coding_steps([], stream=too_long, parse=parse_tool_steps, mode='discuss'))
    thought = lambda _: iter([{'type': 'token', 'content': '<think>' + 'x' * (160 * 1024)}])
    with pytest.raises(ValueError, match='输出预算'):
        list(native_coding_steps([], stream=thought, parse=parse_tool_steps, mode='discuss'))
    unsafe = lambda _: iter([{'type': 'token', 'content': 'sk-synthetic_forbidden_value'}])
    step = next(native_coding_steps([], stream=unsafe, parse=parse_tool_steps, mode='discuss'))
    with pytest.raises(ValueError):
        apply_step({'active_run_id': 'run', 'phase': 'generating', 'round_mode': 'discuss'}, step, 'run')
