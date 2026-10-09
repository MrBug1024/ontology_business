"""Read the trusted client's public signatures and receipt schema for coding."""
from __future__ import annotations

import ast
import re

from ..external_api_schemas import ExternalCapabilityReceiptOut
from .scenario_package_artifact import TEMPLATE_ROOT
from .plugin_delivery_profile import SCENARIO_CREDENTIAL_SCOPES


def client_contract() -> dict:
    source = (TEMPLATE_ROOT / 'server.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    signatures = []
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and any(
                isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == 'tool' for decorator in node.decorator_list):
            signatures.append({'name': node.name, 'signature': ast.unparse(node.args),
                               'description': ast.get_docstring(node) or ''})
    return {'version': 'scenario-client-contract.v1', 'tools': signatures,
            'environment': sorted(set(re.findall(r'SCENARIO_[A-Z0-9_]+', source))),
            'credential_scopes': list(SCENARIO_CREDENTIAL_SCOPES),
            'receipt_schema': ExternalCapabilityReceiptOut.model_json_schema(),
            'workflow_completion': (
                'Receipt identity is invocation_id. The server projects current workflow state into receipt.status; '
                'receipt.output.status is the workflow state and receipt.output.result is the execution trace, not a flat business object. '
                'For a workflow with a declared output contract, inspect receipt.output.result.steps: a successful step belonging to '
                'the declared output nodes and marked contract_validation="passed" supplies its result as the business output. '
                'An end step is an output only when declared; do not assume every succeeded workflow has an end result or invent fields '
                'when no output contract is declared. Preserve the actual structured receipt. running, queued and awaiting_approval '
                'do not prove business completion; rejected, failed, cancelled and timed_out are not success. '
                'If receipt.status="succeeded" but receipt.output.status is queued, pending, running, retry_waiting or '
                'awaiting_approval, this only proves enqueueing: keep the same invocation_id and read get_scenario_receipt '
                'with a bounded wait; do not submit another workflow while waiting. If the wait limit is reached, report pending. '
                'Use get_scenario_receipt and advertised delivery.interactions, present reply_texts and wait for an explicit human reply. '
                'Do not blindly replay an indeterminate effect. Never invent execution_id, workflow_status or workflow_result fields.')}


def check_invocation_options(call: ast.Call, capability: dict, contract: dict | None) -> list[str]:
    if not contract:
        return []  # Published v1 sources retain their original validation semantics.
    keywords = {item.arg: item.value for item in call.keywords}
    allowed = {'mode', 'managed_inputs', 'idempotency_key', 'correlation_id', 'request_id', 'confirmation'}
    if any(key not in allowed for key in keywords) or len(call.args) != 3:
        return ['客户端参数不符合受信工具签名，请使用已公布的关键字参数']
    if capability.get('idempotency_required'):
        value = keywords.get('idempotency_key')
        if value is None or isinstance(value, ast.Constant) and (not isinstance(value.value, str) or not value.value.strip()):
            return ['此能力要求幂等键：调用必须显式提供 idempotency_key，同一请求重试复用，改变输入创建新键']
    return []
