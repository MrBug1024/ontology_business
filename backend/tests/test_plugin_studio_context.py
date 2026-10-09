from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import scenario_releases as releases


def test_studio_release_read_requires_tenant_permission_before_resource_lookup(monkeypatch):
    monkeypatch.setattr(releases.permission_service, 'require_principal', lambda db: SimpleNamespace(tenant_id='tenant'))
    def denied(*args):
        raise HTTPException(403, '无权访问')
    monkeypatch.setattr(releases.permission_service, 'require_tenant_permission', denied)
    with pytest.raises(HTTPException) as error:
        releases.get_release('release', SimpleNamespace())
    assert error.value.status_code == 403


def test_studio_release_read_enforces_scenario_acl_before_serializing(monkeypatch):
    monkeypatch.setattr(releases.permission_service, 'require_principal', lambda db: SimpleNamespace(tenant_id='tenant'))
    monkeypatch.setattr(releases.permission_service, 'require_tenant_permission', lambda *args: None)
    def denied(*args):
        raise HTTPException(404, '资源不可用')
    monkeypatch.setattr(releases.release_service, '_scenario_for_read', denied)
    db = SimpleNamespace(scalar=lambda statement: SimpleNamespace(scenario_id='scenario'))
    with pytest.raises(HTTPException) as error:
        releases.get_release('release', db)
    assert error.value.status_code == 404
