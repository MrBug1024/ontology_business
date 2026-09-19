"""Temporary inputs and library investigation use real ownership/storage contracts."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.distillation_attachment_models import DistillationAttachment as Attachment
from app.distillation_attachment_models import DistillationTurnAttachment as Link
from app.distillation_conversation_models import DistillationConversationTurn as Turn
from app.distillation_conversation_schemas import TurnCreate
from app.distillation_models import DistillationProject, DistillationPublication
from app.distillation_schemas import DistillationDocument, ProjectCreate, ProjectUpdate
from app.models import BucketFile, DataSource, LLMConfig, OrganizationMember, User
from app.routers import business_distillation, distillation_conversation
from app.services import distillation_attachment_service as attachments, distillation_conversation_service as conversations
from app.services import distillation_conversation_worker as worker, distillation_library_service as library, distillation_service, llm_service
from app.services import distillation_conversation_lease as leases
from app.services.auth_service import get_tenant_db
from app.services.distillation_attachment_parser import ParsedAttachment
from distillation_scripted_llm import call, install_scripted_llm
from isolated_postgresql import isolated_database, isolated_postgresql, seed_workspace, tenant_session


def setup(isolated):
    workspace = seed_workspace(isolated.admin_engine)
    # Input/ownership tests create turns but do not call an external provider.
    # Preserve their intended assertions under the fail-closed model policy.
    with Session(isolated.admin_engine) as db:
        db.add(LLMConfig(
            tenant_id=workspace["tenant_id"],
            name="Test investigation fallback",
            model="test-investigation-fallback",
            capabilities=["chat", "tool"],
            enabled=True,
            routing_priority=1_000,
        ))
        db.commit()
    with tenant_session(isolated.runtime_engine, workspace) as db:
        project = distillation_service.create_project(db, ProjectCreate(name="Input investigation",
            scenario_id=workspace["scenario_id"], document=DistillationDocument(beneficiary="Requester", pain="Unresolved request",
                desired_outcome="Verified result", success_metric="Time to resolve", decision="continue", decision_reason="Human-approved pilot")))
        db.commit()
        project_id = project.id
    return workspace, project_id, sessionmaker(bind=isolated.runtime_engine)


def input_record(isolated, workspace, project_id, text="A request was recorded, but no outcome was confirmed."):
    with tenant_session(isolated.runtime_engine, workspace) as db:
        row = attachments.persist_parsed(db, project_id, request_id=uuid4().hex, filename="evidence.txt",
            byte_size=len(text.encode()), content_sha256=hashlib.sha256(text.encode()).hexdigest(),
            parsed=ParsedAttachment("text/plain", text), scenario_id=workspace["scenario_id"])
        db.commit()
        return row.id


def enqueue(isolated, workspace, project_id, attachment_ids=None):
    with tenant_session(isolated.runtime_engine, workspace) as db:
        row = conversations.enqueue(db, project_id, TurnCreate(request_id=uuid4().hex, expected_revision=1,
            message="Investigate these inputs", attachment_ids=attachment_ids or []))
        db.commit()
        return row.id


def test_multipart_upload_is_real_private_idempotent_and_never_creates_library_rows(isolated_postgresql):
    isolated = isolated_postgresql
    workspace, project_id, _factory = setup(isolated)
    app = FastAPI()
    app.include_router(distillation_conversation.router, prefix="/api")
    def scoped_db():
        with tenant_session(isolated.runtime_engine, workspace) as db:
            yield db
    app.dependency_overrides[get_tenant_db] = scoped_db
    base = f"/api/business-distillation/{project_id}/conversation/attachments"
    with TestClient(app) as client:
        response = client.post(base, data={"request_id": "upload_one"}, files={"file": ("notes.txt", b"Observed unresolved request", "text/plain")})
        assert response.status_code == 201, response.text
        result = response.json()
        assert result["status"] == "ready" and result["request_id"] == "upload_one"
        assert result["byte_size"] == len(b"Observed unresolved request")
        again = client.post(base, data={"request_id": "upload_one"}, files={"file": ("notes.txt", b"Observed unresolved request", "text/plain")})
        assert again.status_code == 201 and again.json()["id"] == result["id"]
        assert len(client.get(base).json()) == 1
        assert client.delete(base + "/" + result["id"]).status_code == 204
        assert client.get(base).json() == []
    with Session(isolated.admin_engine) as db:
        assert db.scalar(select(func.count()).select_from(DataSource).where(DataSource.tenant_id == workspace["tenant_id"])) == 0
        row = db.get(Attachment, result["id"])
        assert row.status == "removed" and row.parsed_text == ""


def test_investigation_tool_catalog_requires_project_scope_and_exposes_selection_semantics(isolated_postgresql):
    isolated = isolated_postgresql
    workspace, project_id, _factory = setup(isolated)
    app = FastAPI()
    app.include_router(distillation_conversation.router, prefix="/api")

    def scoped_db():
        with tenant_session(isolated.runtime_engine, workspace) as db:
            yield db

    app.dependency_overrides[get_tenant_db] = scoped_db
    base = f"/api/business-distillation/{project_id}/conversation/tools"
    with TestClient(app) as client:
        response = client.get(base)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["default_tool_keys"]
        assert payload["always_available_tool_keys"] == ["ask_human", "propose_document"]
        assert {item["key"] for item in payload["tools"]} == set(payload["default_tool_keys"]) | set(payload["always_available_tool_keys"])
        assert client.get(f"/api/business-distillation/{uuid4().hex}/conversation/tools").status_code == 404


def test_inputs_require_same_user_project_tenant_and_block_scope_move_before_first_turn(isolated_postgresql):
    isolated = isolated_postgresql
    workspace, project_id, _factory = setup(isolated)
    attachment_id = input_record(isolated, workspace, project_id)
    other_id = uuid4().hex
    with Session(isolated.admin_engine) as db:
        db.add(User(id=other_id, tenant_id=workspace["tenant_id"], email=other_id + "@acceptance.invalid",
            password_hash="unusable", status="active"))
        db.flush()
        db.add(OrganizationMember(organization_id=workspace["organization_id"], user_id=other_id,
            role_id=workspace["role_id"], status="active"))
        db.commit()
    another_user = {**workspace, "user_id": other_id}
    with pytest.raises(HTTPException) as wrong_user:
        enqueue(isolated, another_user, project_id, [attachment_id])
    assert wrong_user.value.status_code == 404
    with tenant_session(isolated.runtime_engine, another_user) as db:
        assert attachments.list_pending(db, project_id) == []
        with pytest.raises(HTTPException) as removal:
            attachments.remove(db, project_id, attachment_id)
        assert removal.value.status_code == 404
    with tenant_session(isolated.runtime_engine, workspace) as db:
        with pytest.raises(HTTPException) as move:
            distillation_service.update_project(db, project_id, ProjectUpdate(name="Moved",
                scenario_id=workspace["other_scenario_id"], expected_revision=1))
        assert move.value.status_code == 409
        db.rollback()
        other_project = distillation_service.create_project(db, ProjectCreate(name="Another conversation", scenario_id=workspace["scenario_id"]))
        db.commit()
        other_project_id = other_project.id
    with pytest.raises(HTTPException) as wrong_project:
        enqueue(isolated, workspace, other_project_id, [attachment_id])
    assert wrong_project.value.status_code == 404
    foreign, foreign_project, _ = setup(isolated)
    with pytest.raises(HTTPException) as wrong_tenant:
        enqueue(isolated, foreign, foreign_project, [attachment_id])
    assert wrong_tenant.value.status_code == 404


def test_followup_reads_prior_input_and_expiry_erases_text_checkpoint_and_fences_worker(isolated_postgresql, monkeypatch):
    isolated = isolated_postgresql
    workspace, project_id, factory = setup(isolated)
    attachment_id = input_record(isolated, workspace, project_id)
    restore = install_scripted_llm(isolated.admin_engine, workspace)
    def model(_cfg, messages, **_kwargs):
        if messages[-1]["role"] == "tool":
            assert "no outcome was confirmed" in messages[-1]["content"]
            return {"content": "The uploaded account lacks outcome confirmation.", "tool_calls": []}
        return {"content": "", "tool_calls": [call("read_attachment", {"attachment_id": attachment_id})]}
    monkeypatch.setattr(llm_service, "chat", model)
    try:
        first = enqueue(isolated, workspace, project_id, [attachment_id])
        assert worker.process_next_turn(session_factory=factory)
        second = enqueue(isolated, workspace, project_id)
        assert worker.process_next_turn(session_factory=factory)
        with tenant_session(isolated.runtime_engine, workspace) as db:
            assert attachments.list_pending(db, project_id) == []
            first_row, second_row = db.get(Turn, first), db.get(Turn, second)
            assert first_row.status == second_row.status == "succeeded"
            assert first_row.context["attachments"][0]["id"] == attachment_id
            assert second_row.context["attachments"] == []
            assert second_row.context["attachment_ids"] == [attachment_id]
            assert "no outcome was confirmed" in json.dumps(second_row.checkpoint)
        active = enqueue(isolated, workspace, project_id)
        with factory() as db:
            lease = leases.claim(db)
            db.commit()
        with Session(isolated.admin_engine) as db:
            db.get(Attachment, attachment_id).expires_at = attachments.now() - timedelta(seconds=1)
            db.commit()
        assert attachments.cleanup_tick(session_factory=factory) >= 1
        with factory() as db:
            row = db.get(Attachment, attachment_id)
            assert row.status == "expired" and not row.parsed_text
            assert all(not db.get(Turn, turn_id).checkpoint for turn_id in (first, second, active))
            assert db.get(Turn, active).status == "failed"
        with pytest.raises(leases.LeaseLost):
            worker._finish(lease, factory, "succeeded", message="Late read")
        followup = enqueue(isolated, workspace, project_id)
        with tenant_session(isolated.runtime_engine, workspace) as db:
            row = db.get(Turn, followup)
            assert row.context["attachment_ids"] == []
            with pytest.raises(HTTPException):
                attachments.read_attachment(db, row, attachment_id, 0, 12000)
            conversations.cancel(db, project_id, followup)
            db.commit()
    finally:
        restore()


def test_scenario_history_and_library_discovery_are_filtered_on_server(isolated_postgresql):
    isolated = isolated_postgresql
    workspace, project_id, _factory = setup(isolated)
    with tenant_session(isolated.runtime_engine, workspace) as db:
        shared = distillation_service.create_project(db, ProjectCreate(name="Legacy shared"))
        other = distillation_service.create_project(db, ProjectCreate(name="Other history", scenario_id=workspace["other_scenario_id"]))
        db.commit()
        assert [row.id for row in business_distillation.list_projects(limit=50, offset=0, scenario_id=workspace["scenario_id"], db=db)] == [project_id]
        assert [row.id for row in business_distillation.list_projects(limit=50, offset=0, scenario_id=None, shared_only=True, db=db)] == [shared.id]
        assert other.id not in [row.id for row in business_distillation.list_projects(limit=50, offset=0, scenario_id=workspace["scenario_id"], db=db)]
    with Session(isolated.admin_engine) as db:
        sources = [DataSource(tenant_id=workspace["tenant_id"], scenario_id=scenario_id, name=name, type="file_bucket", config={})
            for scenario_id, name in ((workspace["scenario_id"], "Current"), (workspace["other_scenario_id"], "Other"), (None, "Shared"))]
        db.add_all(sources)
        db.commit()
        ids = [row.id for row in sources]
    with tenant_session(isolated.runtime_engine, workspace) as db:
        listed = library.list_sources(db, workspace["scenario_id"], 0, 20)
        assert {item["data_source_id"] for item in listed["sources"]} == {ids[0], ids[2]}
        with pytest.raises(HTTPException) as other_scope:
            library.read_source(db, workspace["scenario_id"], ids[1], None)
        assert other_scope.value.status_code == 422


@pytest.mark.parametrize("publish_before_change", [False, True])
def test_library_tool_freezes_unregistered_evidence_and_rejects_adoption_after_reparse(isolated_postgresql, monkeypatch, publish_before_change):
    isolated = isolated_postgresql
    workspace, project_id, factory = setup(isolated)
    with Session(isolated.admin_engine) as db:
        source = DataSource(tenant_id=workspace["tenant_id"], scenario_id=workspace["scenario_id"],
            name="Actual library", type="file_bucket", config={})
        db.add(source)
        db.flush()
        file = BucketFile(data_source_id=source.id, filename="evidence.txt", stored_path="minio://acceptance/opaque",
            content_sha256="a" * 64, parsed_text="Original observed process", status="parsed")
        db.add(file)
        db.commit()
        source_id, file_id = source.id, file.id
    restore = install_scripted_llm(isolated.admin_engine, workspace)
    try:
        def model(_cfg, messages, **_kwargs):
            if messages[-1]["role"] != "tool":
                return {"content": "", "tool_calls": [call("read_library_source", {"data_source_id": source_id, "bucket_file_id": file_id})]}
            assert "Original observed process" in messages[-1]["content"]
            proposed = DistillationDocument(beneficiary="Requester", pain="Observed process lacks an outcome",
                desired_outcome="Verified result", success_metric="Time to resolve")
            return {"content": "", "tool_calls": [call("propose_document", {"message": "Review library findings", "document": proposed.model_dump()})]}
        monkeypatch.setattr(llm_service, "chat", model)
        turn_id = enqueue(isolated, workspace, project_id)
        assert worker.process_next_turn(session_factory=factory)
        with factory() as db:
            turn = db.get(Turn, turn_id)
            assert turn.status == "succeeded"
            assert turn.proposal["evidence"][0]["data_source_id"] == source_id
            assert turn.proposal["evidence"][0]["library_read"]["turn_id"] == turn_id
            assert turn.steps[0]["library"]["data_source_id"] == source_id
            assert db.get(DistillationProject, project_id).document["evidence"] == []
        if publish_before_change:
            with tenant_session(isolated.runtime_engine, workspace) as db:
                conversations.apply(db, project_id, turn_id, 1)
                db.commit()
                publication = distillation_service.publish(db, project_id, 2)
                db.commit()
                publication_id = publication.id
                frozen_content = distillation_service.artifact_content(publication, "provenance")["content"]
                assert "server_library_read" in frozen_content
                assert "Original observed process" in frozen_content
        with Session(isolated.admin_engine) as db:
            db.get(BucketFile, file_id).parsed_text = "Changed later"
            db.commit()
        with tenant_session(isolated.runtime_engine, workspace) as db:
            if publish_before_change:
                publication = db.get(DistillationPublication, publication_id)
                assert distillation_service.artifact_content(publication, "provenance")["content"] == frozen_content
                return
            with pytest.raises(HTTPException) as stale:
                conversations.apply(db, project_id, turn_id, 1)
            assert stale.value.status_code == 409
            db.rollback()
            assert db.get(DistillationProject, project_id).revision == 1
    finally:
        restore()


def test_concurrent_upload_retry_persists_one_input(isolated_postgresql):
    isolated = isolated_postgresql
    workspace, project_id, _factory = setup(isolated)
    def persist(_index):
        with tenant_session(isolated.runtime_engine, workspace) as db:
            row = attachments.persist_parsed(db, project_id, request_id="parallel_upload", filename="parallel.txt",
                byte_size=8, content_sha256=hashlib.sha256(b"Evidence").hexdigest(),
                parsed=ParsedAttachment("text/plain", "Evidence"), scenario_id=workspace["scenario_id"])
            db.commit()
            return row.id
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(persist, range(2)))
    assert ids[0] == ids[1]
    with Session(isolated.admin_engine) as db:
        assert db.scalar(select(func.count()).select_from(Attachment).where(Attachment.project_id == project_id)) == 1


def test_input_owner_foreign_keys_and_erasure_check_reject_invalid_persistent_state(isolated_postgresql):
    isolated = isolated_postgresql
    workspace, project_id, _factory = setup(isolated)
    attachment_id = input_record(isolated, workspace, project_id)
    turn_id = enqueue(isolated, workspace, project_id)
    foreign, foreign_project, _ = setup(isolated)
    foreign_input = input_record(isolated, foreign, foreign_project)
    for values, constraint in (
        ({"turn_id": turn_id, "attachment_id": attachment_id, "user_id": foreign["user_id"]}, "fk_distillation_turn_attachment_turn_owner"),
        ({"turn_id": turn_id, "attachment_id": foreign_input, "user_id": workspace["user_id"]}, "fk_distillation_turn_attachment_input_owner"),
    ):
        with Session(isolated.runtime_engine) as db:
            db.add(Link(**values, tenant_id=workspace["tenant_id"], project_id=project_id))
            with pytest.raises(IntegrityError) as failure:
                db.flush()
            assert failure.value.orig.diag.constraint_name == constraint
            db.rollback()
    with Session(isolated.runtime_engine) as db:
        db.get(Attachment, attachment_id).status = "expired"
        with pytest.raises(IntegrityError) as failure:
            db.flush()
        assert failure.value.orig.diag.constraint_name == "ck_distillation_attachment_expiry_content"
        db.rollback()


def test_attachment_runtime_grants_and_migration_refuse_history_loss():
    from scripts.verify_distillation_storage import verify_attachment_contract

    with isolated_database() as isolated:
        with isolated.runtime_engine.connect() as connection:
            assert verify_attachment_contract(connection)["links"] == "append_only"
        isolated.migrate("20260918_37", downgrade=True)
        isolated.migrate(isolated.head)
        workspace, project_id, _factory = setup(isolated)
        input_record(isolated, workspace, project_id)
        with pytest.raises(RuntimeError, match="attachment history exists"):
            isolated.migrate("20260918_37", downgrade=True)
        with isolated.runtime_engine.connect() as connection:
            assert verify_attachment_contract(connection)["ownership_constraints"] == 7
