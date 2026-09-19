"""Scenario-bound REST/MCP regressions against an isolated real PostgreSQL DB."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from isolated_postgresql import isolated_postgresql, seed_workspace, tenant_session
from app.catalog_schemas import DataAssetCreate
from app.channel_interaction_schemas import ChannelReplyIn
from app.database import get_db
from app.external_api_models import ExternalApiKey, ExternalScenarioAsset
from app.external_api_schemas import ExternalApiKeyCreateIn
from app.models import (
    BusinessScenario, CapabilityInvocation, DataAsset, DataAssetVersion,
    DatasetSchema, DatasetVersion, DatasetVersionAsset, LogicalDataset,
    OntologyWorkflow, WorkflowApprovalRequest, WorkflowRun,
)
from app.routers import channel_interactions, external_api, external_capabilities, external_invocation_artifacts
from app.services import (
    auth_service, capability_mcp_service, catalog_service, external_api_service,
    managed_attachment_access, permission_service, scenario_release_service,
    scenario_purge_plan_service, scenario_purge_service,
)


def test_new_key_requires_explicit_scenario_and_closed_envelope():
    with pytest.raises(ValidationError):
        ExternalApiKeyCreateIn(name="test", scopes=["capabilities:read"])
    with pytest.raises(ValidationError):
        ExternalApiKeyCreateIn(name="test", scenario_id="a", scopes=["capabilities:read"], unrestricted=True)


def test_read_only_runtime_verifier_accepts_migrated_scenario_boundary(boundary):
    from scripts.verify_postgresql_runtime import _verify_external_scenario_contract

    with boundary.isolated.runtime_engine.connect() as connection:
        result = _verify_external_scenario_contract(connection)
    assert result == {"ownership_privileges": "select_insert_delete", "validated_constraints": 4}


@pytest.fixture(scope="module")
def boundary(isolated_postgresql):
    isolated = isolated_postgresql
    workspace = seed_workspace(isolated.admin_engine)
    foreign = seed_workspace(isolated.admin_engine)
    release_ids = {}
    with tenant_session(isolated.runtime_engine, workspace) as db:
        for field in ("scenario_id", "other_scenario_id"):
            release = scenario_release_service.create_release(db, workspace[field], confirmed=True)
            scenario_release_service.change_release(db, workspace[field], release.id,
                                                    expected_revision=release.revision, action="enable")
            release_ids[field] = release.id
        key, token = external_api_service.issue_key(
            db, tenant_id=workspace["tenant_id"], user_id=workspace["user_id"],
            issued_by_user_id=workspace["user_id"], scenario_id=workspace["scenario_id"], name="Scenario A",
            scopes=["scenarios:read", "objects:read", "capabilities:read", "capabilities:invoke", "assets:write"],
            expires_in_days=1,
        )
        db.commit()
        key_id = key.id
    app = FastAPI()
    for router in (external_api.management_router, external_api.router, external_capabilities.router,
                   external_invocation_artifacts.router, channel_interactions.router):
        app.include_router(router, prefix="/api")

    def request_db():
        with Session(isolated.runtime_engine) as db:
            yield db

    def management_db():
        with tenant_session(isolated.runtime_engine, workspace) as db:
            yield db

    app.dependency_overrides[get_db] = request_db
    app.dependency_overrides[auth_service.get_tenant_db] = management_db
    with TestClient(app) as client:
        yield SimpleNamespace(isolated=isolated, workspace=workspace, foreign=foreign,
                              client=client, token=token, key_id=key_id, release_ids=release_ids)


def test_rest_only_discovers_bound_scenario_and_denies_guessed_ids(boundary):
    headers = {"X-API-Key": boundary.token}
    scene = boundary.workspace["scenario_id"]
    for version in ("v1", "v2"):
        response = boundary.client.get(f"/api/external/{version}/scenarios", headers=headers)
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()] == [scene]
    assert boundary.client.get(f"/api/external/v2/scenarios/{scene}/capabilities", headers=headers).status_code == 200
    wrong_release = boundary.client.get(f"/api/external/v2/scenarios/{scene}/capabilities",
        params={"release_id": boundary.release_ids["other_scenario_id"]}, headers=headers)
    assert wrong_release.status_code in {404, 409}
    for other in (boundary.workspace["other_scenario_id"], boundary.foreign["scenario_id"], uuid4().hex):
        endpoints = (
            f"/api/external/v2/scenarios/{other}/capabilities",
            f"/api/external/v2/scenarios/{other}/capabilities/function/guess",
            f"/api/external/v2/scenarios/{other}/capabilities/function/guess/ports/input/managed-input-options",
            f"/api/external/v1/scenarios/{other}/entities",
            f"/api/external/v1/scenarios/{other}/objects",
        )
        for path in endpoints:
            assert boundary.client.get(path, headers=headers).status_code == 404, path
        response = boundary.client.post(f"/api/external/v2/scenarios/{other}/capabilities/function/guess/invoke",
                                        json={"inputs": {}}, headers=headers)
        assert response.status_code == 404, response.text


def test_key_issuance_retains_scene_metadata_and_rejects_foreign_scene(boundary):
    response = boundary.client.post("/api/developer/api-keys", json={
        "name": "Scenario B", "scenario_id": boundary.workspace["other_scenario_id"],
        "scopes": ["capabilities:read"],
    })
    assert response.status_code == 201, response.text
    assert response.json()["scenario_id"] == boundary.workspace["other_scenario_id"]
    assert response.json()["binding_status"] == "bound"
    response = boundary.client.post("/api/developer/api-keys", json={
        "name": "Forbidden", "scenario_id": boundary.foreign["scenario_id"], "scopes": ["capabilities:read"],
    })
    assert response.status_code == 400
    response = boundary.client.post("/api/developer/api-keys", json={"name": "Unbound", "scopes": ["capabilities:read"]})
    assert response.status_code == 422


def test_live_owner_and_public_acl_cannot_widen_scenario(boundary):
    with Session(boundary.isolated.runtime_engine) as db:
        context = external_api_service.authenticate_token(boundary.token, db)
        own = db.get(BusinessScenario, context.scenario_id)
        other = db.get(BusinessScenario, boundary.workspace["other_scenario_id"])
        foreign = db.get(BusinessScenario, boundary.foreign["scenario_id"])
        foreign.is_public = True
        assert permission_service.check_scenario(db, own, "manage").allowed
        for target in (other, foreign):
            assert not permission_service.check_scenario(db, target, "read").allowed
            assert not permission_service._check_resource(db, target, resource_type="workflow",
                                                           resource_id=uuid4().hex, verb="approve").allowed
        db.rollback()


def _historical_invocation(boundary, scenario_id):
    with Session(boundary.isolated.admin_engine) as db:
        row = CapabilityInvocation(
            tenant_id=boundary.workspace["tenant_id"], scenario_id=scenario_id,
            requested_by_user_id=boundary.workspace["user_id"], principal_type="external_api",
            principal_id=boundary.key_id, capability_kind="function", capability_key="legacy",
            invocation_source="rest", request_id=uuid4().hex, correlation_id=uuid4().hex,
            definition_hash="a" * 64, deployment_fingerprint="b" * 64,
            data_context_fingerprint="c" * 64, input_hash="d" * 64, status="awaiting_confirmation",
        )
        db.add(row)
        db.commit()
        return row.id


def test_rest_cannot_read_or_confirm_old_cross_scenario_receipt(boundary):
    invocation_id = _historical_invocation(boundary, boundary.workspace["other_scenario_id"])
    headers = {"X-API-Key": boundary.token}
    receipt = boundary.client.get(f"/api/external/v2/invocations/{invocation_id}", headers=headers)
    assert receipt.status_code == 404
    download = boundary.client.get(f"/api/external/v2/invocations/{invocation_id}/attachments/{uuid4().hex}/download", headers=headers)
    assert download.status_code == 404
    reply = boundary.client.post(f"/api/external/v2/interactions/confirmation/{invocation_id}/reply", headers=headers,
                                  json={"text": "取消", "message_id": uuid4().hex, "expected_revision": 1})
    assert reply.status_code == 404, reply.text
    with Session(boundary.isolated.admin_engine) as db:
        assert db.get(CapabilityInvocation, invocation_id).status == "awaiting_confirmation"


def _other_approval(boundary):
    with Session(boundary.isolated.admin_engine) as db:
        workflow = OntologyWorkflow(scenario_id=boundary.workspace["other_scenario_id"], name="Other approval")
        db.add(workflow)
        db.flush()
        run = WorkflowRun(scenario_id=workflow.scenario_id, workflow_id=workflow.id, status="awaiting_approval")
        db.add(run)
        db.flush()
        approval = WorkflowApprovalRequest(workflow_run_id=run.id, scenario_id=workflow.scenario_id, node_id="approve")
        db.add(approval)
        db.commit()
        return approval.id


def test_rest_and_mcp_reject_other_scene_approval_without_mutation(boundary, monkeypatch):
    approval_id = _other_approval(boundary)
    headers = {"X-API-Key": boundary.token}
    read = boundary.client.get(f"/api/external/v2/interactions/approval/{approval_id}", headers=headers)
    assert read.status_code == 404
    reply = boundary.client.post(f"/api/external/v2/interactions/approval/{approval_id}/reply", headers=headers,
                                  json={"text": "同意", "message_id": uuid4().hex, "expected_revision": 1})
    assert reply.status_code == 404, reply.text
    monkeypatch.setattr(capability_mcp_service, "SessionLocal", lambda: Session(boundary.isolated.runtime_engine))
    auth = capability_mcp_service.authenticate_token(boundary.token)
    assert auth is not None
    with pytest.raises(capability_mcp_service.CapabilityMCPError):
        capability_mcp_service.read_approval(auth, interaction_id=approval_id)
    with pytest.raises(capability_mcp_service.CapabilityMCPError):
        capability_mcp_service.reply_interaction(auth, kind="approval", interaction_id=approval_id,
            reply=ChannelReplyIn(text="同意", message_id=uuid4().hex, expected_revision=1))
    with Session(boundary.isolated.admin_engine) as db:
        assert db.get(WorkflowApprovalRequest, approval_id).status == "pending"


def test_mcp_tools_observe_binding_and_revocation_after_authentication(boundary, monkeypatch):
    monkeypatch.setattr(capability_mcp_service, "SessionLocal", lambda: Session(boundary.isolated.runtime_engine))
    auth = capability_mcp_service.authenticate_token(boundary.token)
    assert auth is not None and auth.scenario_id == boundary.workspace["scenario_id"]
    assert capability_mcp_service.list_capabilities(auth, scenario_id=auth.scenario_id) == []
    for other in (boundary.workspace["other_scenario_id"], boundary.foreign["scenario_id"]):
        with pytest.raises(capability_mcp_service.CapabilityMCPError):
            capability_mcp_service.list_capabilities(auth, scenario_id=other)
        with pytest.raises(capability_mcp_service.CapabilityMCPError):
            capability_mcp_service.invoke_capability(auth, scenario_id=other, capability_kind="function", capability_key="guess")
    invocation_id = _historical_invocation(boundary, boundary.workspace["other_scenario_id"])
    with pytest.raises(capability_mcp_service.CapabilityMCPError):
        capability_mcp_service.get_receipt(auth, invocation_id=invocation_id)
    with tenant_session(boundary.isolated.runtime_engine, boundary.workspace) as db:
        key, token = external_api_service.issue_key(db, tenant_id=auth.tenant_id, user_id=auth.user_id,
            issued_by_user_id=auth.user_id, scenario_id=auth.scenario_id, name="Revocation",
            scopes=["capabilities:read"], expires_in_days=1)
        db.commit()
        new_auth = capability_mcp_service.authenticate_token(token)
        external_api_service.revoke_key(db, tenant_id=auth.tenant_id, key_id=key.id, revoked_by_user_id=auth.user_id)
        db.commit()
    with pytest.raises(capability_mcp_service.CapabilityMCPError, match="credential"):
        capability_mcp_service.list_capabilities(new_auth, scenario_id=auth.scenario_id)


def test_external_upload_ownership_is_persisted_and_cross_scene_inputs_fail(boundary):
    workspace = boundary.workspace
    with tenant_session(boundary.isolated.runtime_engine, workspace) as db:
        db.info["external_scenario_id"] = workspace["other_scenario_id"]
        asset = catalog_service.create_asset(db, DataAssetCreate(key="test." + uuid4().hex, name="Attachment",
            kind="file", usage_plane="invocation_input", labels={"catalog_purpose": "invocation_attachment"}))
        db.commit()
        asset_id = asset.id
        assert db.get(ExternalScenarioAsset, asset.id).scenario_id == workspace["other_scenario_id"]
    with Session(boundary.isolated.runtime_engine) as db:
        external_api_service.authenticate_token(boundary.token, db)
        asset = db.get(DataAsset, asset_id)
        version = DataAssetVersion(id=uuid4().hex, asset_id=asset.id, tenant_id=asset.tenant_id,
            bucket_file_id=None, bucket_data_source_id=None, version_document={})
        with pytest.raises(managed_attachment_access.AttachmentAccessError, match="业务场景"):
            managed_attachment_access.require_asset_version_scope(db, asset, version,
                tenant_id=asset.tenant_id, agent_id=None, scenario_id=workspace["scenario_id"])
        db.info["external_scenario_id"] = workspace["other_scenario_id"]
        managed_attachment_access.require_asset_version_scope(db, asset, version,
            tenant_id=asset.tenant_id, agent_id=None, scenario_id=workspace["other_scenario_id"])
        legacy = DataAsset(id=uuid4().hex, tenant_id=workspace["tenant_id"], owner_agent_id=None, labels={})
        legacy_version = DataAssetVersion(id=uuid4().hex, asset_id=legacy.id, tenant_id=legacy.tenant_id,
            version_document={"lifecycle": {"purpose": "invocation_attachment"}})
        with pytest.raises(managed_attachment_access.AttachmentAccessError, match="业务场景"):
            managed_attachment_access.require_asset_version_scope(db, legacy, legacy_version,
                tenant_id=legacy.tenant_id, agent_id=None, scenario_id=workspace["other_scenario_id"])


def test_legacy_unbound_key_is_visible_as_requiring_reissuance_and_cannot_authenticate(boundary):
    token = "ont_sk_" + uuid4().hex
    with Session(boundary.isolated.admin_engine) as db:
        key = ExternalApiKey(tenant_id=boundary.workspace["tenant_id"], user_id=boundary.workspace["user_id"],
            name="Legacy", token_hash=external_api_service.token_hash(token), key_prefix="synthetic",
            token_hint="test", status="revoked", scenario_id=None, scopes=["capabilities:read"],
            expires_at=datetime.now(timezone.utc) + timedelta(days=1))
        db.add(key)
        db.commit()
        key_id = key.id
    assert boundary.client.get("/api/external/v2/scenarios", headers={"X-API-Key": token}).status_code == 401
    response = boundary.client.get("/api/developer/api-keys")
    assert response.status_code == 200
    metadata = next(item for item in response.json() if item["id"] == key_id)
    assert metadata["binding_status"] == "reissue_required"
    assert "token" not in metadata and "token_hash" not in metadata


def test_dataset_alias_preserves_external_attachment_scenario_ownership(boundary):
    workspace = boundary.workspace
    with tenant_session(boundary.isolated.admin_engine, workspace) as db:
        db.info["external_scenario_id"] = workspace["other_scenario_id"]
        asset = catalog_service.create_asset(db, DataAssetCreate(key="test." + uuid4().hex, name="Private attachment",
            kind="file", usage_plane="invocation_input", labels={"catalog_purpose": "invocation_attachment"}))
        asset_version = DataAssetVersion(asset_id=asset.id, tenant_id=asset.tenant_id, version_number=1,
            status="ready", content_sha256="d" * 64)
        dataset = LogicalDataset(tenant_id=asset.tenant_id, key="test." + uuid4().hex,
                                 name="Alias", usage_plane="invocation_input")
        db.add_all([asset_version, dataset])
        db.flush()
        schema = DatasetSchema(tenant_id=asset.tenant_id, dataset_id=dataset.id, schema_version=1,
                               schema_hash="e" * 64)
        db.add(schema)
        db.flush()
        version = DatasetVersion(tenant_id=asset.tenant_id, dataset_id=dataset.id, schema_id=schema.id,
                                 version_number=1, content_hash="f" * 64, status="assembling")
        db.add(version)
        db.flush()
        db.add(DatasetVersionAsset(tenant_id=asset.tenant_id, dataset_id=dataset.id,
            dataset_version_id=version.id, asset_version_id=asset_version.id))
        db.commit()
        version_id, dataset_id = version.id, dataset.id
    with Session(boundary.isolated.runtime_engine) as db:
        external_api_service.authenticate_token(boundary.token, db)
        dataset, version = db.get(LogicalDataset, dataset_id), db.get(DatasetVersion, version_id)
        with pytest.raises(managed_attachment_access.AttachmentAccessError, match="业务场景"):
            managed_attachment_access._check_dataset_scope(db, dataset, version,
                tenant_id=workspace["tenant_id"], agent_id=None, scenario_id=workspace["scenario_id"])
        db.info["external_scenario_id"] = workspace["other_scenario_id"]
        managed_attachment_access._check_dataset_scope(db, dataset, version,
            tenant_id=workspace["tenant_id"], agent_id=None, scenario_id=workspace["other_scenario_id"])


def test_scenario_purge_requires_key_audit_consent_and_preserves_attachment_ownership(boundary):
    workspace = seed_workspace(boundary.isolated.admin_engine)
    with tenant_session(boundary.isolated.runtime_engine, workspace) as db:
        key, _ = external_api_service.issue_key(db, tenant_id=workspace["tenant_id"],
            user_id=workspace["user_id"], issued_by_user_id=workspace["user_id"], scenario_id=workspace["scenario_id"],
            name="Purge", scopes=["capabilities:read"], expires_in_days=1)
        scenario = db.get(BusinessScenario, workspace["scenario_id"])
        scenario.status = "retired"
        db.commit()
        plan = scenario_purge_plan_service.build_purge_plan(db, scenario)
        assert plan.counts["external_api_keys"] == 1 and plan.requires_audit_confirmation
        assert plan.can_purge
        key_id = key.id
        with pytest.raises(scenario_purge_service.ScenarioPurgeConflict, match="审计"):
            scenario_purge_service.prepare_scenario_purge(db, scenario_id=scenario.id,
                tenant_id=workspace["tenant_id"], expected_name=scenario.name, confirmed=True, delete_audit_history=False)
        db.rollback()
        scenario_purge_service.prepare_scenario_purge(db, scenario_id=scenario.id,
            tenant_id=workspace["tenant_id"], expected_name=scenario.name, confirmed=True, delete_audit_history=True)
        db.commit()
        assert db.get(ExternalApiKey, key_id) is None
        other = db.get(BusinessScenario, workspace["other_scenario_id"])
        db.info["external_scenario_id"] = other.id
        catalog_service.create_asset(db, DataAssetCreate(key="test." + uuid4().hex, name="Retained attachment",
            kind="file", usage_plane="invocation_input", labels={"catalog_purpose": "invocation_attachment"}))
        other.status = "retired"
        db.commit()
        plan = scenario_purge_plan_service.build_purge_plan(db, other)
        assert not plan.can_purge and plan.retained["external_scenario_assets"] == 1
        with pytest.raises(scenario_purge_service.ScenarioPurgeConflict, match="附件"):
            scenario_purge_service.prepare_scenario_purge(db, scenario_id=other.id,
                tenant_id=workspace["tenant_id"], expected_name=other.name, confirmed=True, delete_audit_history=True)
