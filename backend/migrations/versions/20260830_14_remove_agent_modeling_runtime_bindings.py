"""remove Agent modeling-resource runtime bindings

Revision ID: 20260830_14
Revises: 20260830_13
Create Date: 2026-08-30
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260830_14"
down_revision: Union[str, None] = "20260830_13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ALL_CAPABILITIES = (
    '{"functions":{"mode":"all","selected_ids":[]},'
    '"actions":{"mode":"all","selected_ids":[]},'
    '"rules":{"mode":"all","selected_ids":[]},'
    '"events":{"mode":"all","selected_ids":[]},'
    '"workflows":{"mode":"all","selected_ids":[]}}'
)


def upgrade() -> None:
    # Old Agent bindings referenced modeling materials.  The ids are removed
    # because silently copying them into Agent-owned runtime connections would
    # preserve exactly the semantic mistake this migration fixes.
    op.execute(
        sa.text(
            """
            UPDATE agents
               SET data_source_ids = CAST('[]' AS json),
                   runtime_binding_mode = 'capability_only',
                   capability_scope = COALESCE(
                       capability_scope,
                       CAST(:all_capabilities AS json)
                   )
            """
        ).bindparams(all_capabilities=_ALL_CAPABILITIES)
    )
    op.alter_column(
        "agents",
        "runtime_binding_mode",
        existing_type=sa.String(length=20),
        server_default="capability_only",
        existing_nullable=False,
    )


def downgrade() -> None:
    # Discarded modeling bindings are intentionally not reconstructed.
    op.alter_column(
        "agents",
        "runtime_binding_mode",
        existing_type=sa.String(length=20),
        server_default="legacy",
        existing_nullable=False,
    )
