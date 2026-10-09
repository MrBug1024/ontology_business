import json
from types import SimpleNamespace

import pytest

from app.services import agent_runtime_adapter as adapter


def runtime(monkeypatch, **changes):
    client = adapter.CapabilityAgentRuntime.__new__(adapter.CapabilityAgentRuntime)
    row = SimpleNamespace(tenant_id='tenant', requested_by_user_id='user', agent_id='agent',
        scenario_id='scenario', capability_kind='workflow', capability_key='flow')
    for key, value in changes.items():
        setattr(row, key, value)
    client.agent = SimpleNamespace(id='agent')
    client.scenario = SimpleNamespace(id='scenario')
    client.db = SimpleNamespace(get=lambda *args: row, rollback=lambda: None)
    client.capabilities = [{'kind': 'workflow', 'key': 'flow'}]
    client._capability_by_ref = {('workflow', 'flow'): client.capabilities[0]}
    client.turn_input = SimpleNamespace(attachments=[])
    client.runtime_connection_options = []
    monkeypatch.setattr(client, '_actor', lambda: SimpleNamespace(tenant_id='tenant', user_id='user'))
    monkeypatch.setattr(client, '_model_receipt', lambda value: value)
    calls = []
    def read(*args):
        calls.append(args)
        return {'invocation_id': 'i' * 32, 'status': 'succeeded',
            'capability': {'kind': 'workflow', 'key': 'flow'}, 'output': {'route': 'standard'}}
    monkeypatch.setattr(adapter.capability_application_service, 'get_receipt', read)
    return client, calls


def test_agent_exposes_read_only_receipt_tool(monkeypatch):
    client, _ = runtime(monkeypatch)
    tools = {item['function']['name']: item['function'] for item in client.build_tools()}
    schema = tools['get_capability_receipt']['parameters']
    assert schema['required'] == ['invocation_id']
    assert schema['additionalProperties'] is False


def test_agent_reads_actual_terminal_receipt_through_shared_service(monkeypatch):
    client, calls = runtime(monkeypatch)
    result = json.loads(client.execute_tool('get_capability_receipt', {'invocation_id': 'i' * 32}))
    assert result['status'] == 'succeeded'
    assert result['output']['route'] == 'standard'
    assert len(calls) == 1


@pytest.mark.parametrize('changes', [
    {'tenant_id': 'other'}, {'requested_by_user_id': 'other'}, {'agent_id': 'other'},
    {'scenario_id': 'other'}, {'capability_key': 'revoked'},
])
def test_agent_receipt_cannot_expand_current_scope(monkeypatch, changes):
    client, calls = runtime(monkeypatch, **changes)
    result = json.loads(client.execute_tool('get_capability_receipt', {'invocation_id': 'i' * 32}))
    assert result['error']['code'] == 'INVOCATION_NOT_FOUND'
    assert calls == []


def test_agent_receipt_rejects_unknown_fields(monkeypatch):
    client, calls = runtime(monkeypatch)
    result = json.loads(client.execute_tool('get_capability_receipt', {'invocation_id': 'i' * 32, 'tenant_id': 'other'}))
    assert result['error']['code'] == 'INVALID_RECEIPT_REQUEST'
    assert calls == []


def test_agent_receipt_shared_reader_rechecks_acl(monkeypatch):
    client, _ = runtime(monkeypatch)
    def denied(*args):
        raise adapter.capability_application_service.CapabilityApplicationError(
            'invocation_not_found', 'unavailable', status_code=404)
    monkeypatch.setattr(adapter.capability_application_service, 'get_receipt', denied)
    result = json.loads(client.execute_tool('get_capability_receipt', {'invocation_id': 'i' * 32}))
    assert result['error']['code'] == 'INVOCATION_NOT_FOUND'


def test_agent_receipt_history_refreshes_only_authorized_current_result(monkeypatch):
    client, calls = runtime(monkeypatch)
    previous = {'invocation_id': 'i' * 32, 'status': 'running',
        'capability': {'kind': 'workflow', 'key': 'flow'}}
    current = json.loads(client.model_historic_tool_result('get_capability_receipt', {}, previous))
    assert current['status'] == 'succeeded'
    assert len(calls) == 1
    client._capability_by_ref = {}
    assert client.model_historic_tool_result('get_capability_receipt', {}, previous) is None
    assert not client.authorize_historic_tool_result('get_capability_receipt', {}, previous)
    assert len(calls) == 1


@pytest.mark.parametrize('name, count', [('get_capability_receipt', 2), ('invoke_capability', 1)])
def test_receipt_polling_can_repeat_while_business_calls_stay_guarded(monkeypatch, name, count):
    client, _ = runtime(monkeypatch)
    client.agent = SimpleNamespace(temperature=0.2, max_tokens=100)
    client.llm = None
    client._evidence_refs = []
    monkeypatch.setattr(client, '_system_prompt', lambda: 'Check actual state')
    monkeypatch.setattr(client, '_model_user_message', lambda text: text)
    monkeypatch.setattr(client, 'build_tools', lambda: [])
    monkeypatch.setattr(adapter, 'get_settings', lambda: SimpleNamespace(max_tool_rounds=3))
    reads = []
    monkeypatch.setattr(client, 'execute_tool', lambda *args: reads.append(args) or '{}')
    rounds = []
    def stream(*args, **kwargs):
        rounds.append(1)
        if len(rounds) < 3:
            yield {'type':'tool_calls','tool_calls':[{'id':str(len(rounds)),
                'function':{'name':name,'arguments':{'invocation_id':'i' * 32}}}]}
        else:
            yield {'type':'token','content':'Done.'}
    monkeypatch.setattr(adapter.llm_service, 'chat_stream', stream)
    list(client.run_agent([], 'Synthetic poll'))
    assert len(reads) == count
