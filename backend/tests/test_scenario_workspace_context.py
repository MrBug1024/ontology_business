"""Workspace-private scenario context must never follow public visibility."""
from types import SimpleNamespace

import pytest

from app.routers import scenarios
from app.services import permission_service, tenant_service


@pytest.mark.parametrize(
    ("scenario_tenant", "allowed", "expected"),
    [
        ("tenant-a", True, True),
        ("tenant-a", False, False),
        ("tenant-b", True, False),
    ],
)
def test_workspace_context_requires_same_tenant_and_scenario_read(
    monkeypatch,
    scenario_tenant,
    allowed,
    expected,
):
    checked = []
    monkeypatch.setattr(tenant_service, "current_tenant_id", lambda _db: "tenant-a")

    def check(_db, scenario, verb):
        checked.append((scenario.tenant_id, verb))
        return SimpleNamespace(allowed=allowed)

    monkeypatch.setattr(permission_service, "check_scenario", check)
    scenario = SimpleNamespace(tenant_id=scenario_tenant)

    assert scenarios._can_read_workspace_context(SimpleNamespace(), scenario) is expected
    assert checked == ([((scenario_tenant), "read")] if scenario_tenant == "tenant-a" else [])


@pytest.mark.parametrize(
    ("scenario_tenant", "status", "allowed", "expected"),
    [
        ("tenant-a", "active", True, True),
        ("tenant-a", "active", False, False),
        ("tenant-a", "retired", True, False),
        ("tenant-b", "active", True, False),
    ],
)
def test_scenario_source_errors_require_editable_same_tenant_context(
    monkeypatch,
    scenario_tenant,
    status,
    allowed,
    expected,
):
    monkeypatch.setattr(tenant_service, "current_tenant_id", lambda _db: "tenant-a")
    monkeypatch.setattr(
        permission_service,
        "check_scenario",
        lambda _db, _scenario, verb: SimpleNamespace(allowed=allowed),
    )
    scenario = SimpleNamespace(tenant_id=scenario_tenant, status=status)
    source = SimpleNamespace(
        tenant_id=scenario_tenant,
        last_error="postgres://private-host:5432/password-leaked",
    )
    can_write = scenarios._can_write_workspace_context(SimpleNamespace(), scenario)

    assert can_write is expected
    assert scenarios._scenario_source_last_error(
        SimpleNamespace(),
        source,
        can_write_workspace=can_write,
    ) == (source.last_error if expected else "")
