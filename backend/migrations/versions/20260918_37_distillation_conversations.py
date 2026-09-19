"""Durable collaborative business investigation turns.

Revision ID: 20260918_37
Revises: 20260918_36
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260918_37"
down_revision = "20260918_36"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("distillation_conversation_turns",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("created_by", sa.String(32), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("turn_number", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("assistant_message", sa.Text(), nullable=False),
        sa.Column("steps", postgresql.JSONB(), nullable=False),
        sa.Column("questions", postgresql.JSONB(), nullable=False),
        sa.Column("proposal", postgresql.JSONB(), nullable=True),
        sa.Column("applied_revision", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(500), nullable=False),
        sa.Column("context", postgresql.JSONB(), nullable=False),
        sa.Column("checkpoint", postgresql.JSONB(), nullable=False),
        sa.Column("model_calls", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(32), nullable=True),
        sa.Column("lease_generation", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id", "tenant_id"], ["distillation_projects.id", "distillation_projects.tenant_id"],
            name="fk_distillation_turn_project_tenant", ondelete="RESTRICT"),
        sa.UniqueConstraint("project_id", "request_id", name="uq_distillation_turn_request"),
        sa.UniqueConstraint("project_id", "turn_number", name="uq_distillation_turn_number"),
        sa.CheckConstraint("status IN ('queued','running','waiting','succeeded','cancelled','failed')", name="ck_distillation_turn_status"),
        sa.CheckConstraint("base_revision >= 1 AND turn_number >= 1 AND attempt >= 0 AND model_calls >= 0 AND lease_generation >= 0", name="ck_distillation_turn_counters"),
    )
    op.create_index("ix_distillation_conversation_turns_tenant_id", "distillation_conversation_turns", ["tenant_id"])
    op.create_index("uq_distillation_turn_active", "distillation_conversation_turns", ["project_id"], unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"))
    op.create_index("ix_distillation_turn_claim", "distillation_conversation_turns", ["status", "lease_expires_at", "created_at"])
    op.execute("""
        REVOKE ALL ON TABLE distillation_conversation_turns FROM PUBLIC;
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
            GRANT SELECT, INSERT, UPDATE ON TABLE distillation_conversation_turns TO ontology_app;
          END IF;
        END $$;
    """)


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM distillation_conversation_turns")):
        raise RuntimeError("Investigation history exists; export and archive it before downgrading")
    op.drop_table("distillation_conversation_turns")

