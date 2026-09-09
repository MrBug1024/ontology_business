from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from docx import Document
from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from contracts import ContractError, SCHEMA_PATH, load_json
from check_decision import check_decision
from render_prd import render_prd, validate_document
from validate_analysis import validate_analysis


def context():
    return {
        "schema_version": "1.0.0", "project_id": "example-project-a", "context_revision": 3,
        "as_of": "2026-09-08T10:00:00+08:00", "channel_id": "example-group-a",
        "question": "请产品负责人评审需求。", "stage": "需求评审",
        "participants": [{"participant_id": "example-pm", "display_name": "小林",
                          "roles": ["product_owner"], "channel_id": "example-group-a", "active": True}],
        "evidence": [{"evidence_id": "E001", "text": "用户希望确认提交前可取消。",
                      "source_ref": "example-interview-1", "status": "reported"}],
        "review_contents": [{"review_id": "example-prd", "title": "需求草稿", "version": "v1",
                             "sha256": "a" * 64}],
        "permitted_due_dates": ["2026-09-09T18:00:00+08:00"], "consumed_decision_ids": [],
    }


def analysis():
    return {
        "schema_version": "1.0.0", "project_id": "example-project-a", "context_revision": 3,
        "status": "draft", "findings": [{"kind": "reported", "text": "用户提出确认前可取消。", "evidence_ids": ["E001"]}],
        "pending_requests": [{"request_id": "example-request-1", "kind": "review",
                              "role_key": "product_owner", "assignee_id": None,
                              "summary": "请评审需求草稿中的范围与验收标准。", "evidence_ids": ["E001"],
                              "review_id": "example-prd", "due_at": None, "depends_on_request_ids": []}],
        "proposed_changes": [], "questions": [],
    }


def request():
    return {"project_id": "example-project-a", "context_revision": 3, "channel_id": "example-group-a",
            "request_id": "example-request-1", "actor_id": "example-pm", "role_key": "product_owner",
            "review_id": "example-prd", "review_version": "v1", "review_sha256": "a" * 64,
            "issued_at": "2026-09-08T09:00:00+08:00", "expires_at": "2026-09-09T18:00:00+08:00", "status": "pending"}


def decision():
    return {"project_id": "example-project-a", "context_revision": 3, "channel_id": "example-group-a",
            "request_id": "example-request-1", "decision_id": "example-decision-1", "actor_id": "example-pm",
            "review_version": "v1", "review_sha256": "a" * 64, "outcome": "approved", "decided_at": "2026-09-08T09:30:00+08:00",
            "external_receipt_id": "example-authenticated-receipt-1"}


