"""Real browser + real PG; scripted model verifies orchestration, not reasoning quality."""
from datetime import datetime, timedelta, timezone
import json
import secrets
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.distillation_access_schemas import SystemConfigurationRequest
from app.distillation_conversation_models import DistillationConversationTurn as Turn
from app.distillation_schemas import DistillationDocument, ProjectUpdate
from app.services import distillation_access_crypto, distillation_access_service as access
from app.services import distillation_conversation_service as service, distillation_conversation_worker as worker
from app.services import distillation_service, llm_service
from distillation_scripted_llm import call
from isolated_postgresql import isolated_postgresql, tenant_session
from test_distillation_browser_runtime import business_site, ref
from test_distillation_conversation_postgresql import setup_project, enqueue, read


def test_browser_evidence_survives_clarification_and_proposal_adoption_and_handoff(isolated_postgresql, business_site, monkeypatch):
    isolated = isolated_postgresql
    target, credentials, received = business_site
    workspace, project_id, factory = setup_project(isolated)
    ring = SimpleNamespace(active_key_id="ephemeral", keys={"ephemeral": secrets.token_bytes(32)})
    monkeypatch.setattr(distillation_access_crypto, "load_keyring", lambda: ring)
    with tenant_session(isolated.runtime_engine, workspace) as db:
        project = access.configure(db, project_id, SystemConfigurationRequest(expected_revision=1, target=target,
            credentials={"auth_type": "browser", **credentials, "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
                "authorization_basis": "Synthetic system owner allows read-only investigation", "authorized_readonly": True}))
        db.commit()
        revision = project.revision
        assert access.statuses(db, project_id)[0].status == "active"

    phase = 0

    def model(_config, messages, **_kwargs):
        nonlocal phase
        current = messages[max(index for index, item in enumerate(messages) if item["role"] == "user"):]
        outputs = [json.loads(item["content"]) for item in current if item["role"] == "tool"]
        if phase == 0:
            if not outputs:
                return {"tool_calls": [call("open_business_system", {"target_key": target.key})]}
            if len(outputs) == 1:
                page = outputs[-1]
                return {"tool_calls": [call("login_business_system", {"target_key": target.key, "page_id": page["page_id"],
                    "username_ref": ref(page, "Account"), "masked_input_ref": ref(page, "Password"), "submit_ref": ref(page, "Log in")})]}
            page = outputs[-1]
            assert "reworked" in page["observation"]["text"]
            return {"tool_calls": [call("ask_human", {"message": "历史样本包含完成和返工两类结果，请确认结果口径。",
                "questions": [{"id": "confirmation", "title": "结果确认", "question": "谁确认最终结果？", "reason": "页面结果不能证明验收人", "options": []}]})]}
        if not outputs:
            return {"tool_calls": [call("record_human_statement", {}), call("list_evidence", {})]}
        sources = outputs[-1]["evidence"]
        web = next(item for item in sources if item.get("investigation_source"))
        interview = next(item for item in sources if item.get("interview"))
        document = DistillationDocument(beneficiary="请求人", pain="完成状态缺少结果确认", desired_outcome="结果由请求人核验",
            success_metric="请求人确认的结果比例", scope="历史样本涉及的处理结果", evidence=sources,
            entities=[{"key": "result", "name": "业务结果", "identity": "业务标识及处理期间", "evidence_refs": [web["key"]]}],
            assertions=[{"key": "confirmation", "status": "inference", "statement": "结果由请求人确认，需核对更多案例", "evidence_refs": [interview["key"], web["key"]]}],
            open_questions=["返工结果是否必须再次确认？"])
        return {"tool_calls": [call("propose_document", {"message": "已结合网页证据和你的回答生成待采用产物。", "document": document.model_dump()})]}

    monkeypatch.setattr(llm_service, "chat", model)
    first = enqueue(isolated, workspace, project_id, message="调查已配置系统的历史结果", revision=revision)
    assert worker.process_next_turn(session_factory=factory)
    observed = read(isolated, first)
    assert observed.status == "waiting", observed.error
    assert ("POST", "/login") in received and ("GET", "/data") in received
    assert observed.steps[0].source.status == "login_required" and observed.steps[1].source.status == "observed"
    phase = 1
    second = enqueue(isolated, workspace, project_id, "answer", "由请求人确认最终结果；返工要求还需核对", revision=revision)
    assert worker.process_next_turn(session_factory=factory)
    proposed = read(isolated, second)
    assert proposed.status == "succeeded" and proposed.proposal, proposed.error
    with tenant_session(isolated.runtime_engine, workspace) as db:
        assert distillation_service.project(db, project_id).document["entities"] == []
        project = service.apply(db, project_id, second, revision)
        db.commit()
        assert project.document["entities"][0]["name"] == "业务结果"
        document = DistillationDocument.model_validate(project.document).model_copy(update={"decision": "continue", "decision_reason": "先做小范围验证"})
        project = distillation_service.update_project(db, project_id, ProjectUpdate(name=project.name, scenario_id=project.scenario_id,
            expected_revision=project.revision, document=document))
        publication = distillation_service.publish(db, project_id, project.revision)
        db.commit()
        assert "返工结果" in distillation_service.artifact_content(publication, "brief")["content"]
        assert "target_systems" not in distillation_service.artifact_content(publication, "contract")["content"]
    with Session(isolated.runtime_engine) as db:
        for turn_id in (first, second):
            row = db.get(Turn, turn_id)
            persisted = json.dumps([row.context, row.steps, row.checkpoint, row.proposal], ensure_ascii=False)
            assert all(value not in persisted for value in credentials.values())
