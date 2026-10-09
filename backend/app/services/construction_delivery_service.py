"""Server projections distinguish delivered definitions from repair work."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy


def question_contract(question: dict) -> dict:
    identity = {key: question.get(key) for key in (
        "code", "message", "source_refs", "affected_change_keys",
    )}
    question_id = hashlib.sha256(json.dumps(identity, sort_keys=True,
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()[:24]
    return {**question, "question_id": question_id,
        "completion_condition": "补充信息必须形成可追溯依据；顾问重新生成后，由服务端校验原阻塞及依赖是否消除。提交回答本身不代表已解决。",
        "next_options": ["补充事实或依据", "说明修正口径", "请求重新规划并说明取舍"]}


def build_delivery(payload: dict) -> dict:
    # Properties are included in the parent object. Counting both inflates the
    # visible completion ratio when an object contains many trivial fields.
    candidates = [item for item in payload.get("draft_candidates", [])
        if isinstance(item, dict) and item.get("resource_kind") != "property"]
    ready, blocked, implementation = [], [], []
    governed = {item['resource_key']: item for item in
        (payload.get('candidate_governance') or {}).get('candidate_results', [])}
    governance_skipped = bool((payload.get('candidate_governance') or {}).get('skipped_reason'))
    for item in candidates:
        key = str(item.get("resource_key") or "")
        definition = item.get("payload") or {}
        if governance_skipped or not governed.get(key, item).get("promotion_eligible"):
            blocked.append(key)
        elif item.get("resource_kind") == "function" and definition.get("runtime_kind", "contract") == "contract":
            implementation.append(key)
        elif item.get("resource_kind") == "action" and definition.get("executor_type", "unbound") in {"none", "unbound"}:
            implementation.append(key)
        else:
            ready.append(key)
    issues = [item for item in payload.get("unresolved", [])
        if isinstance(item, dict) and item.get("blocking", True)]
    missing = [item for item in issues if item.get("code") in {"missing_source_entity", "missing_source_relation"}]
    return {"version": "construction-delivery.v1", "definition_count": len(candidates),
        "validated_definition_count": len(ready), "blocked_definition_count": len(blocked),
        "implementation_required_count": len(implementation), "missing_requirement_count": len(missing),
        "validated_keys": ready, "blocked_keys": blocked, "implementation_required_keys": implementation,
        "definition_complete": bool(candidates) and not issues and not implementation and not blocked,
        "business_acceptance": "not_verified",
        "questions": (payload.get("decision_gate") or {}).get("questions", []),
        "repair_summary": payload.get("repair_summary"),
        "next_step": "采用完整定义后，以本次输入运行成功、边界和失败案例；业务验收通过后人工发布并构建插件。"}


def apply_governance(payload: dict, summary: dict) -> dict:
    """Final persisted governance must replace optimistic compiler verdicts."""
    result = deepcopy(payload)
    result['candidate_governance'] = deepcopy(summary)
    by_key = {item['resource_key']: item for item in summary.get('candidate_results', [])}
    issues = list(result.get('unresolved', []))
    for candidate in result.get('draft_candidates', []):
        final = by_key.get(candidate['resource_key'])
        if final is None:
            continue
        candidate['promotion_eligible'] = final['promotion_eligible']
        candidate['validation_status'] = 'ready' if final['promotion_eligible'] else 'blocked'
        candidate['validation_issues'] = deepcopy(final['validation_issues'])
        for issue in final['validation_issues']:
            if issue.get('blocking', True) and issue not in issues:
                issues.append(deepcopy(issue))
    if summary.get('skipped_reason'):
        issues.append({'code': 'candidate_revalidation_required', 'blocking': True,
            'message': '最终治理校验未完成，不能确认建设可用。',
            'resolution_hint': '缩小候选批次后重新校验。'})
    result['unresolved'] = issues
    return result
