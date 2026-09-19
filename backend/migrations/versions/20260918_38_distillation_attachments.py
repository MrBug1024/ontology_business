"""Expiring conversation inputs kept outside the modeling library.

Revision ID: 20260918_38
Revises: 20260918_37
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260918_38"
down_revision = "20260918_37"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_distillation_turn_owner", "distillation_conversation_turns",
        ["id", "project_id", "tenant_id", "created_by"])
    op.create_table("distillation_attachments",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("scenario_id", sa.String(32), nullable=True),
        sa.Column("created_by", sa.String(32), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(150), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("parsed_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id", "tenant_id"], ["distillation_projects.id", "distillation_projects.tenant_id"],
            name="fk_distillation_attachment_project", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["scenario_id", "tenant_id"], ["business_scenarios.id", "business_scenarios.tenant_id"],
            name="fk_distillation_attachment_scenario", ondelete="RESTRICT"),
        sa.UniqueConstraint("id", "project_id", "tenant_id", "created_by", name="uq_distillation_attachment_owner"),
        sa.UniqueConstraint("project_id", "created_by", "request_id", name="uq_distillation_attachment_request"),
        sa.CheckConstraint("status IN ('ready','bound','removed','expired')", name="ck_distillation_attachment_status"),
        sa.CheckConstraint("byte_size > 0 AND byte_size <= 10485760 AND char_length(parsed_text) <= 200000", name="ck_distillation_attachment_size"),
        sa.CheckConstraint("status IN ('ready','bound') OR parsed_text = ''", name="ck_distillation_attachment_expiry_content"),
    )
    for column in ("tenant_id", "project_id", "expires_at"):
        op.create_index("ix_distillation_attachments_" + column, "distillation_attachments", [column])
    op.create_table("distillation_turn_attachments",
        sa.Column("turn_id", sa.String(32), primary_key=True),
        sa.Column("attachment_id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("user_id", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(["turn_id", "project_id", "tenant_id", "user_id"],
            ["distillation_conversation_turns.id", "distillation_conversation_turns.project_id",
             "distillation_conversation_turns.tenant_id", "distillation_conversation_turns.created_by"],
            name="fk_distillation_turn_attachment_turn_owner", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["attachment_id", "project_id", "tenant_id", "user_id"],
            ["distillation_attachments.id", "distillation_attachments.project_id",
             "distillation_attachments.tenant_id", "distillation_attachments.created_by"],
            name="fk_distillation_turn_attachment_input_owner", ondelete="RESTRICT"),
    )
    op.create_index("ix_distillation_turn_attachments_input", "distillation_turn_attachments", ["attachment_id", "turn_id"])
    op.execute("""
        REVOKE ALL ON TABLE distillation_attachments, distillation_turn_attachments FROM PUBLIC;
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
            GRANT SELECT, INSERT, UPDATE ON TABLE distillation_attachments TO ontology_app;
            GRANT SELECT, INSERT ON TABLE distillation_turn_attachments TO ontology_app;
          END IF;
        END $$;
    """)


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM distillation_attachments")):
        raise RuntimeError("Conversation attachment history exists; export and archive it before downgrading")
    op.drop_table("distillation_turn_attachments")
    op.drop_table("distillation_attachments")
    op.drop_constraint("uq_distillation_turn_owner", "distillation_conversation_turns", type_="unique")

