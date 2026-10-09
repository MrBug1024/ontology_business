"""Native coding tool turns with persisted acknowledgements and bounded continuation."""
from __future__ import annotations

import json
from pydantic import ValidationError
from .llm_public_response import PublicResponseText

MAX_NATIVE_TURNS = 8
MAX_NATIVE_CALLS = 40
MAX_NATIVE_OUTPUT_BYTES = 160 * 1024
MAX_PUBLIC_SUMMARY_STEPS = 20


def public_answer_calls(answer: str, *, first_index: int) -> list[dict]:
    if len(answer) > 1000 * MAX_PUBLIC_SUMMARY_STEPS:
        raise ValueError('模型讨论回答超过分段预算')
    return [{'id': f'public-answer-{first_index + index}', 'function': {
                'name': 'submit_plugin_step', 'arguments': {'kind': 'summary', 'message': answer[offset:offset + 1000]}}}
            for index, offset in enumerate(range(0, len(answer), 1000))]


def native_coding_steps(messages, *, stream, parse, execute_tool=None, mode='generate'):
    conversation = list(messages)
    call_count = 0
    argument_bytes = 0
    for _ in range(MAX_NATIVE_TURNS):
        calls = []
        public_text = PublicResponseText()
        public_parts = []
        for chunk in stream(conversation):
            if chunk.get('type') == 'tool_calls':
                calls.extend(chunk.get('tool_calls', []))
            elif mode == 'discuss' and chunk.get('type') == 'token':
                part = str(chunk.get('content') or '')
                argument_bytes += len(part.encode('utf-8'))
                if argument_bytes > MAX_NATIVE_OUTPUT_BYTES:
                    raise ValueError('模型讨论回答超过输出预算')
                public_parts.append(public_text.feed(part))
        if not calls:
            if mode == 'discuss':
                answer = (''.join(public_parts) + public_text.finish()).strip()
                if answer:
                    answer_calls = public_answer_calls(answer, first_index=call_count)
                    if call_count + len(answer_calls) > MAX_NATIVE_CALLS:
                        raise ValueError('模型原生编程工具调用超过预算')
                    yield from parse([{'type': 'tool_calls', 'tool_calls': answer_calls}])
            return
        replies = []
        finished = False
        for call in calls:
            call_count += 1
            argument_bytes += len(json.dumps(call['function'].get('arguments', {}), ensure_ascii=False).encode('utf-8'))
            if call_count > MAX_NATIVE_CALLS or argument_bytes > MAX_NATIVE_OUTPUT_BYTES:
                raise ValueError('模型原生编程工具调用超过预算')
            if call['function']['name'] == 'submit_plugin_step':
                try:
                    steps = list(parse([{'type': 'tool_calls', 'tool_calls': [call]}]))
                except ValidationError:
                    # Schema failures are correctable model arguments. Never
                    # echo Pydantic details, which include the rejected input.
                    replies.append({'ok': False, 'persisted': False, 'error': {
                        'code': 'invalid_coding_step',
                        'message': '提交未保存。严格按 CodingStep Schema 修正参数；kind 仅 plan/file/summary，message 最多1000字符，path 最多160字符，content 最多32768字符，禁止额外字段。长回答拆成多条 summary；讨论模式禁止 file。'}})
                    continue
                if len(steps) != 1:
                    raise ValueError('编码提交步骤不完整')
                step = steps[0]
                # The consumer persists each step before resuming this generator.
                yield step
                replies.append({'persisted': True, 'kind': step.kind, 'path': step.path,
                    'next': 'Continue reading and discussing, then submit an answer summary. Do not submit files or change source.'
                    if mode == 'discuss' else 'Continue required files, then submit a summary. Code has not been executed.'})
                finished = finished or step.kind == 'summary'
            elif execute_tool is not None:
                replies.append(execute_tool(call['function']['name'], call['function'].get('arguments', {})))
            else:
                raise ValueError('模型请求了不可用的编码工具')
        if finished:
            return
        conversation.append({'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': call['id'], 'type': 'function', 'function': {
                'name': call['function']['name'], 'arguments': json.dumps(call['function']['arguments'], ensure_ascii=False)}}
            for call in calls]})
        for call, reply in zip(calls, replies, strict=True):
            conversation.append({'role': 'tool', 'tool_call_id': call['id'],
                                 'content': json.dumps(reply, ensure_ascii=False, allow_nan=False)})
    raise ValueError('模型原生编程对话超过轮数预算')
