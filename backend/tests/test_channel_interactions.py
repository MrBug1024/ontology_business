"""Message replies, asynchronous results and immutable evidence behavior."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from app.approval_models import WorkflowApprovalEvidence
from app.channel_interaction_schemas import ChannelReplyIn, EvidenceReference
from app.models import Conversation, DataAsset, DataAssetVersion, Message, OntologyWorkflow, OrganizationMember, OrganizationRole, User, WorkflowApprovalRequest, WorkflowRun
from app.services import agent_channel_reply_service, agent_runtime_adapter, capability_application_service, capability_delivery_service, channel_interaction_service, operations_service, permission_service
from app.services.capability_contracts import Actor, CapabilityRef, Receipt, Request
from app.services.channel_reply_parser import parse_reply
from app.services.channel_text import plain_text
from sdk.ontology_platform_sdk import CapabilityClient
from .release_fixtures import enable_current_release
from .test_agent_runtime_adapter import db, _world
from .test_agent_capability_confirmations import _preview


def approval_world(db, *, evidence=False, two_nodes=False, assigned=True):
    tenant, user, scenario, llm, _, agent = _world(db, "channel")
    nodes = [{"id": "start", "type": "start", "data": {}}]
    nodes.append({"id": "review", "type": "approval", "data": {
        "name": "业务评审", "instructions": "核对处理结果", "timeout_seconds": 600,
        "approver_user_ids": [user.id] if assigned else [], "requires_evidence": evidence,
    }})
    if two_nodes:
        nodes.append({"id": "review-2", "type": "approval", "data": {"instructions": "再次核对", "timeout_seconds": 600}})
    nodes.append({"id": "end", "type": "end", "data": {"summary": "**审核完成**\n业务记录已核对"}})
    workflow = OntologyWorkflow(id="channel-workflow", scenario_id=scenario.id, name="审批流程", nodes=nodes,
        edges=[{"id": f"e-{i}", "source": left["id"], "target": right["id"], "label": ""}
               for i, (left, right) in enumerate(zip(nodes, nodes[1:]))], enabled=True, status="active")
    scope = copy.deepcopy(agent.capability_scope)
    scope["workflows"] = {"mode": "explicit", "selected_ids": [workflow.id]}
    agent.capability_scope = scope
    conversation = Conversation(id="channel-conversation", agent_id=agent.id, created_by_user_id=user.id)
    message = Message(id="channel-response", conversation_id=conversation.id, role="assistant", content="", stream_finalized=True)
    db.add_all([workflow, conversation, message])
    db.commit()
    db.info["action_audit_context"] = {"agent_id": agent.id}
    db.info["llm_trace_context"] = {"assistant_message_id": message.id, "correlation_id": "channel-thread"}
    runtime = agent_runtime_adapter.build_runtime_context(db, agent, llm,
        turn_input=agent_runtime_adapter.AgentTurnInput(idempotency_key="channel-invocation"))
    raw = runtime.execute_tool("invoke_capability", {"kind": "workflow", "key": workflow.id, "inputs": {}})
    receipt = json.loads(raw)
    assert receipt.get("status") == "running", receipt
    message.tool_results = [{"name": "invoke_capability", "result": raw}]
    db.commit()
    run = db.get(WorkflowRun, receipt["output"]["workflow_run_id"])
    operations_service.process_available_runs(db)
    db.refresh(run)
    assert run.status == "awaiting_approval", run.error
    approval = db.scalar(select(WorkflowApprovalRequest).where(WorkflowApprovalRequest.workflow_run_id == run.id, WorkflowApprovalRequest.status == "pending"))
    return SimpleNamespace(tenant=tenant, user=user, scenario=scenario, llm=llm, agent=agent, runtime=runtime,
        conversation=conversation, message=message, run=run, approval=approval, receipt=receipt)


def evidence_version(db, world):
    asset = DataAsset(tenant_id=world.tenant.id, key="approval-evidence", name="佐证.txt", usage_plane="invocation_input",
        created_by_user_id=world.user.id, labels={"catalog_purpose": "invocation_attachment"})
    db.add(asset)
    db.flush()
    version = DataAssetVersion(tenant_id=world.tenant.id, asset_id=asset.id, version_number=1, status="ready",
        content_sha256="b" * 64, byte_size=6, version_document={"lifecycle": {"purpose": "invocation_attachment",
        "temporary": True, "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}})
    db.add(version)
    db.commit()
    return version


def test_text_rendering_preserves_meaning_without_rich_controls():
    text = plain_text("# 结果\n\n**通过**，`数量=2`\n\n[文件](https://example.test/file)\n\n&lt;script&gt;")
    assert text == "结果\n通过，数量=2\n文件 (https://example.test/file)\n<script>"
    assert parse_reply("模型认为张三同意了") is None
    assert parse_reply("请确认一下方案") is None
    assert parse_reply("同意 A-0123456789\n已核对文件").comment == "已核对文件"


def test_plain_delivery_preserves_structured_content_for_caller_file_tools():
    output = {"summary": "## Draft\n\n**Review required**", "document": {
        "media_type": "text/markdown", "content": "# Requirements\n\n- Review scope",
        "suggested_filename": "requirements.md", "status": "draft",
    }}
    receipt = Receipt(invocation_id="presentation-result", status="succeeded",
        capability=CapabilityRef(kind="function", resource_id="document-content"),
        definition_hash="a" * 64, deployment_fingerprint="b" * 64,
        data_context_fingerprint="c" * 64, output=output)
    actor = Actor(actor_type="external_api", principal_id="caller", tenant_id="workspace", user_id="user")
    projected = capability_delivery_service.project(None, actor, receipt)
    document = capability_application_service.receipt_document(projected)
    assert document["output"] == output
    assert document["delivery"]["format"] == "text/plain"
    assert "**" not in document["delivery"]["text"]
    assert document["delivery"]["attachments"] == []
    assert receipt.output["summary"] == output["summary"]


def test_analysis_runs_without_execution_confirmation_and_delivers_final_result_in_place(db):
    world = approval_world(db)
    before = capability_application_service.get_receipt(db, world.runtime._actor(), world.receipt["invocation_id"])
    assert before["status"] == "awaiting_approval"
    interaction = before["delivery"]["interactions"][0]
    assert interaction["recipient_user_ids"] == [world.user.id]
    reply = ChannelReplyIn(text="同意", message_id="human-message-1", expected_revision=interaction["revision"])
    accepted = channel_interaction_service.reply_approval(db, world.runtime._actor(), world.approval.id, reply)
    assert accepted["status"] == "approved"
    replay = channel_interaction_service.reply_approval(db, world.runtime._actor(), world.approval.id, reply)
    assert replay == accepted
    operations_service.process_available_runs(db)
    final = capability_application_service.get_receipt(db, world.runtime._actor(), world.receipt["invocation_id"])
    assert final["status"] == "succeeded"
    assert "审核完成\n业务记录已核对" in final["delivery"]["text"]
    assert final["delivery"]["interactions"] == []
    assert final["delivery"]["revision"] != before["delivery"]["revision"]
    replayed_history = world.runtime.model_historic_tool_result("invoke_capability", {}, json.dumps(world.receipt))
    assert json.loads(replayed_history)["status"] == "succeeded"


def test_bare_approval_only_matches_this_conversation_and_replayed_message_never_approves_next_node(db):
    world = approval_world(db, two_nodes=True)
    unrelated = Conversation(id="another-conversation", agent_id=world.agent.id, created_by_user_id=world.user.id)
    db.add(unrelated)
    db.commit()
    absent = agent_channel_reply_service.handle_message(db, world.agent, unrelated.id, "同意", "unrelated-reply", [], world.runtime)
    assert "没有匹配" in absent["answer"]
    assert db.get(WorkflowApprovalRequest, world.approval.id).status == "pending"
    first = agent_channel_reply_service.handle_message(db, world.agent, world.conversation.id, "同意", "reply-one", [], world.runtime)
    assert "审批已通过" in first["answer"]
    operations_service.process_available_runs(db)
    next_approval = db.scalar(select(WorkflowApprovalRequest).where(WorkflowApprovalRequest.workflow_run_id == world.run.id, WorkflowApprovalRequest.status == "pending"))
    assert next_approval.id != world.approval.id
    replay = agent_channel_reply_service.handle_message(db, world.agent, world.conversation.id, "同意", "reply-one", [], world.runtime)
    assert replay["answer"] == first["answer"]
    assert replay["receipt"]["invocation_id"] == first["receipt"]["invocation_id"]
    db.refresh(next_approval)
    assert next_approval.status == "pending"
    changed = ChannelReplyIn(text="驳回", message_id="reply-one", expected_revision=1)
    with pytest.raises(channel_interaction_service.ChannelInteractionError, match="内容发生变化"):
        channel_interaction_service.reply_approval(db, world.runtime._actor(), world.approval.id, changed)


def test_evidence_is_required_authorized_and_retained_as_immutable_version(db):
    world = approval_world(db, evidence=True)
    payload = ChannelReplyIn(text="同意", message_id="evidence-reply", expected_revision=1)
    with pytest.raises(channel_interaction_service.ChannelInteractionError, match="佐证文件"):
        channel_interaction_service.reply_approval(db, world.runtime._actor(), world.approval.id, payload)
    db.rollback()
    version = evidence_version(db, world)
    forged = payload.model_copy(update={"evidence": [EvidenceReference(asset_version_id=version.id, expected_signature="a" * 64)]})
    with pytest.raises(channel_interaction_service.ChannelInteractionError, match="证据"):
        channel_interaction_service.reply_approval(db, world.runtime._actor(), world.approval.id, forged)
    db.rollback()
    accepted = payload.model_copy(update={"evidence": [EvidenceReference(asset_version_id=version.id, expected_signature=version.content_sha256)]})
    result = channel_interaction_service.reply_approval(db, world.runtime._actor(), world.approval.id, accepted)
    assert result["status"] == "approved"
    retained = db.scalar(select(WorkflowApprovalEvidence).where(WorkflowApprovalEvidence.approval_id == world.approval.id))
    assert retained.asset_version_id == version.id
    assert retained.content_signature == version.content_sha256
    assert db.get(WorkflowApprovalRequest, world.approval.id).evidence_refs == [{"asset_version_id": version.id, "expected_signature": version.content_sha256}]


def test_even_another_owner_cannot_reply_as_the_assigned_person(db):
    world = approval_world(db)
    principal = permission_service.require_principal(db)
    role = db.scalar(select(OrganizationRole).where(OrganizationRole.organization_id == principal.organization_id, OrganizationRole.key == "owner"))
    other = User(id="other-approver", tenant_id=world.tenant.id, email="other-approver@example.test", password_hash="synthetic", status="active")
    db.add(other)
    db.flush()
    db.add(OrganizationMember(organization_id=principal.organization_id, user_id=other.id, role_id=role.id))
    db.commit()
    db.info.update(user_id=other.id)
    db.info.pop("permission_cache", None)
    actor = Actor(actor_type="api_key", principal_id="other-channel", tenant_id=world.tenant.id, user_id=other.id)
    with pytest.raises(channel_interaction_service.ChannelInteractionError) as error:
        channel_interaction_service.reply_approval(db, actor, world.approval.id, ChannelReplyIn(text="同意", message_id="forged", expected_revision=1))
    assert error.value.status_code == 404
    assert db.get(WorkflowApprovalRequest, world.approval.id).status == "pending"


def test_message_confirmation_executes_fixed_input_once_and_delivers_running_state(db):
    agent, workflow, message, invocation = _preview(db)
    llm = agent_runtime_adapter.LLMConfig(name="Message confirmation")
    runtime = agent_runtime_adapter.build_runtime_context(db, agent, llm)
    for _ in range(2):
        result = agent_channel_reply_service.handle_message(db, agent, message.conversation_id, "确认", "confirmation-reply", [], runtime)
        db.commit()
        assert result["receipt"]["status"] == "running", result
    assert db.scalar(select(func.count(WorkflowRun.id))) == 1
    operations_service.process_available_runs(db)
    final = capability_application_service.get_receipt(db, runtime._actor(), invocation.id)
    assert final["status"] == "succeeded", final


def test_external_confirmation_uses_same_invoker_and_replays_the_original_request(db):
    agent, workflow, _, _ = _preview(db)
    scenario = db.get(agent_runtime_adapter.BusinessScenario, agent.scenario_id)
    enable_current_release(db, scenario)
    actor = Actor(actor_type="external_api", principal_id="human-channel-key", client_id="human-channel-key", tenant_id=agent.tenant_id, user_id=db.info["user_id"])
    request = Request(capability=CapabilityRef(kind="workflow", resource_id=workflow.id), inputs={"request": "External request"}, mode="preview", idempotency_key="external-preview", correlation_id="external-thread")
    preview = capability_application_service.invoke(db, scenario, actor, request, invocation_source="rest")
    db.commit()
    assert preview.status == "awaiting_confirmation"
    reply = ChannelReplyIn(text="确认", message_id="external-message", expected_revision=1)
    first = channel_interaction_service.reply_confirmation(db, actor, preview.invocation_id, reply)
    db.commit()
    second = channel_interaction_service.reply_confirmation(db, actor, preview.invocation_id, reply)
    db.commit()
    assert first["status"] == second["status"] == "running"
    assert first["workflow_run_id"] == second["workflow_run_id"]
    assert db.scalar(select(func.count(WorkflowRun.id))) == 1


def test_workflow_delivery_does_not_present_model_invented_or_failed_files():
    artifact = {"artifact": {"id": "f" * 32, "filename": "result.txt"}}
    output = {"steps": [{"type": "llm", "status": "success", "result": artifact}, {"type": "action", "status": "failed", "result": artifact}]}
    assert capability_delivery_service._artifacts(output) == []
    output["steps"].append({"type": "action", "status": "success", "result": artifact})
    assert capability_delivery_service._artifacts(output) == [artifact["artifact"]]


def test_sdk_transfers_message_and_downloads_file_without_url_credentials():
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=b"file-content") if request.url.path.endswith("/download") else httpx.Response(200, json={"status": "approved"})
    transport = httpx.Client(transport=httpx.MockTransport(respond))
    client = CapabilityClient("https://example.test/api/external/v2", "synthetic-channel-token", http_client=transport)
    result = client.reply_business_interaction("approval", "approval-1", text="同意", message_id="message-1", expected_revision=1)
    assert result["status"] == "approved"
    assert client.download_invocation_attachment("invocation-1", "file-1") == b"file-content"
    assert all("synthetic-channel-token" not in str(request.url) for request in requests)
    assert json.loads(requests[0].content)["message_id"] == "message-1"
    transport.close()


def test_human_retry_does_not_replace_the_original_conversation_result(db):
    world = approval_world(db)
    operations_service.cancel_run(db, world.run)
    db.refresh(world.approval)
    assert world.approval.status == "cancelled"
    original = capability_application_service.get_receipt(db, world.runtime._actor(), world.receipt["invocation_id"])
    old_key = world.run.execution_key
    operations_service.retry_run(db, world.run)
    assert world.run.execution_key != old_key
    operations_service.process_available_runs(db)
    current = db.scalar(select(WorkflowApprovalRequest).where(WorkflowApprovalRequest.workflow_run_id == world.run.id, WorkflowApprovalRequest.status == "pending"))
    assert current.id != world.approval.id
    after = capability_application_service.get_receipt(db, world.runtime._actor(), world.receipt["invocation_id"])
    assert after["status"] == original["status"] == "cancelled"
    assert after["delivery"] == original["delivery"]
    with pytest.raises(channel_interaction_service.ChannelInteractionError):
        channel_interaction_service.reply_approval(db, world.runtime._actor(), world.approval.id,
            ChannelReplyIn(text="同意", message_id="late-before-retry", expected_revision=1))
    assert current.status == "pending"


def test_expired_confirmation_stops_requesting_a_reply(db):
    from app.services import agent_capability_confirmation_service
    agent, _, message, invocation = _preview(db)
    document = copy.deepcopy(invocation.result_document)
    document["confirmation"]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    invocation.result_document = document
    db.commit()
    result = agent_capability_confirmation_service.get_receipt(db, agent.id, invocation.id, message.id)
    assert result["status"] == "timed_out"
    assert result["can_confirm"] is False
    assert result["delivery"]["interactions"] == []


def test_infrastructure_mode_never_changes_invocation_identity_or_permissions(db, monkeypatch):
    from app.config import get_settings
    from app.models import CapabilityInvocation
    from .test_agent_runtime_adapter import _invoke
    _, _, _, llm, function, agent = _world(db, "infra-independent")
    turn = agent_runtime_adapter.AgentTurnInput(structured_inputs={"amount": 8},
        target_kind="function", target_key=function.id, idempotency_key="same-business-message")
    receipts = []
    for mode in ("dev", "staging", "prod"):
        monkeypatch.setattr(get_settings(), "runtime_environment", mode)
        runtime = agent_runtime_adapter.build_runtime_context(db, agent, llm, turn_input=turn)
        receipts.append(_invoke(runtime, function.id))
        db.commit()
    assert len({item["invocation_id"] for item in receipts}) == 1
    assert len({item["definition_hash"] for item in receipts}) == 1
    assert len({item["deployment_fingerprint"] for item in receipts}) == 1
    assert all(item["status"] == "succeeded" and item["output"]["score"] == 6 for item in receipts)
    assert db.scalar(select(func.count(CapabilityInvocation.id))) == 1


def test_another_authorized_person_can_approve_and_the_requester_receives_the_result(db):
    world = approval_world(db, assigned=False)
    principal = permission_service.require_principal(db)
    role = db.scalar(select(OrganizationRole).where(OrganizationRole.organization_id == principal.organization_id, OrganizationRole.key == "owner"))
    other = User(id="second-reviewer", tenant_id=world.tenant.id, email="reviewer@example.test", password_hash="synthetic", status="active")
    db.add(other)
    db.flush()
    db.add(OrganizationMember(organization_id=principal.organization_id, user_id=other.id, role_id=role.id))
    db.commit()
    db.info["user_id"] = other.id
    db.info.pop("permission_cache", None)
    actor = Actor(actor_type="user", principal_id=other.id, tenant_id=world.tenant.id, user_id=other.id)
    channel_interaction_service.reply_approval(db, actor, world.approval.id,
        ChannelReplyIn(text="同意", message_id="second-person-message", expected_revision=1))
    assert db.get(WorkflowApprovalRequest, world.approval.id).resolved_by_user_id == other.id
    db.info["user_id"] = world.user.id
    db.info.pop("permission_cache", None)
    operations_service.process_available_runs(db)
    result = capability_application_service.get_receipt(db, world.runtime._actor(), world.receipt["invocation_id"])
    assert result["status"] == "succeeded"
