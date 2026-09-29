from types import SimpleNamespace

import pytest

from app.services import agent_runtime_adapter as adapter


def runtime_client(monkeypatch):
    client = adapter.CapabilityAgentRuntime.__new__(adapter.CapabilityAgentRuntime)
    client.agent = SimpleNamespace(temperature=0.2, max_tokens=100)
    client.llm = None
    client.db = None
    client._evidence_refs = []
    monkeypatch.setattr(client, '_system_prompt', lambda: 'Public answer only')
    monkeypatch.setattr(client, '_model_user_message', lambda value: value)
    monkeypatch.setattr(client, 'build_tools', lambda: [])
    monkeypatch.setattr(adapter, 'get_settings', lambda: SimpleNamespace(max_tool_rounds=1))
    return client


def test_agent_preserves_model_thinking_for_collapsible_display(monkeypatch):
    client = runtime_client(monkeypatch)
    monkeypatch.setattr(adapter.llm_service, 'chat_stream', lambda *args, **kwargs: iter([
        {'type': 'token', 'content': item} for item in ['<thi', 'nk>private analysis</th', 'ink>', 'Final answer.']]))
    events = list(client.run_agent([], 'Synthetic request'))
    assert ''.join(item['data'] for item in events if item['type'] == 'token') == '<think>private analysis</think>Final answer.'
    assert next(item['data'] for item in events if item['type'] == 'done') == '<think>private analysis</think>Final answer.'


@pytest.mark.parametrize('content, expected', [
    ('  Ordinary answer with <think> as a literal.', '  Ordinary answer with <think> as a literal.'),
    ('<THINK>private</ThInK>\nAnswer.', 'Answer.'),
    ('<think>one</think>\n<think>two</think>Answer.', 'Answer.'),
    ('<think>never closed', ''),
    ('<thi', ''),
    ('< 10', '< 10'),
    ('```json\n{"result": 1}\n```', '```json\n{"result": 1}\n```'),
])
def test_public_response_handles_every_token_boundary(content, expected):
    from app.services.llm_public_response import PublicResponseText
    for split in range(len(content) + 1):
        stream = PublicResponseText()
        result = stream.feed(content[:split]) + stream.feed(content[split:]) + stream.finish()
        assert result == expected
    stream = PublicResponseText()
    assert ''.join(stream.feed(char) for char in content) + stream.finish() == expected


def test_large_reasoning_body_has_bounded_pending_state():
    from app.services.llm_public_response import PublicResponseText
    stream = PublicResponseText()
    assert stream.feed('<think>') == ''
    for _ in range(100):
        assert stream.feed('private ' * 1000) == ''
        assert len(stream._pending) <= 7
    assert stream.feed('</think>Answer.') == 'Answer.'


def test_legacy_backslash_reasoning_close_is_filtered():
    from app.services.llm_public_response import PublicResponseText
    stream = PublicResponseText()
    assert stream.feed('<think>private<' + chr(92) + 'think>Answer.') == 'Answer.'


def test_pathological_prefix_is_rejected_without_echo():
    from app.services.llm_public_response import PublicResponseText
    stream = PublicResponseText()
    with pytest.raises(ValueError):
        stream.feed(' ' * 5000)


@pytest.mark.parametrize('content', ['<think>unfinished', '<think>only thought</think>', '   ', '<thi'])
def test_reasoning_without_answer_is_not_a_successful_completion(monkeypatch, content):
    client = runtime_client(monkeypatch)
    monkeypatch.setattr(adapter.llm_service, 'chat_stream', lambda *args, **kwargs: iter([
        {'type': 'token', 'content': content}]))
    events = []
    with pytest.raises(adapter.AgentRuntimeAdapterError, match='模型未返回可显示的回答'):
        events.extend(client.run_agent([], 'Synthetic request'))
    assert not any(event['type'] == 'done' for event in events)


def test_tool_round_keeps_display_content_but_replays_only_answer(monkeypatch):
    client = runtime_client(monkeypatch)
    monkeypatch.setattr(adapter, 'get_settings', lambda: SimpleNamespace(max_tool_rounds=2))
    monkeypatch.setattr(client, 'execute_tool', lambda *args: {'ok': True})
    calls = []
    def stream(_llm, messages, **kwargs):
        calls.append([dict(message) for message in messages])
        if len(calls) == 1:
            yield {'type': 'token', 'content': '<think>checking</think>Looking up.'}
            yield {'type': 'tool_calls', 'tool_calls': [
                {'id': 'synthetic-call', 'function': {'name': 'synthetic_tool', 'arguments': {}}}]}
        else:
            yield {'type': 'token', 'content': '<think>reviewing</think>Done.'}
    monkeypatch.setattr(adapter.llm_service, 'chat_stream', stream)
    events = list(client.run_agent([], 'Synthetic request'))
    assert calls[1][-2]['content'] == 'Looking up.'
    assert next(event['data'] for event in events if event['type'] == 'token') == '<think>checking</think>Looking up.'
    assert events[-1] == {'type': 'done', 'data': '<think>reviewing</think>Done.'}
