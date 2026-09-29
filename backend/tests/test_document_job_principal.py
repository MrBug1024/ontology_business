from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import document_job_access as access


def test_worker_uses_original_actor_and_restores_caller_even_on_failure(monkeypatch):
    db = SimpleNamespace(info={"tenant_id": "caller-tenant", "user_id": "caller"})
    snapshots = []
    monkeypatch.setattr(access.permission_service, "refresh_request_authorization", lambda db: None)
    monkeypatch.setattr(access.permission_service, "require_principal",
                        lambda db: snapshots.append(dict(db.info)))
    job = SimpleNamespace(tenant_id="job-tenant", requested_by_user_id="job-actor")
    with pytest.raises(RuntimeError):
        with access.execution_principal(db, job):
            assert db.info["user_id"] == "job-actor"
            raise RuntimeError("storage failed")
    assert snapshots == [{"tenant_id": "job-tenant", "user_id": "job-actor"}]
    assert db.info == {"tenant_id": "caller-tenant", "user_id": "caller"}


@pytest.mark.parametrize("actor", [None, "disabled-user"])
def test_missing_or_disabled_actor_never_runs_under_previous_identity(monkeypatch, actor):
    db = SimpleNamespace(info={"tenant_id": "t", "user_id": "owner"})
    monkeypatch.setattr(access.permission_service, "refresh_request_authorization", lambda db: None)
    def denied(db):
        assert db.info["user_id"] == actor
        raise HTTPException(403, "Identity unavailable")
    monkeypatch.setattr(access.permission_service, "require_principal", denied)
    with pytest.raises(HTTPException):
        with access.execution_principal(db, SimpleNamespace(tenant_id="t", requested_by_user_id=actor)):
            pytest.fail("No anonymous or owner fallback")
    assert db.info["user_id"] == "owner"
