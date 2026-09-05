from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AssistantAttachment, AssistantThread, Tenant, User
from app.routers import assistant
from app.services import assistant_compilation_job_service
from app.services import content_retrieval_service
from app.services import scenario_model_compiler


def _large_attachment() -> SimpleNamespace:
    relevant = "合同审批规则要求业务负责人复核。"
    unrelated_tail = "RAW-UNRELATED-TAIL-MUST-NOT-ENTER-PROMPT"
    body = relevant + ("普通历史记录。" * 2_000) + unrelated_tail
    return SimpleNamespace(
        id="attachment-large-data-boundary",
        tenant_id="tenant-a",
        created_by_user_id="user-a",
        thread_id="thread-a",
        filename="contract-rules.md",
        mime="text/markdown",
        size=len(body.encode("utf-8")),
        content_hash="a" * 64,
        status="parsed",
        parsed_text=body,
        error="",
        bucket_name="private-bucket",
        object_key="tenant-a/raw-object-key",
        object_url="minio://private-bucket/tenant-a/raw-object-key",
    )


def test_assistant_attachment_context_contains_manifest_and_bounded_citations_only() -> None:
    attachment = _large_attachment()

    with patch.object(
        content_retrieval_service,
        "authorized_attachments",
        return_value=[attachment],
    ):
        context, sources = assistant._attachment_context(
            [attachment],
            query="合同审批规则",
            top_k=2,
            max_chars=900,
            db=SimpleNamespace(),
            tenant_id=attachment.tenant_id,
            user_id=attachment.created_by_user_id,
            thread_id=attachment.thread_id,
        )

    assert "附件内容清单" in context
    assert "【A1】" in context
    assert "合同审批规则" in context
    assert "RAW-UNRELATED-TAIL-MUST-NOT-ENTER-PROMPT" not in context
    assert attachment.parsed_text not in context
    assert sources[0]["characters"] == len(attachment.parsed_text)
    assert sources[0]["retrieval_complete"] is False
    assert attachment.bucket_name not in context
    assert attachment.object_key not in context
    assert attachment.object_url not in context


def test_compilation_freezes_bounded_passages_without_raw_attachment_text() -> None:
    attachment = _large_attachment()

    documents = assistant_compilation_job_service.canonical_compiler_documents(
        [attachment],
        query="合同审批规则",
        top_k=2,
        max_chars=900,
    )

    assert len(documents) == 1
    assert "text" not in documents[0]
    assert documents[0]["retrieval_complete"] is False
    assert len(documents[0]["passages"]) <= 2
    serialized = str(documents)
    assert "合同审批规则" in serialized
    assert "RAW-UNRELATED-TAIL-MUST-NOT-ENTER-PROMPT" not in serialized
    assert attachment.parsed_text not in serialized


def test_legacy_compiler_document_is_retrieved_before_prompt_construction() -> None:
    attachment = _large_attachment()
    bundle = scenario_model_compiler.build_source_bundle(
        "请根据合同审批规则编译模型",
        [{
            "id": "legacy-contract-rules",
            "filename": attachment.filename,
            "status": "parsed",
            "text": attachment.parsed_text,
        }],
    )

    with patch.object(scenario_model_compiler, "_existing_catalog", return_value={}):
        prompt = scenario_model_compiler._compiler_prompt(
            SimpleNamespace(id="scenario-a", name="测试场景"),
            message="请根据合同审批规则编译模型",
            paragraphs=bundle["paragraphs"],
            mapping_catalog=[],
        )

    assert "合同审批规则" in prompt
    assert "source_profile" in prompt
    assert "retrieval_complete" in prompt
    assert "RAW-UNRELATED-TAIL-MUST-NOT-ENTER-PROMPT" not in prompt
    assert attachment.parsed_text not in prompt
    assert bundle["documents"][0]["retrieval_complete"] is False


