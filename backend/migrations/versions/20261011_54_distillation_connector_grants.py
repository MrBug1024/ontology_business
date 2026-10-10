"""Runtime-role grants for investigation connector sessions.

Revision ID: 20261011_54
Revises: 20261010_53
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20261011_54"
down_revision = "20261010_53"
branch_labels = None
depends_on = None


TABLE = "distillation_investigation_connector_sessions"


def upgrade() -> None:
    op.execute(f"REVOKE ALL ON TABLE {TABLE} FROM PUBLIC")
    op.execute(sa.text(f"""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
            REVOKE ALL ON TABLE {TABLE} FROM ontology_app;
            GRANT SELECT, INSERT, UPDATE ON TABLE {TABLE} TO ontology_app;
          END IF;
        END $$
    """))


def downgrade() -> None:
    op.execute(f"REVOKE SELECT, INSERT, UPDATE ON TABLE {TABLE} FROM ontology_app")
