"""Allow the runtime role to remove assistant request run lineage during purge.

Revision ID: 20260912_34
Revises: 20260912_33
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260912_34"
down_revision: Union[str, None] = "20260912_33"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(sa.text("""
        DO $$
        BEGIN
          REVOKE ALL ON TABLE assistant_request_runs FROM PUBLIC;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
            REVOKE ALL ON TABLE assistant_request_runs FROM ontology_app;
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE assistant_request_runs TO ontology_app;
          END IF;
        END
        $$
    """))


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(sa.text("""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
            REVOKE DELETE ON TABLE assistant_request_runs FROM ontology_app;
          END IF;
        END
        $$
    """))
