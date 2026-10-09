"""Stream public coding steps into the workspace under a durable lease."""
from __future__ import annotations

from copy import deepcopy
import time
from contextlib import closing
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing import Literal
import json
import logging
from pathlib import Path

from ..database import SessionLocal
from ..models import AssistantMessage, AssistantRequestRun, LLMConfig
from . import assistant_request_run_service as jobs, llm_service, release_service, tenant_service
from .plugin_coding_validation import safe_text, validate_files
from .plugin_source_policy import MAX_PROJECT_FILES, editable_path
from .plugin_coding_repair import code_and_repair
from .plugin_project_validation import validate_project
from .plugin_native_coding import native_coding_steps
from .plugin_coding_identity import assert_adapter_identity
from .plugin_coding_prompt import coding_messages
from .llm_public_response import PublicResponseText
from .plugin_coding_workspace import append_event, owned_root
from . import plugin_coding_resources as resources, plugin_coding_resource_runtime as resource_runtime


def tool_diagnostic(name: str, arguments) -> dict:
    allowed_names = set(resource_runtime.plugin_coding_tools.BASE_TOOLS) | set(resource_runtime.MCP_TOOLS)
    allowed_fields = {'paths', 'query', 'mcp_id', 'resource_key'}
    payload = arguments if isinstance(arguments, dict) else {}
    return {'tool': name if name in allowed_names else 'unsupported',
            'fields': sorted(allowed_fields.intersection(payload)), 'extra_field_count': len(set(payload) - allowed_fields),
            'paths_count': len(payload['paths']) if isinstance(payload.get('paths'), list) else None}


def failure_diagnostic(error: Exception, diagnostic: dict | None = None) -> dict:
    service_root = Path(__file__).resolve().parent
    frames = []
    cursor = error.__traceback__
    while cursor is not None:
        code = cursor.tb_frame.f_code
        if Path(code.co_filename).resolve().is_relative_to(service_root):
            frames.append(code.co_name)
        cursor = cursor.tb_next
    shape = diagnostic or {}
    allowed_names = set(resource_runtime.plugin_coding_tools.BASE_TOOLS) | set(resource_runtime.MCP_TOOLS)
    allowed_fields = {'paths', 'query', 'mcp_id', 'resource_key'}
    safe_shape = {'tool': shape.get('tool') if shape.get('tool') in allowed_names else 'unsupported',
                  'fields': sorted(allowed_fields.intersection(shape.get('fields', [])))}
    for name in ('extra_field_count', 'paths_count'):
        value = shape.get(name)
        safe_shape[name] = min(1000, max(0, value)) if isinstance(value, int) else None
    return {'error_type': type(error).__name__[:64], 'local_functions': [name[:80] for name in frames[-12:]],
            'tool_shape': safe_shape}


def record_failure_diagnostic(document: dict, error: Exception, *, run_id: str, diagnostic: dict | None = None) -> None:
    if document.get('active_run_id') != run_id:
        raise ValueError('旧编码轮次已失效')
    document['coding_failure_diagnostic'] = {'run_id': run_id, **failure_diagnostic(error, diagnostic)}


def log_boundary_failure(error: Exception, diagnostic: dict | None = None) -> None:
    safe = failure_diagnostic(error, diagnostic)
    # Only trusted code names and whitelisted shape metadata are logged. Error
    # messages, argument values, traceback locals and supplier text stay private.
    logging.getLogger(__name__).warning('Plugin coding boundary failure type=%s functions=%s tool_shape=%s',
        safe['error_type'], ' -> '.join(safe['local_functions']) or 'external_boundary', safe['tool_shape'])


