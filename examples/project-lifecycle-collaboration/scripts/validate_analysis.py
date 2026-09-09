"""Validate a model draft and derive routing candidates without sending."""
from __future__ import annotations

import argparse
import copy
import json
from graphlib import CycleError, TopologicalSorter
from typing import Any

from contracts import (ContractError, check_context, index_by, instant,
                       load_json, require_same, validate)


def _evidence_refs(item: dict[str, Any], evidence: dict[str, Any]) -> None:
    refs = item["evidence_ids"]
    if any(ref not in evidence for ref in refs):
        raise ContractError("unknown_evidence")
    if item.get("kind") in {"fact", "reported"} and not refs:
        raise ContractError("observation_without_evidence")
    if item.get("kind") == "fact" and any(evidence[ref]["status"] != "verified" for ref in refs):
        raise ContractError("reported_evidence_is_not_verified_fact")


def _route(request: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(request)
    candidates = [person for person in context["participants"]
                  if person["active"] and person["channel_id"] == context["channel_id"]
                  and request["role_key"] in person["roles"]
                  and (request["assignee_id"] is None
                       or person["participant_id"] == request["assignee_id"])]
    reason = None
    if not candidates:
        reason = "role_binding_missing_or_forbidden"
    elif len(candidates) != 1:
        reason = "ambiguous_role_binding"
    reviews = index_by(context["review_contents"], "review_id")
    if request["kind"] in {"review", "confirm_plan", "acceptance"} and not request["review_id"]:
        reason = "review_content_required"
    if request["review_id"] is not None and request["review_id"] not in reviews:
        reason = "unknown_review_content"
    if request["due_at"] is not None:
        if request["due_at"] not in context["permitted_due_dates"]:
            raise ContractError("unconfirmed_due_date")
        if instant(request["due_at"]) <= instant(context["as_of"]):
            reason = "request_deadline_passed"
    result.update(routing_status="blocked" if reason else "ready", block_reason=reason,
                  mentions=[], message_preview=None)
    if reason is None:
        person = candidates[0]
        result["mentions"] = [{"participant_id": person["participant_id"],
                               "display_name": person["display_name"],
                               "channel_id": context["channel_id"]}]
        result["message_preview"] = f"@{person['display_name']} {request['summary']}"
    return result


def validate_analysis(context: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    check_context(context)
    validate("analysis", analysis)
    require_same(context, analysis, ("project_id", "context_revision"))
    evidence = index_by(context["evidence"], "evidence_id")
    for item in [*analysis["findings"], *analysis["proposed_changes"], *analysis["pending_requests"]]:
        _evidence_refs(item, evidence)
    if any(item["kind"] != "suggestion" for item in analysis["proposed_changes"]):
        raise ContractError("change_must_be_proposal")
    requests = index_by(analysis["pending_requests"], "request_id")
    graph = {key: item["depends_on_request_ids"] for key, item in requests.items()}
    if any(ref not in requests for refs in graph.values() for ref in refs):
        raise ContractError("unknown_request_dependency")
    try:
        order = list(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise ContractError("request_dependency_cycle") from exc
    routed = {key: _route(requests[key], context) for key in order}
    for key in order:
        if graph[key]:
            # All requests in a model draft are pending, including routable ones.
            routed[key].update(routing_status="blocked", block_reason="dependency_pending",
                               mentions=[], message_preview=None)
    result = copy.deepcopy(analysis)
    result["pending_requests"] = list(routed.values())
    result["executed"] = False
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("context")
    parser.add_argument("analysis")
    args = parser.parse_args()
    try:
        result = validate_analysis(load_json(args.context), load_json(args.analysis))
    except (ContractError, OSError) as exc:
        print(json.dumps({"valid": False, "code": str(exc) if isinstance(exc, ContractError) else "file_unavailable"}))
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
