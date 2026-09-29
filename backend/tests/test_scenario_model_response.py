from types import SimpleNamespace

import pytest

from app.services import llm_service, scenario_model_response_service as response_service


def _stream(monkeypatch, chunks, *, fail_close=False):
    closed = []
    class Stream:
        def __iter__(self):
            yield from chunks
        def close(self):
            closed.append('stream')
            if fail_close:
                raise OSError('synthetic transport close failure')
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: Stream())),
                             close=lambda: closed.append('client'))
    monkeypatch.setattr(llm_service, '_client', lambda *args, **kwargs: client)
    monkeypatch.setattr(llm_service, '_ensure_callable', lambda *args, **kwargs: None)
    monkeypatch.setattr(llm_service, '_assert_budget_available', lambda *args, **kwargs: None)
    traces = []
    monkeypatch.setattr(llm_service, '_record_trace', lambda *args, **kwargs: traces.append(kwargs))
    return closed, traces


def _chunk(content='', reason=None):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=reason,
        delta=SimpleNamespace(content=content, tool_calls=None))])


def _call():
    return response_service.chat(SimpleNamespace(model='synthetic', max_tokens=100, temperature=0), [], temperature=0,
        max_tokens=100, request_timeout=30, max_retries=0, db=None, before_provider_call=None)


def test_complete_compiler_stream_preserves_truncation_and_closes_connections(monkeypatch):
    closed, traces = _stream(monkeypatch, [_chunk('{'), _chunk('"entities":[]}', 'length')])
    response = _call()
    assert response['content'] == '{"entities":[]}'
    assert response['raw']['choices'][0]['finish_reason'] == 'length'
    assert closed == ['stream', 'client']
    assert traces[0]['status'] == 'succeeded'


def test_disconnected_stream_is_not_accepted_as_a_complete_definition(monkeypatch):
    closed, traces = _stream(monkeypatch, [_chunk('{"entities":[]}')])
    with pytest.raises(llm_service.LLMRuntimeError): _call()
    assert closed == ['stream', 'client']
    assert traces[0]['status'] == 'failed'


def test_compiler_stream_enforces_output_bound_and_total_timeout(monkeypatch):
    closed, traces = _stream(monkeypatch, [_chunk('x' * 1201, 'stop')])
    with pytest.raises(llm_service.LLMRuntimeError): _call()
    assert closed == ['stream', 'client']
    assert traces[0]['status'] == 'failed'
    closed, traces = _stream(monkeypatch, [_chunk('{}', 'stop')])
    clock = iter([0, 31])
    monkeypatch.setattr(llm_service.time, 'perf_counter', lambda: next(clock))
    with pytest.raises(TimeoutError): _call()
    assert closed == ['stream', 'client']
    assert traces[0]['status'] == 'failed'


def test_cancelling_stream_closes_transport_and_records_cancellation(monkeypatch):
    closed, traces = _stream(monkeypatch, [_chunk('part'), _chunk('rest', 'stop')])
    stream = llm_service.chat_stream(SimpleNamespace(model='synthetic', max_tokens=100, temperature=0), [])
    assert next(stream)['content'] == 'part'
    stream.close()
    assert closed == ['stream', 'client']
    assert traces[0]['status'] == 'cancelled'


def test_stream_cleanup_failure_still_closes_client_and_records_original_failure(monkeypatch):
    closed, traces = _stream(monkeypatch, [_chunk('partial')], fail_close=True)
    with pytest.raises(llm_service.LLMRuntimeError):
        _call()
    assert closed == ['stream', 'client']
    assert traces[0]['status'] == 'failed'
    assert isinstance(traces[0]['error'], llm_service.LLMRuntimeError)


def test_reasoning_json_is_not_combined_with_the_final_definition():
    text = '<think>Example {"discard":true}</think>\n{"workflows":[]}'
    assert response_service.extract_model_output(text) == {'workflows': []}
    assert response_service.extract_model_output('<THINK>draft</THINK>\n```json\n{"entities":[]}\n```') == {'entities': []}


def test_json_string_contents_are_preserved_verbatim():
    import json
    value = {'prompt': 'Literal <think>{example}</think> and comma ,} and ```json'}
    assert response_service.extract_model_output(json.dumps(value)) == value


@pytest.mark.parametrize('content', [
    '<think>{"entities":[]}', '{} {}', '{"a":1,"a":2}', '{"value":NaN}',
    '{"value":1e999}', '[]', '```json\n{}', '{"a":1,}',
])
def test_ambiguous_incomplete_or_noncanonical_model_output_is_rejected(content):
    with pytest.raises(ValueError):
        response_service.extract_model_output(content)


def test_failure_log_preserves_code_location_without_exception_content(caplog):
    import logging
    logger = logging.getLogger('synthetic.compiler')
    try:
        raise ValueError('synthetic-private-input-do-not-log')
    except ValueError as error:
        response_service.log_failure(logger, error, job_id='synthetic-job')
    assert 'ValueError' in caplog.text and 'synthetic-job' in caplog.text
    assert 'test_failure_log' in caplog.text
    assert 'synthetic-private-input-do-not-log' not in caplog.text
