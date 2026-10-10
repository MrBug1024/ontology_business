"""场景退役对能力版本的级联治理与永久删除阻塞项。"""
from types import SimpleNamespace

import pytest

from app.services import release_service, scenario_purge_plan_service


class _ScalarResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)


class _CascadeDb:
    """记录级联写入并返回既定的未退役发布行。"""

    def __init__(self, releases):
        self.releases = releases
        self.added: list[object] = []
        self.statement = None

    def execute(self, statement):
        self.statement = statement
        return _ScalarResult(self.releases)

    def add(self, obj):
        self.added.append(obj)


def _release(*, status="released", enabled=True, revision=3, deleted=False):
    return SimpleNamespace(
        id="release-id",
        tenant_id="tenant",
        scenario_id="scenario",
        snapshot_id="snapshot",
        name="库存盘点能力版本",
        status=status,
        enabled=enabled,
        revision=revision,
        retired_at=None,
        deleted_at="x" if deleted else None,
    )


def test_scenario_retirement_cascades_to_enabled_and_disabled_releases():
    enabled = _release(enabled=True)
    disabled_not_retired = _release(enabled=False, revision=7)
    db = _CascadeDb([enabled, disabled_not_retired])
    scenario = SimpleNamespace(id="scenario", tenant_id="tenant")

    retired = release_service.retire_scenario_releases(
        db, scenario, actor_id="user-1"
    )

    assert retired == [enabled, disabled_not_retired]
    for release in (enabled, disabled_not_retired):
        assert release.status == "retired"
        assert release.enabled is False
        assert release.retired_at is not None
        assert release.revision == (4 if release is enabled else 8)
    events = [obj for obj in db.added if type(obj).__name__ == "ReleaseLifecycleEvent"]
    assert len(events) == 2
    assert {event.action for event in events} == {"scenario_retire"}
    assert {event.actor_id for event in events} == {"user-1"}
    assert {event.revision for event in events} == {4, 8}
    # 锁定范围仍排除已退役/已删除的历史行。
    assert "deleted_at" in str(db.statement)


def test_scenario_retirement_cascade_is_noop_without_live_releases():
    db = _CascadeDb([])
    scenario = SimpleNamespace(id="scenario", tenant_id="tenant")

    assert release_service.retire_scenario_releases(db, scenario, actor_id=None) == []
    assert db.added == []


def test_purge_plan_blocks_live_plugin_installation_sources(monkeypatch):
    scenario = SimpleNamespace(id="scenario", tenant_id="tenant", status="retired")
    monkeypatch.setattr(
        scenario_purge_plan_service,
        "scenario_tenant_mismatch_exists",
        lambda db, value: False,
    )
    monkeypatch.setattr(
        scenario_purge_plan_service.scenario_purge_asset_service,
        "inspect_scenario_sources",
        lambda db, value: SimpleNamespace(blockers=()),
    )
    blockers = scenario_purge_plan_service._plan_blockers(
        SimpleNamespace(),
        scenario,
        SimpleNamespace(),
        published_plugin_count=1,
    )

    assert blockers == ("场景仍有已发布插件，请先撤回插件安装源后再永久删除",)
