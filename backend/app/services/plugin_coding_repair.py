"""A bounded write/check/repair loop over persistent candidate source."""
from __future__ import annotations

from pydantic import ValidationError
from .plugin_project_validation import validate_project

MAX_CODING_ATTEMPTS = 3


def discuss_project(document, instruction, *, generate, checkpoint):
    answered = False
    for step in generate(document, instruction):
        if step.kind == 'file':
            raise ValueError('讨论模式不能修改文件，请切换到编码模式')
        answered = answered or (step.kind == 'summary' and bool(step.message.strip()))
        document = checkpoint(step)
    if not answered:
        raise ValueError('本轮没有提供讨论答复，请显式重试')
    # Discussion reads the saved project; its existing validation and delivery
    # state must not be promoted simply because a model answered a question.
    checkpoint(finish=True)


def code_and_repair(document, instruction, *, generate, checkpoint, step_factory):
    if document.get('round_mode') == 'discuss':
        discuss_project(document, instruction, generate=generate, checkpoint=checkpoint)
        return
    for attempt in range(MAX_CODING_ATTEMPTS):
        files_written = 0
        protocol_error = None
        try:
            for step in generate(document, instruction):
                document = checkpoint(step)
                files_written += int(step.kind == 'file')
        except ValidationError as exc:
            protocol_error = exc
        if not files_written and protocol_error is None:
            raise ValueError('模型没有交付源文件，不能将模板标记为编码完成')
        issues = validate_project(document)
        if protocol_error is not None:
            issues.insert(0, '模型输出不符合 JSON Lines；只允许 plan/file/summary 和 kind/message/path/content，换行须使用 JSON 转义。')
        if not issues:
            checkpoint(finish=True)
            return
        if attempt + 1 == MAX_CODING_ATTEMPTS:
            checkpoint(finish=True, **({'error': protocol_error} if protocol_error is not None else {}))
            return
        document = checkpoint(step_factory(kind='plan', message=f'服务端校验发现 {len(issues)} 项缺口，开始第 {attempt + 1} 次自动修复。'))
        document['validation'] = issues
        instruction = '继续完成原始需求，仅修复以下服务端校验失败，保留已正确文件：\n' + '\n'.join(issues[:20])
