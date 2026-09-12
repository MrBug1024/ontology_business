from app.services.assistant_decision_gate import attach_decision_gate, build_decision_gate


def test_clear_evidence_allows_formal_working_model():
    payload = {
        "entities": [{"key": "order", "name": "订单", "evidence_refs": ["C1"]}],
        "coverage": [
            {"source_refs": ["C1"], "status": "modeled", "change_keys": ["entity:order"]}
        ],
        "unresolved": [],
    }

    gate = build_decision_gate(payload)

    assert gate["mode"] == "formalize"
    assert gate["safe_to_formalize"] is True
    assert gate["evidence_coverage"]["modeled_ratio"] == 1.0


def test_blocking_ambiguity_requires_user_alignment():
    payload = {
        "coverage": [{"source_refs": ["C1"], "status": "ambiguous", "reason": "状态枚举未定义"}],
        "unresolved": [{
            "code": "MISSING_ENUM",
            "message": "订单状态枚举未定义",
            "blocking": True,
            "source_refs": ["C1"],
            "resolution_hint": "请确认允许的状态值",
        }],
    }

    gate = build_decision_gate(payload)

    assert gate["mode"] == "clarify"
    assert gate["blocking_question_count"] == 1
    assert gate["questions"][0]["source_refs"] == ["C1"]
    assert gate["human_review_required"] is True


def test_side_effecting_resource_stays_in_candidate_review():
    payload = {
        "actions": [{"key": "approve", "name": "审批", "side_effecting": True, "evidence_refs": ["C2"]}],
        "coverage": [{"source_refs": ["C2"], "status": "modeled"}],
        "unresolved": [],
    }

    gate = build_decision_gate(payload)

    assert gate["mode"] == "candidate_review"
    assert "SIDE_EFFECTING_ACTION" in gate["risk_codes"]
    assert gate["safe_to_formalize"] is False


def test_attach_does_not_mutate_compiler_payload():
    payload = {"coverage": [], "unresolved": []}

    result = attach_decision_gate(payload)

    assert "decision_gate" not in payload
    assert result["decision_gate"]["version"] == "decision-gate.v1"


def test_resource_without_evidence_cannot_be_formalized():
    gate = build_decision_gate({"entities": [{"key": "order", "name": "订单"}]})

    assert gate["mode"] == "clarify"
    assert gate["reason_codes"] == ["MISSING_EVIDENCE"]
    assert gate["missing_evidence_resource_count"] == 1