def test_v1_execution_input_is_projected_before_compiler_recovery() -> None:
    attachment = _large_attachment()
    policy = {
        "llm_call_budget": 3,
        "request_timeout": 30.0,
        "assistant_scope_key": "scenario:scenario-a|path:/",
    }
    context_fingerprint = "f" * 64
    legacy_input = {
        "version": 1,
        "compiler_message": "请根据合同审批规则编译模型",
        "compiler_documents": [{
            "id": "legacy-contract-rules",
            "filename": attachment.filename,
            "status": "parsed",
            "text": attachment.parsed_text,
        }],
        "prepared_context": {
            "mapping_catalog": [],
            "columns_by_table": [],
            "working_drafts": [],
            "consumed_draft_revisions": {},
            "fingerprint": context_fingerprint,
        },
        "llm_config_id": "",
        "context": {},
        "sources": [],
        "execution_policy": policy,
    }
    job = SimpleNamespace(
        id="legacy-job",
        execution_policy_fingerprint=(
            assistant_compilation_job_service.execution_policy_fingerprint(policy)
        ),
        mapping_context_fingerprint=context_fingerprint,
        llm_call_budget=3,
    )

    with patch.object(
        assistant_compilation_job_service,
        "load_leased_execution_input",
        return_value=legacy_input,
    ):
        recovered = assistant._load_compilation_execution_input(
            SimpleNamespace(),
            job,
            lease_token="test-lease",
            lease_attempt=1,
        )

    recovered_document = recovered["compiler_documents"][0]
    assert "text" not in recovered_document
    assert recovered_document["retrieval_complete"] is False
    assert recovered_document["retrieved_characters"] <= (
        content_retrieval_service.COMPILER_MAX_CHARS
    )
    serialized = str(recovered_document)
    assert "合同审批规则" in serialized
    assert "RAW-UNRELATED-TAIL-MUST-NOT-ENTER-PROMPT" not in serialized
    assert attachment.parsed_text not in serialized


def test_attachment_retrieval_rechecks_tenant_owner_and_thread_scope() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)
    try:
        tenant = Tenant(id="tenant-a", name="甲方")
        foreign_tenant = Tenant(id="tenant-b", name="乙方")
        owner = User(
            id="user-a",
            tenant_id=tenant.id,
            email="owner-boundary@example.test",
            password_hash="test-only",
            status="active",
        )
        foreign_user = User(
            id="user-b",
            tenant_id=foreign_tenant.id,
            email="foreign-boundary@example.test",
            password_hash="test-only",
            status="active",
        )
        thread = AssistantThread(
            id="thread-a",
            tenant_id=tenant.id,
            created_by_user_id=owner.id,
            scope_key="scenario:global|path:/",
            title="边界测试",
        )
        own = AssistantAttachment(
            id="own-attachment",
            tenant_id=tenant.id,
            created_by_user_id=owner.id,
            thread_id=thread.id,
            filename="own.md",
            status="parsed",
            parsed_text="本租户资料",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        foreign = AssistantAttachment(
            id="foreign-attachment",
            tenant_id=foreign_tenant.id,
            created_by_user_id=foreign_user.id,
            filename="foreign.md",
            status="parsed",
            parsed_text="其他租户机密",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add_all([tenant, foreign_tenant, owner, foreign_user, thread, own, foreign])
        db.commit()
        db.info["tenant_id"] = tenant.id
        db.info["user_id"] = owner.id

        authorized = content_retrieval_service.authorized_attachments(
            db,
            [own],
            tenant_id=tenant.id,
            user_id=owner.id,
            thread_id=thread.id,
        )
        assert [item.id for item in authorized] == [own.id]

        with pytest.raises(PermissionError, match="无权访问"):
            content_retrieval_service.authorized_attachments(
                db,
                [foreign],
                tenant_id=tenant.id,
                user_id=owner.id,
                thread_id=thread.id,
            )
        with pytest.raises(PermissionError, match="无权访问"):
            content_retrieval_service.authorized_attachments(
                db,
                [own],
                tenant_id=tenant.id,
                user_id=owner.id,
                thread_id="another-thread",
            )
    finally:
        db.close()
        engine.dispose()