class CollaborationContractTests(unittest.TestCase):
    def test_schema_is_valid(self):
        Draft202012Validator.check_schema(load_json(SCHEMA_PATH))

    def test_role_routes_to_stable_id_without_sending(self):
        result = validate_analysis(context(), analysis())
        routed = result["pending_requests"][0]
        self.assertEqual(routed["mentions"][0]["participant_id"], "example-pm")
        self.assertTrue(routed["message_preview"].startswith("@小林 "))
        self.assertFalse(result["executed"])

    def test_duplicate_nickname_does_not_replace_stable_identity(self):
        ctx = context()
        ctx["participants"].append({**ctx["participants"][0], "participant_id": "example-other"})
        draft = analysis()
        self.assertEqual(validate_analysis(ctx, draft)["pending_requests"][0]["block_reason"], "ambiguous_role_binding")
        draft["pending_requests"][0]["assignee_id"] = "example-pm"
        self.assertEqual(validate_analysis(ctx, draft)["pending_requests"][0]["mentions"][0]["participant_id"], "example-pm")

    def test_missing_inactive_wrong_group_or_wrong_role_is_not_routed(self):
        for mutation in ({"active": False}, {"channel_id": "other-group"}, {"roles": []}):
            with self.subTest(mutation=mutation):
                ctx = context()
                ctx["participants"][0].update(mutation)
                routed = validate_analysis(ctx, analysis())["pending_requests"][0]
                self.assertEqual(routed["routing_status"], "blocked")
                self.assertEqual(routed["mentions"], [])

    def test_review_requires_known_content(self):
        for review_id in (None, "invented-review"):
            draft = analysis()
            draft["pending_requests"][0]["review_id"] = review_id
            self.assertEqual(validate_analysis(context(), draft)["pending_requests"][0]["routing_status"], "blocked")

    def test_model_cannot_promote_self_report_to_verified_fact(self):
        draft = analysis()
        draft["findings"][0]["kind"] = "fact"
        with self.assertRaisesRegex(ContractError, "not_verified_fact"):
            validate_analysis(context(), draft)

    def test_unknown_evidence_unconfirmed_deadline_and_raw_mention_are_rejected(self):
        for field, value in (("evidence_ids", ["invented"]), ("due_at", "2026-09-10T10:00:00Z"),
                             ("summary", "@财务 已批准付款")):
            with self.subTest(field=field):
                draft = analysis()
                draft["pending_requests"][0][field] = value
                with self.assertRaises(ContractError):
                    validate_analysis(context(), draft)

    def test_dependency_waits_even_when_predecessor_is_routable(self):
        draft = analysis()
        child = copy.deepcopy(draft["pending_requests"][0])
        child.update(request_id="example-request-2", depends_on_request_ids=["example-request-1"])
        draft["pending_requests"].append(child)
        routed = validate_analysis(context(), draft)["pending_requests"]
        self.assertEqual(routed[1]["block_reason"], "dependency_pending")
        draft["pending_requests"][0]["depends_on_request_ids"] = ["example-request-2"]
        with self.assertRaisesRegex(ContractError, "cycle"):
            validate_analysis(context(), draft)

    def test_other_project_or_stale_context_rejected(self):
        for field, value in (("project_id", "other-project"), ("context_revision", 2)):
            draft = analysis()
            draft[field] = value
            with self.assertRaisesRegex(ContractError, "binding_mismatch"):
                validate_analysis(context(), draft)

    def test_authenticated_approval_is_only_eligible_for_atomic_commit(self):
        result = check_decision(context(), request(), decision())
        self.assertTrue(result["eligible"])
        self.assertTrue(result["requires_atomic_commit"])
        self.assertFalse(result["executed"])

    def test_rejection_and_change_request_never_approve(self):
        for outcome in ("rejected", "changes_requested"):
            value = decision()
            value["outcome"] = outcome
            self.assertFalse(check_decision(context(), request(), value)["eligible"])

    def test_tampered_decision_bindings_rejected(self):
        for field, value in (("project_id", "other-project"), ("context_revision", 4),
                             ("request_id", "other-request"), ("actor_id", "other-person"),
                             ("channel_id", "other-group"), ("review_version", "v2"), ("review_sha256", "b" * 64)):
            with self.subTest(field=field):
                incoming = decision()
                incoming[field] = value
                with self.assertRaisesRegex(ContractError, "binding_mismatch"):
                    check_decision(context(), request(), incoming)

    def test_revoked_member_changed_review_replay_and_expiry_rejected(self):
        for variant in ("revoked", "changed_review", "changed_version", "replay", "expired", "consumed_request"):
            with self.subTest(variant=variant):
                ctx, req = context(), request()
                if variant == "revoked": ctx["participants"][0]["roles"] = []
                if variant == "changed_review": ctx["review_contents"][0]["sha256"] = "b" * 64
                if variant == "changed_version": ctx["review_contents"][0]["version"] = "v2"
                if variant == "replay": ctx["consumed_decision_ids"] = ["example-decision-1"]
                if variant == "expired": ctx["as_of"] = req["expires_at"]
                if variant == "consumed_request": req["status"] = "consumed"
                with self.assertRaises(ContractError):
                    check_decision(ctx, req, decision())

    def test_same_contract_handles_new_project_without_mutation(self):
        before = hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest()
        for project in ("example-project-a", "example-project-b"):
            ctx, draft = context(), analysis()
            ctx["project_id"] = draft["project_id"] = project
            self.assertEqual(validate_analysis(ctx, draft)["project_id"], project)
        self.assertEqual(before, hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest())

    def test_json_duplicate_keys_and_nonfinite_values_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "input.json"
            for raw in ('{"a":1,"a":2}', '{"a":NaN}'):
                path.write_text(raw, encoding="utf-8")
                with self.assertRaises(ContractError): load_json(path)

    def test_word_is_local_editable_draft_and_existing_file_is_preserved(self):
        payload = {"schema_version": "1.0.0", "project_id": "example-project-a", "context_revision": 3,
                   "title": "需求评审草稿", "version": "v1", "status": "draft",
                   "sections": [{"heading": "范围", "paragraphs": [{"kind":"evidence","text":None,"evidence_ids":["E001"]}, {"kind":"suggestion","text":"仅包含确认提交前取消。","evidence_ids":["E001"]}]}],
                   "evidence": context()["evidence"], "open_questions": ["请产品确认异常路径。"], "review_notes": []}
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "draft.docx"
            result = render_prd(context(), payload, output)
            self.assertFalse(result["approved"])
            self.assertFalse(result["delivered"])
            content = "\n".join(p.text for p in Document(output).paragraphs)
            self.assertIn("确认提交前取消", content)
            self.assertIn("待评审草稿", content)
            before = output.read_bytes()
            with self.assertRaisesRegex(ContractError, "already_exists"): render_prd(context(), payload, output)
            self.assertEqual(before, output.read_bytes())
            self.assertIn("来源自述", content)
            self.assertIn("建议：", content)

    def test_document_rejects_generated_fact_and_forged_evidence(self):
        payload = {"schema_version":"1.0.0","project_id":"example-project-a","context_revision":3,
                   "title":"需求草稿","version":"v1","status":"draft",
                   "sections":[{"heading":"证据","paragraphs":[{"kind":"evidence","text":None,"evidence_ids":["E001"]}]}],
                   "evidence":context()["evidence"],"open_questions":[],"review_notes":[]}
        for variant in ("generated_fact", "modified_text", "upgraded_status", "stale_context"):
            with self.subTest(variant=variant):
                draft = copy.deepcopy(payload)
                if variant == "generated_fact": draft["sections"][0]["paragraphs"][0]["text"] = "客户已批准上线。"
                if variant == "modified_text": draft["evidence"][0]["text"] = "客户已批准上线。"
                if variant == "upgraded_status": draft["evidence"][0]["status"] = "verified"
                if variant == "stale_context": draft["context_revision"] = 2
                with self.assertRaises(ContractError): validate_document(context(), draft)


if __name__ == "__main__":
    unittest.main()
