"""scope capability ports to one runtime capability

Revision ID: 20260829_10
Revises: 20260829_09
Create Date: 2026-08-29

Existing rows are backfilled only from explicit ``config.contract_source``
evidence. A semantic source key may resolve through one already-promoted draft
resource, but names, port prefixes and single-capability scenarios are never
used as ownership guesses.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260829_10"
down_revision: Union[str, None] = "20260829_09"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TARGET_TABLES = {
    "function": "function_definitions",
    "action": "ontology_actions",
    "workflow": "ontology_workflows",
}


def _object(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, Mapping) else {}
    return {}


def _contract_source(config: Any) -> tuple[str, str] | None:
    source = _object(_object(config).get("contract_source"))
    kind = str(source.get("resource_kind") or "").strip().lower()
    key = str(source.get("resource_key") or "").strip()
    if kind not in _TARGET_TABLES or not key or len(key) > 240:
        return None
    return kind, key


def _target_exists(
    bind: sa.engine.Connection,
    *,
    kind: str,
    key: str,
    scenario_id: str,
) -> bool:
    table = _TARGET_TABLES[kind]
    row = bind.execute(
        sa.text(
            f"SELECT id FROM {table} "
            "WHERE id = :key AND scenario_id = :scenario_id"
        ),
        {"key": key, "scenario_id": scenario_id},
    ).first()
    return row is not None


def _resolved_draft_targets(
    bind: sa.engine.Connection,
    *,
    kind: str,
    source_key: str,
    scenario_id: str,
) -> tuple[str, ...]:
    rows = bind.execute(
        sa.text(
            "SELECT DISTINCT resolved_resource_id "
            "FROM scenario_model_draft_resources "
            "WHERE scenario_id = :scenario_id "
            "AND resource_kind = :kind "
            "AND resource_key = :source_key "
            "AND draft_status = 'resolved' "
            "AND resolved_resource_id IS NOT NULL "
            "AND resolved_resource_id <> ''"
        ),
        {
            "scenario_id": scenario_id,
            "kind": kind,
            "source_key": source_key,
        },
    ).scalars().all()
    return tuple(sorted({str(value) for value in rows if str(value or "").strip()}))


def _resolve_owner(
    bind: sa.engine.Connection,
    *,
    scenario_id: str,
    config: Any,
) -> tuple[str, str]:
    source = _contract_source(config)
    if source is None:
        raise ValueError("missing or invalid config.contract_source")
    kind, source_key = source
    candidates: set[str] = set()
    if _target_exists(
        bind,
        kind=kind,
        key=source_key,
        scenario_id=scenario_id,
    ):
        candidates.add(source_key)
    for candidate in _resolved_draft_targets(
        bind,
        kind=kind,
        source_key=source_key,
        scenario_id=scenario_id,
    ):
        if _target_exists(
            bind,
            kind=kind,
            key=candidate,
            scenario_id=scenario_id,
        ):
            candidates.add(candidate)
    if len(candidates) != 1:
        detail = "no governed target" if not candidates else "ambiguous governed targets"
        raise ValueError(detail)
    return kind, next(iter(candidates))


def _backfill_ownership(bind: sa.engine.Connection) -> None:
    rows = bind.execute(
        sa.text(
            "SELECT id, scenario_id, config "
            "FROM scenario_capability_ports ORDER BY id"
        )
    ).mappings().all()
    unresolved: list[str] = []
    resolved: list[tuple[str, str, str]] = []
    for row in rows:
        port_id = str(row["id"])
        try:
            kind, key = _resolve_owner(
                bind,
                scenario_id=str(row["scenario_id"]),
                config=row["config"],
            )
        except ValueError as exc:
            unresolved.append(f"{port_id} ({exc})")
            continue
        resolved.append((port_id, kind, key))
    if unresolved:
        preview = ", ".join(unresolved[:20])
        remaining = len(unresolved) - min(len(unresolved), 20)
        suffix = f"; and {remaining} more" if remaining else ""
        raise RuntimeError(
            "capability port ownership backfill is not deterministic: "
            f"{preview}{suffix}. Add explicit config.contract_source evidence "
            "and retry the migration."
        )
    for port_id, kind, key in resolved:
        bind.execute(
            sa.text(
                "UPDATE scenario_capability_ports "
                "SET capability_kind = :kind, capability_key = :key "
                "WHERE id = :port_id"
            ),
            {"kind": kind, "key": key, "port_id": port_id},
        )


def upgrade() -> None:
    op.add_column(
        "scenario_capability_ports",
        sa.Column("capability_kind", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "scenario_capability_ports",
        sa.Column("capability_key", sa.String(length=240), nullable=True),
    )
    _backfill_ownership(op.get_bind())
    op.alter_column(
        "scenario_capability_ports",
        "capability_kind",
        existing_type=sa.String(length=40),
        nullable=False,
    )
    op.alter_column(
        "scenario_capability_ports",
        "capability_key",
        existing_type=sa.String(length=240),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_scenario_capability_ports_capability_kind",
        "scenario_capability_ports",
        "capability_kind IN ('function', 'action', 'workflow')",
    )
    op.drop_constraint(
        "uq_scenario_capability_ports_key",
        "scenario_capability_ports",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_scenario_capability_ports_owner_key",
        "scenario_capability_ports",
        ["scenario_id", "capability_kind", "capability_key", "port_key"],
    )
    op.drop_index(
        "ix_scenario_capability_ports_scenario_role",
        table_name="scenario_capability_ports",
    )
    op.create_index(
        "ix_scenario_capability_ports_owner_role",
        "scenario_capability_ports",
        [
            "scenario_id",
            "capability_kind",
            "capability_key",
            "direction",
            "role",
            "status",
        ],
    )


def downgrade() -> None:
    bind = op.get_bind()
    duplicates = bind.execute(
        sa.text(
            "SELECT scenario_id, port_key, COUNT(*) AS row_count "
            "FROM scenario_capability_ports "
            "GROUP BY scenario_id, port_key HAVING COUNT(*) > 1 "
            "ORDER BY scenario_id, port_key"
        )
    ).mappings().all()
    if duplicates:
        preview = ", ".join(
            f"{row['scenario_id']}:{row['port_key']} ({row['row_count']})"
            for row in duplicates[:20]
        )
        raise RuntimeError(
            "cannot downgrade capability port ownership without collapsing "
            f"distinct capability contracts: {preview}"
        )
    op.drop_index(
        "ix_scenario_capability_ports_owner_role",
        table_name="scenario_capability_ports",
    )
    op.create_index(
        "ix_scenario_capability_ports_scenario_role",
        "scenario_capability_ports",
        ["scenario_id", "direction", "role", "status"],
    )
    op.drop_constraint(
        "uq_scenario_capability_ports_owner_key",
        "scenario_capability_ports",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_scenario_capability_ports_key",
        "scenario_capability_ports",
        ["scenario_id", "port_key"],
    )
    op.drop_constraint(
        "ck_scenario_capability_ports_capability_kind",
        "scenario_capability_ports",
        type_="check",
    )
    op.drop_column("scenario_capability_ports", "capability_key")
    op.drop_column("scenario_capability_ports", "capability_kind")
