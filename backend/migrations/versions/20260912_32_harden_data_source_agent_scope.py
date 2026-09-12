"""Harden Agent runtime DataSource ownership and tenant isolation.

Revision ID: 20260912_32
Revises: 20260911_31

The previous runtime-connector migration used a single-column ``agents.id``
foreign key with ``CASCADE``.  That allowed a cross-tenant owner reference and
made deleting an Agent capable of deleting a connector row.  This revision
closes both gaps without rewriting existing rows: owner-bearing rows must be
runtime-scoped and reference the same ``(id, tenant_id)`` Agent, and Agent
deletion is RESTRICTed until the application has explicitly detached the
connector.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260912_32"
down_revision: Union[str, None] = "20260911_31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OLD_OWNER_FK = "fk_data_sources_owner_agent"
_OWNER_FK = "fk_data_sources_owner_agent_tenant"
_RESOURCE_CHECK = "ck_data_sources_resource_scope"
_OWNER_CHECK = "ck_data_sources_owner_scope"


def _lock_for_scope_validation(bind: sa.engine.Connection) -> None:
    """Serialize owner/Agent changes while the FK is being replaced."""

    if bind.dialect.name == "postgresql":
        op.execute(
            "LOCK TABLE data_sources, agents IN SHARE ROW EXCLUSIVE MODE"
        )


def _precheck_existing_scope(bind: sa.engine.Connection) -> None:
    """Reject ambiguous legacy rows before dropping the old constraint.

    No owner is guessed or reassigned here.  A deployment operator must fix a
    reported row explicitly, then rerun the migration.  The query also
    protects against a NULL tenant, which PostgreSQL's default MATCH SIMPLE
    composite-FK semantics would otherwise permit for a non-NULL owner.
    """

    invalid = bind.execute(
        sa.text(
            """
            SELECT source.id
              FROM data_sources AS source
             WHERE source.resource_scope IS NULL
                OR source.resource_scope NOT IN ('modeling', 'agent_runtime')
                OR (
                    source.owner_agent_id IS NOT NULL
                    AND (
                        source.tenant_id IS NULL
                        OR source.resource_scope <> 'agent_runtime'
                        OR NOT EXISTS (
                            SELECT 1
                              FROM agents AS agent
                             WHERE agent.id = source.owner_agent_id
                               AND agent.tenant_id = source.tenant_id
                        )
                    )
                )
             ORDER BY source.id
             LIMIT 1
            """
        )
    ).first()
    if invalid is not None:
        identifier = str(invalid[0])
        raise RuntimeError(
            "20260912_32 data_sources ownership precheck failed for id="
            f"{identifier}; set an explicit same-tenant Agent owner or clear "
            "owner_agent_id before retrying"
        )


def upgrade() -> None:
    bind = op.get_bind()
    _lock_for_scope_validation(bind)
    _precheck_existing_scope(bind)

    # Remove the permissive single-column relationship before installing the
    # tenant-paired RESTRICT relationship.  All operations remain in Alembic's
    # transaction, so a failed DDL step rolls back the entire change.
    op.drop_constraint(_OLD_OWNER_FK, "data_sources", type_="foreignkey")
    op.drop_constraint(_RESOURCE_CHECK, "data_sources", type_="check")
    op.create_foreign_key(
        _OWNER_FK,
        "data_sources",
        "agents",
        ["owner_agent_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        _RESOURCE_CHECK,
        "data_sources",
        "resource_scope IN ('modeling', 'agent_runtime')",
    )
    op.create_check_constraint(
        _OWNER_CHECK,
        "data_sources",
        "owner_agent_id IS NULL OR (tenant_id IS NOT NULL AND resource_scope = 'agent_runtime')",
    )


def _precheck_downgrade(bind: sa.engine.Connection) -> None:
    """Do not silently restore CASCADE while Agent-owned rows exist."""

    owner_row = bind.execute(
        sa.text(
            "SELECT id FROM data_sources "
            "WHERE owner_agent_id IS NOT NULL ORDER BY id LIMIT 1"
        )
    ).first()
    if owner_row is not None:
        raise RuntimeError(
            "20260912_32 downgrade requires every Agent-owned DataSource to "
            "be explicitly detached first; refusing to restore CASCADE ownership"
        )


def downgrade() -> None:
    bind = op.get_bind()
    _lock_for_scope_validation(bind)
    _precheck_downgrade(bind)

    op.drop_constraint(_OWNER_CHECK, "data_sources", type_="check")
    op.drop_constraint(_OWNER_FK, "data_sources", type_="foreignkey")
    op.drop_constraint(_RESOURCE_CHECK, "data_sources", type_="check")
    op.create_check_constraint(
        _RESOURCE_CHECK,
        "data_sources",
        "resource_scope IN ('modeling', 'agent_runtime')",
    )
    op.create_foreign_key(
        _OLD_OWNER_FK,
        "data_sources",
        "agents",
        ["owner_agent_id"],
        ["id"],
        ondelete="CASCADE",
    )