class CodingStep(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['plan', 'file', 'summary']
    message: str = Field(default='', max_length=1000)
    path: str = Field(default='', max_length=160)
    content: str = Field(default='', max_length=32768)


def coding_tools(document: dict | None = None) -> list[dict]:
    result = [{'type': 'function', 'function': {'name': 'submit_plugin_step',
        'description': 'Persist a public plan, a complete candidate source file, or a completion summary. Source is validated and never executed on the platform.',
        'parameters': CodingStep.model_json_schema()}}]
    return result + resource_runtime.definitions(document or {})


def parse_tool_steps(chunks):
    count = 0
    total = 0
    for chunk in chunks:
        if chunk.get('type') != 'tool_calls':
            continue
        for call in chunk.get('tool_calls', []):
            function = call.get('function', {})
            if function.get('name') != 'submit_plugin_step':
                raise ValueError('模型请求了不可用的编码工具')
            count += 1
            if count > 20:
                raise ValueError('模型编码步骤超过预算')
            args = function.get('arguments', {})
            total += len(json.dumps(args, ensure_ascii=False).encode('utf-8'))
            if total > 160 * 1024:
                raise ValueError('模型输出超过编码预算')
            yield CodingStep.model_validate(args)


def parse_steps(chunks):
    buffer = ''
    total = 0
    count = 0
    public_text = PublicResponseText()
    for chunk in chunks:
        if chunk.get('type') != 'token':
            continue
        part = str(chunk.get('content') or '')
        total += len(part.encode('utf-8'))
        if total > 160 * 1024:
            raise ValueError('模型输出超过编码预算')
        buffer += public_text.feed(part)
        while '\n' in buffer:
            line, buffer = buffer.split('\n', 1)
            if not line.strip():
                continue
            count += 1
            if count > 20:
                raise ValueError('模型编码步骤超过预算')
            yield CodingStep.model_validate_json(line)
    buffer += public_text.finish()
    if buffer.strip():
        if count >= 20:
            raise ValueError('模型编码步骤超过预算')
        yield CodingStep.model_validate_json(buffer)


def apply_step(document: dict, step: CodingStep, run_id: str) -> dict:
    if document.get('active_run_id') != run_id or document['phase'] != 'generating':
        raise ValueError('旧编码轮次已失效')
    updated = deepcopy(document)
    safe_text(step.message)
    if step.kind == 'file':
        if document.get('round_mode') == 'discuss':
            raise ValueError('讨论轮次不能修改源码，请明确切换为编程后再提交文件')
        if not editable_path(step.path):
            raise ValueError('模型试图修改受保护文件')
        safe_text(step.content)
        updated['previous'][step.path] = updated['files'].get(step.path, '')
        updated['files'][step.path] = step.content
        updated['authored_paths'] = sorted(set(updated.get('authored_paths', [])) | {step.path})
        if len(updated['files']) > MAX_PROJECT_FILES or sum(len(value.encode('utf-8')) for value in updated['files'].values()) > 128 * 1024:
            raise ValueError('源文件超过项目预算')
        append_event(updated, 'file', step.message or '候选文件已生成，等待校验和审阅', step.path)
    else:
        append_event(updated, step.kind, step.message)
        if step.kind == 'summary':
            updated['round_summary'] = step.message
    updated['revision'] += 1
    return updated


def finish_document(document: dict, *, error: Exception | None = None) -> None:
    if document.get('round_mode') == 'discuss':
        document['phase'] = document.get('round_base_phase', 'draft')
        document['round_failed'] = error is not None
        append_event(document, 'discussion', '讨论未完成，源码与原校验状态已保留' if error else '讨论完成，源码与原校验状态已保留')
        document['revision'] += 1
        return
    issues = validate_project(document)
    if error is not None:
        # Provider output and exception text may contain private content. Explain
        # the failed boundary without copying either into the public workspace.
        reason = ('模型编码输出不符合 JSON Lines 协议，请要求只输出约定的 plan/file/summary 对象。'
                  if isinstance(error, ValidationError) else
                  '本轮模型编码未完成，已保留候选文件；请显式重试或修正编码要求。')
        issues.insert(0, reason)
    document['validation'] = issues
    document['phase'] = 'validation_failed' if issues else 'ready_for_review'
    document['round_failed'] = bool(issues)
    append_event(document, 'validation', '编码未完成，请查看具体原因后重试' if error else
        ('文件校验未通过，请查看具体缺口' if issues else '结构校验通过；仍需人工审阅代码与业务使用方式'))
    document['revision'] += 1


def execute(payload, tenant_id: str, user_id: str, run_id: str, token: str, generation: int) -> None:
    identity = {'tenant_id': tenant_id, 'user_id': user_id,
                'llm_trace_context': {'correlation_id': run_id, 'user_id': user_id}}
    workspace_id = payload['workspace_id']

    def checkpoint(step=None, *, finish: bool = False, error: Exception | None = None,
                   tool_operation: str | None = None, tool_result: tuple | None = None):
        with SessionLocal() as db:
            db.info.update(identity)
            jobs.assert_execution_lease(db, run_id, lease_token=token, lease_generation=generation,
                                        for_update=True, scenario_verb='write')
            row = owned_root(db, workspace_id, lock=True)
            release_service._scenario_for_manage(db, row.proposal['manifest']['scenario']['id'])
            document = deepcopy(row.proposal)
            assert_adapter_identity(document)
            if document.get('active_run_id') != run_id:
                raise ValueError('旧编码轮次已失效')
            if error is None:
                resources.prepare_context(db, document)
            if step:
                document = apply_step(document, step, run_id)
            if tool_operation is not None:
                resource_runtime.reserve_operation(document, tool_operation, run_id)
                document['revision'] += 1
            if tool_result is not None:
                name, content, record = tool_result
                resource_runtime.record_result(document, name=name, content=content, record=record, run_id=run_id)
                document['revision'] += 1
            if finish or error is not None:
                finish_document(document, error=error)
            if error is not None:
                record_failure_diagnostic(document, error, run_id=run_id, diagnostic=active_tool_diagnostic)
            row.proposal = document
            if finish:
                run = db.get(AssistantRequestRun, run_id)
                message = db.get(AssistantMessage, run.assistant_message_id)
                failed = document.get('round_failed', document['phase'] == 'validation_failed')
                message.content = ('讨论已完成，源码与原校验状态已保留。' if document.get('round_mode') == 'discuss' and not failed else
                                   '编码自动修复已达到上限，候选文件已保留，请修正需求或更换模型后继续。' if failed
                                   else '插件候选文件已生成；请在编码工作台核对差异与校验结果。')
                message.context = {**dict(message.context or {}), 'assistant_request_run_id': run_id,
                                   'assistant_request_status': 'failure_committed' if failed else 'result_committed'}
            db.commit()
            return deepcopy(document)

    trace_db = SessionLocal()
    trace_db.info.update(identity)
    active_tool_diagnostic = None
    try:
        jobs.assert_execution_lease(trace_db, run_id, lease_token=token, lease_generation=generation, scenario_verb='write')
        row = owned_root(trace_db, workspace_id)
        document = deepcopy(row.proposal)
        assert_adapter_identity(document)
        if document.get('active_run_id') != run_id:
            raise ValueError('旧编码轮次已失效')
        method_context = resources.prepare_context(trace_db, document)
        from .plugin_authoring_context import release_context
        release_context(trace_db, document['release_id'], document['acceptance_request']['expected_revision'])
        cfg = tenant_service.get_visible(trace_db, LLMConfig, document['llm_config_id'])
        if cfg is None or not llm_service.supports_capability(cfg, 'chat'):
            raise ValueError('编码模型已不可用')
        trace_db.expunge(cfg)
        trace_db.rollback()
        deadline = time.monotonic() + 180
        native_tools = llm_service.supports_capability(cfg, 'tool')
        def perform_tool(name, arguments):
            nonlocal active_tool_diagnostic
            active_tool_diagnostic = tool_diagnostic(name, arguments)
            if time.monotonic() >= deadline:
                raise ValueError('编程工具时间预算已耗尽')
            current = checkpoint(tool_operation=name)
            with SessionLocal() as resource_db:
                resource_db.info.update(identity)
                jobs.assert_execution_lease(resource_db, run_id, lease_token=token,
                                            lease_generation=generation, scenario_verb='write')
                resources.prepare_context(resource_db, current)
                result, record = resource_runtime.execute(resource_db, current, name, arguments, deadline=deadline)
            checkpoint(tool_result=(name, result, record))
            return result

        def generate(current, instruction):
            def stream(messages):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('编码自动修复时间预算已耗尽')
                chunks = llm_service.chat_stream(cfg, messages, db=trace_db,
                    tools=coding_tools(current) if native_tools else None,
                    temperature=0.2, max_tokens=12000, request_timeout=min(60, remaining), max_retries=0,
                    total_timeout=remaining, max_output_chars=130000, before_provider_call=lambda: trace_db.rollback())
                with closing(chunks):
                    yield from authorized_chunks(chunks)
            messages = coding_messages({**current, **method_context}, instruction, native_tools=native_tools)
            if native_tools:
                yield from native_coding_steps(messages, stream=stream, parse=parse_tool_steps,
                                              execute_tool=perform_tool, mode=current.get('round_mode', 'generate'))
            else:
                yield from parse_steps(stream(messages))

        def authorized_chunks(chunks):
            next_check = 0.0
            for chunk in chunks:
                if time.monotonic() >= next_check:
                    with SessionLocal() as authorization_db:
                        authorization_db.info.update(identity)
                        jobs.assert_execution_lease(authorization_db, run_id, lease_token=token,
                                                    lease_generation=generation, scenario_verb='write')
                    next_check = time.monotonic() + 1
                yield chunk

        code_and_repair(document, payload['instruction'], generate=generate, checkpoint=checkpoint, step_factory=CodingStep)
    except Exception as error:
        log_boundary_failure(error, active_tool_diagnostic)
        try:
            checkpoint(error=error)
        except (ValueError, jobs.AssistantRequestError):
            pass  # Cancellation/newer edits already own the workspace.
        raise
    finally:
        trace_db.close()
