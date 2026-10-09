"""Verify receipt provenance; business correctness remains a human attestation."""
from __future__ import annotations


class PackageValidationError(ValueError):
    pass


def validate_acceptance(capabilities: list[dict], receipts: dict, request, *, release_id: str, definition_hash: str) -> list[dict]:
    if not request.confirmed_business_acceptance:
        raise PackageValidationError("请先人工核对案例的业务结果，并明确确认验收")
    available = {(item['kind'], item['key']): item for item in capabilities}
    selected = []
    for capability in request.capabilities:
        item = available.get((capability.kind, capability.key))
        if item is None or not item['ready']:
            raise PackageValidationError("选择的能力不可用或尚未具备执行条件")
        selected.append(item)
    for case in request.acceptance_cases:
        row = receipts.get(case.invocation_id)
        if (row is None or row.release_id != release_id or row.definition_hash != definition_hash
                or row.capability_kind != case.kind or row.capability_key != case.key
                or row.status != case.expected_status):
            raise PackageValidationError("验收回执不可用、版本不一致或结果与预期不符")
        if case.role == 'success' and row.status != 'succeeded':
            raise PackageValidationError("成功案例必须实际执行成功")
    return selected
