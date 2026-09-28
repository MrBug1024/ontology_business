from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models import User
from app.services import external_api_service


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return self

    def first(self):
        return self.value


class KeyDb:
    def __init__(self, key):
        self.key = key
        self.added = []

    def execute(self, _statement):
        return ScalarResult(self.key)

    def get(self, model, _identifier):
        return SimpleNamespace(status="active") if model is User else None

    def add(self, value):
        self.added.append(value)


def _key(*, scenario_id, status="revoked"):
    return SimpleNamespace(
        id="key-id",
        tenant_id="tenant-id",
        user_id="subject-id",
        scenario_id=scenario_id,
        status=status,
        revoked_at=None,
        revoked_by_user_id=None,
        deleted_at=None,
    )


def test_unbound_revoked_key_is_hidden_with_an_audit_event(monkeypatch):
    monkeypatch.setattr(external_api_service, "_active_member", lambda *_args: True)
    key = _key(scenario_id=None)
    db = KeyDb(key)

    removed, action = external_api_service.remove_key(
        db,
        tenant_id="tenant-id",
        key_id=key.id,
        revoked_by_user_id="admin-id",
    )

    assert removed is key
    assert action == "deleted"
    assert key.deleted_at is not None
    assert db.added[0].event_type == "deleted"
    assert db.added[0].details["reason"] == "unbound_scenario_key_removed"
    assert db.added[0].details["key_id"] == key.id


def test_bound_key_is_revoked_and_keeps_its_binding(monkeypatch):
    monkeypatch.setattr(external_api_service, "_active_member", lambda *_args: True)
    key = _key(scenario_id="scenario-id", status="active")
    db = KeyDb(key)

    removed, action = external_api_service.remove_key(
        db,
        tenant_id="tenant-id",
        key_id=key.id,
        revoked_by_user_id="admin-id",
    )

    assert removed is key
    assert action == "revoked"
    assert key.status == "revoked"
    assert key.deleted_at is None
    assert db.added[0].event_type == "revoked"


def test_active_unbound_key_is_revoked_then_removed_with_audit(monkeypatch):
    monkeypatch.setattr(external_api_service, "_active_member", lambda *_args: True)
    key = _key(scenario_id=None, status="active")
    db = KeyDb(key)

    removed, action = external_api_service.remove_key(
        db,
        tenant_id="tenant-id",
        key_id=key.id,
        revoked_by_user_id="admin-id",
    )

    assert removed is key
    assert action == "deleted"
    assert key.status == "revoked"
    assert key.revoked_by_user_id == "admin-id"
    assert key.revoked_at is not None
    assert key.deleted_at is not None
    assert [event.event_type for event in db.added] == ["revoked", "deleted"]
