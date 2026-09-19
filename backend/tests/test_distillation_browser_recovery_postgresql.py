"""Interrupted authentication must not be replayed by a replacement worker."""
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.distillation_conversation_models import DistillationConversationTurn as Turn
from app.services import distillation_conversation_lease as leases
from app.services import distillation_conversation_worker as worker
from isolated_postgresql import isolated_postgresql
from test_distillation_conversation_postgresql import enqueue, setup_project


def test_expired_login_is_marked_unknown_without_reclaim_or_replay(isolated_postgresql):
    isolated = isolated_postgresql
    workspace, project_id, factory = setup_project(isolated)
    turn_id = enqueue(isolated, workspace, project_id)
    with factory() as db:
        original = leases.claim(db)
        db.commit()
    with Session(isolated.admin_engine) as db:
        row = db.get(Turn, turn_id)
        row.steps = [{"id": "interrupted_login", "tool_name": "login_business_system", "status": "running"}]
        row.lease_expires_at = leases.now() - timedelta(seconds=1)
        db.commit()
    with factory() as db:
        assert leases.claim(db) is None
        db.commit()
        row = db.get(Turn, turn_id)
        assert row.status == "failed" and "结果未知" in row.error
        assert row.steps[0]["status"] == "failed"
        assert row.attempt == 1 and row.lease_token is None
    with pytest.raises(leases.LeaseLost):
        worker._finish(original, factory, "succeeded", message="Late login result")
