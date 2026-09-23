from types import SimpleNamespace

from app.distillation_schemas import DistillationDocument, Evidence, InterviewReference, LibraryReadReference
from app.services import distillation_service


class _EmptyReceiptDb:
    def __init__(self, turn_ids=()):
        self.turn_ids = list(turn_ids)

    def scalars(self, _statement):
        return SimpleNamespace(all=lambda: self.turn_ids)


def _document(turn_id: str) -> DistillationDocument:
    return DistillationDocument(
        evidence=[Evidence(
            key="read_receipt",
            title="历史资料样本",
            kind="material",
            role="knowledge",
            data_source_id="source-id",
            library_read=LibraryReadReference(
                turn_id=turn_id,
                step_id="step-id",
                identity_sha256="a" * 64,
            ),
        )],
        assertions=[{
            "key": "confirmed_result",
            "statement": "曾经核对过",
            "status": "fact",
            "evidence_refs": ["read_receipt"],
        }],
    )


def test_orphaned_library_receipt_is_downgraded_without_live_source_fallback(monkeypatch):
    monkeypatch.setattr(
        distillation_service.permission_service,
        "require_principal",
        lambda _db: SimpleNamespace(tenant_id="tenant-id"),
    )

    repaired = distillation_service.repair_orphaned_conversation_receipts(
        _EmptyReceiptDb(), _document("deleted-turn"), scenario_id=None,
        invalid_turn_ids={"deleted-turn"},
    )

    evidence = repaired.evidence[0]
    assert evidence.kind == "observation"
    assert evidence.data_source_id is None
    assert evidence.library_read is None
    assert "原资料调查回执所属会话已删除" in evidence.limitations
    assert repaired.assertions[0].status == "inference"


def test_existing_library_receipt_is_left_unchanged(monkeypatch):
    monkeypatch.setattr(
        distillation_service.permission_service,
        "require_principal",
        lambda _db: SimpleNamespace(tenant_id="tenant-id"),
    )
    document = _document("live-turn")

    repaired = distillation_service.repair_orphaned_conversation_receipts(
        _EmptyReceiptDb(["live-turn"]), document, scenario_id=None, invalid_turn_ids=set(),
    )

    assert repaired == document
    assert repaired.evidence[0].library_read is not None
    assert repaired.assertions[0].status == "fact"


def test_orphaned_interview_receipt_is_downgraded(monkeypatch):
    monkeypatch.setattr(
        distillation_service.permission_service,
        "require_principal",
        lambda _db: SimpleNamespace(tenant_id="tenant-id"),
    )
    document = DistillationDocument(
        evidence=[Evidence(
            key="interview_receipt",
            title="历史访谈",
            kind="observation",
            role="reference",
            summary="来自历史对话的专家陈述",
            interview=InterviewReference(turn_id="deleted-turn", message_sha256="b" * 64),
        )],
        assertions=[{
            "key": "interview_fact",
            "statement": "曾经访谈确认",
            "status": "fact",
            "evidence_refs": ["interview_receipt"],
        }],
    )

    repaired = distillation_service.repair_orphaned_conversation_receipts(
        _EmptyReceiptDb(), document, scenario_id=None, invalid_turn_ids={"deleted-turn"},
    )

    evidence = repaired.evidence[0]
    assert evidence.interview is None
    assert evidence.kind == "observation"
    assert "原对话回执所属会话已删除" in evidence.limitations
    assert repaired.assertions[0].status == "inference"
