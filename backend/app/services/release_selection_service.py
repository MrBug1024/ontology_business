"""Select a manually enabled immutable release independently of deployment mode."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BusinessScenario, OntologyRelease


class ReleaseSelectionError(ValueError):
    pass


def require_enabled_release(
    db: Session, scenario: BusinessScenario, release_id: str | None = None,
) -> OntologyRelease:
    statement = select(OntologyRelease).where(
        OntologyRelease.tenant_id == scenario.tenant_id,
        OntologyRelease.scenario_id == scenario.id,
        OntologyRelease.status == "released",
        OntologyRelease.enabled.is_(True),
        OntologyRelease.deleted_at.is_(None),
    )
    if release_id:
        statement = statement.where(OntologyRelease.id == release_id)
    releases = list(db.scalars(statement.limit(2)))
    if not releases:
        raise ReleaseSelectionError("场景没有可用的已启用发布，请先人工创建并启用发布")
    if len(releases) != 1:
        raise ReleaseSelectionError("场景有多个已启用发布，请明确选择发布版本")
    return releases[0]
