from __future__ import annotations

import base64
import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.distillation_access_schemas import SystemAccessRequest
from app.distillation_target_schemas import TargetSystem
from app.routers import distillation_access
from app.services import distillation_access_crypto as crypto, distillation_target_service as targets, readonly_http_adapter
from app.services.distillation_conversation_tools import observations_from_steps


def test_grant_validation_never_echoes_request_credentials():
    app = FastAPI()
    app.include_router(distillation_access.router)
    app.dependency_overrides[distillation_access.get_tenant_db] = lambda: None
    value = secrets.token_urlsafe(30)
    response = TestClient(app).post('/business-distillation/project/targets/system/authorize', json={
        "secret": value, "auth_type": "invalid", "password": value})
    assert response.status_code == 422
    assert value not in response.text and 'input' not in response.text


def test_ciphertext_is_bound_to_workspace_project_grant_target_and_auth(monkeypatch):
    monkeypatch.setattr(crypto, "load_keyring", lambda: SimpleNamespace(active_key_id="ephemeral", keys={"ephemeral": b"x" * 32}))
    row = SimpleNamespace(tenant_id="tenant", project_id="project", id="grant", target_hash="a" * 64, auth_type="basic")
    secret = secrets.token_urlsafe(30)
    row.credential_envelope = crypto.seal(row, "research", secret)
    assert secret not in str(row.credential_envelope)
    assert base64.b64decode(crypto.authorization_header(row).split()[1]).decode() == "research:" + secret
    for field in ("tenant_id", "project_id", "id", "target_hash", "auth_type"):
        original = getattr(row, field)
        setattr(row, field, "tampered")
        with pytest.raises(ValueError, match="凭据不可用"):
            crypto.authorization_header(row)
        setattr(row, field, original)


def test_authorized_read_requires_credentials_redacts_echo_and_keeps_real_receipt(monkeypatch):
    target = TargetSystem(key="system", name="系统", base_url="https://example.com", purpose="调查", allowed_paths=["/history"], access_mode="authorized_readonly")
    with pytest.raises(targets.TargetReadError, match="有效只读授权"):
        targets.read_target_system(target, "/history")
    secret = secrets.token_urlsafe(30)
    seen = []

    def read(url, *, authorization):
        seen.append((url, authorization))
        return readonly_http_adapter.ReadonlyDocument(200, "application/json", ('{"stage":"done","echo":"' + secret + '"}').encode())

    monkeypatch.setattr(readonly_http_adapter, "read_document", read)
    result = targets.read_target_system(target, "/history", authorization="Bearer " + secret)
    assert seen == [("https://example.com/history", "Bearer " + secret)]
    assert result["status"] == "observed" and 'done' in result["text"]
    assert secret not in str(result)
    assert len(result["content_sha256"]) == 64
    assert "专用凭据" in result["limitations"][0]


def test_login_page_is_not_business_process_evidence():
    assert not observations_from_steps("turn", [{"id": "step", "status": "succeeded", "source": {"status": "login_required"}}])
