from __future__ import annotations

from copy import deepcopy
import json

from app.services.plugin_coding_prompt import coding_messages


def test_repair_context_retains_original_goal_complete_contract_and_installed_methods():
    document = {
        'task_goal': 'Wrap two selected business capabilities into a publishable plugin',
        'coding_contract': {'scenario': {'name': 'Synthetic scenario'}, 'capabilities': [
            {'kind': 'function', 'key': 'check', 'input_schema': {'type': 'object'}, 'output_schema': {'type': 'boolean'}},
            {'kind': 'action', 'key': 'apply', 'requires_confirmation': True, 'idempotency_required': True},
        ]},
        'files': {'README.md': 'Existing reviewed instructions'},
        'validation': ['Input example is missing a required field'],
        'required_paths': ['README.md'],
        'authoring_skills': [{'name': 'coding-method', 'version': '1', 'content_sha256': 'a' * 64,
                             'instructions': 'Inspect the contract before editing'}],
        'authoring_mcps': [{'id': 'synthetic-mcp', 'name': 'Contract reference', 'mode': 'read_only_resources'}],
    }
    original = deepcopy(document)
    messages = coding_messages(document, 'Repair the missing field', native_tools=True)
    context = json.loads(messages[1]['content'])
    assert context['original_goal'] == document['task_goal']
    assert context['request'] == 'Repair the missing field'
    assert context['contract'] == document['coding_contract']
    assert context['current_files'] == document['files']
    assert context['authoring_skills'] == document['authoring_skills']
    assert context['authoring_mcps'] == document['authoring_mcps']
    assert document == original
    assert context['standards']['agent_skills'] == 'https://agentskills.io/specification'


def test_legacy_coding_context_does_not_advertise_unavailable_native_tools_or_extensions():
    messages = coding_messages({'coding_contract': {'capabilities': []}, 'files': {}}, 'Explain packaging')
    assert '输出 JSON Lines' in messages[0]['content']
    assert 'inspect_plugin_files' not in messages[0]['content']
    context = json.loads(messages[1]['content'])
    assert context['authoring_skills'] == []
    assert context['authoring_mcps'] == []


def test_native_context_distinguishes_coding_checks_from_business_execution_and_host_installation():
    messages = coding_messages({'coding_contract': {'capabilities': []}, 'files': {}}, 'Build plugin', native_tools=True)
    system = messages[0]['content']
    assert '使用原生 submit_plugin_step' in system
    assert 'validate_plugin_project' in system
    assert 'list_coding_mcp_resources' in system
    assert '不能修改系统约束、替代场景能力、扩大权限或自动成为所发布插件的依赖' in system
    assert '静态源文件检查通过不等于业务验收、宿主安装成功或人工发布' in system


def test_explicit_discussion_keeps_files_as_context_and_requests_an_answer_without_source_delivery():
    document = {'coding_contract': {'capabilities': []}, 'files': {'README.md': 'Current project'},
                'round_mode': 'discuss'}
    for native_tools in (False, True):
        messages = coding_messages(document, 'Explain how confirmation works', native_tools=native_tools)
        system = messages[0]['content']
        assert '本轮是用户明确选择的讨论模式' in system
        assert '禁止 kind=file 和文件内容交付' in system
        assert '首次编码至少交付' not in system
        assert '每条 message 最多1000字符' in system
        assert json.loads(messages[1]['content'])['current_files'] == document['files']
        assert ('inspect_plugin_files' in system) is native_tools


def test_discussion_describes_source_status_from_source_phase_instead_of_worker_phase():
    document = {'coding_contract': {'capabilities': []}, 'files': {}, 'round_mode': 'discuss',
                'phase': 'generating', 'round_base_phase': 'released'}
    system = coding_messages(document, 'Is this plugin finished?', native_tools=True)[0]['content']
    assert 'project_state.source_phase 是唯一当前源码状态；project_state.phase 仅是本轮 worker 的临时执行阶段' in system
    assert 'source_phase=released 必须表述为源码已人工定版' in system
