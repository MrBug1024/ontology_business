"""Explicit human release commands for protocol fixtures."""
from __future__ import annotations

from app.services import scenario_release_service


def enable_current_release(db, scenario):
    release = scenario_release_service.create_release(db, scenario.id, confirmed=True)
    return scenario_release_service.change_release(db, scenario.id, release.id,
        expected_revision=release.revision, action="enable")
