"""Check a decision supplied by an already authenticated third-party adapter."""
from __future__ import annotations

import argparse
import json
from typing import Any

from contracts import (ContractError, check_context, index_by, instant,
                       load_json, require_same, validate)


def check_decision(context: dict[str, Any], request: dict[str, Any],
                   decision: dict[str, Any]) -> dict[str, Any]:
    check_context(context)
    validate("decision_request", request)
    validate("decision", decision)
    require_same(context, request, ("project_id", "context_revision", "channel_id"))
    require_same(request, decision, ("project_id", "context_revision", "channel_id",
                                    "request_id", "actor_id", "review_version", "review_sha256"))
    if request["status"] != "pending":
        raise ContractError("request_not_pending")
    if decision["decision_id"] in context["consumed_decision_ids"]:
        raise ContractError("decision_already_consumed")
    participants = index_by(context["participants"], "participant_id")
    actor = participants.get(decision["actor_id"])
    if (actor is None or not actor["active"] or actor["channel_id"] != context["channel_id"]
            or request["role_key"] not in actor["roles"]):
        raise ContractError("actor_not_authorized")
    review = index_by(context["review_contents"], "review_id").get(request["review_id"])
    if (review is None or review["sha256"] != request["review_sha256"]
            or review["version"] != request["review_version"]):
        raise ContractError("review_content_changed")
    issued, expires = instant(request["issued_at"]), instant(request["expires_at"])
    decided, now = instant(decision["decided_at"]), instant(context["as_of"])
    if not issued <= decided <= now < expires:
        raise ContractError("decision_time_invalid_or_expired")
    return {"eligible": decision["outcome"] == "approved", "outcome": decision["outcome"],
            "project_id": context["project_id"], "expected_revision": context["context_revision"],
            "request_id": request["request_id"], "decision_id": decision["decision_id"],
            "executed": False, "requires_atomic_commit": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("context")
    parser.add_argument("request")
    parser.add_argument("trusted_decision")
    args = parser.parse_args()
    try:
        result = check_decision(load_json(args.context), load_json(args.request),
                                load_json(args.trusted_decision))
    except (ContractError, OSError) as exc:
        print(json.dumps({"eligible": False, "code": str(exc) if isinstance(exc, ContractError) else "file_unavailable"}))
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
