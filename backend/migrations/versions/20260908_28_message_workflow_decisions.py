"""Bind business approval messages to revisions and immutable evidence."""
from alembic import op
import sqlalchemy as sa


revision = "20260908_28"
down_revision = "20260908_27"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workflow_approval_requests", sa.Column("execution_key", sa.String(64), nullable=False, server_default=""))
    # Legacy decisions do not reliably identify a retry generation. Keep that
    # absence explicit; only a still-pending legacy request may resume a run.
    op.drop_index("uq_workflow_approvals_node", table_name="workflow_approval_requests")
    op.create_index("uq_workflow_approvals_execution_node", "workflow_approval_requests", ["workflow_run_id", "execution_key", "node_id"], unique=True)
    op.add_column("workflow_approval_requests", sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("workflow_approval_requests", sa.Column("decision_message_id", sa.String(160), nullable=True))
    op.add_column("workflow_approval_requests", sa.Column("decision_digest", sa.String(64), nullable=True))
    op.add_column("workflow_approval_requests", sa.Column("evidence_refs", sa.JSON(), nullable=False, server_default="[]"))
    op.create_check_constraint("ck_workflow_approval_revision", "workflow_approval_requests", "revision > 0")
    op.create_unique_constraint("uq_workflow_approval_id_scenario", "workflow_approval_requests", ["id", "scenario_id"])
    op.create_unique_constraint("uq_workflow_approval_decision_message", "workflow_approval_requests", ["resolved_by_user_id", "decision_message_id"])
    op.create_table("workflow_approval_evidence",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("approval_id", sa.String(32), nullable=False),
        sa.Column("scenario_id", sa.String(32), nullable=False),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("asset_version_id", sa.String(32)),
        sa.Column("dataset_version_id", sa.String(32)),
        sa.Column("bucket_file_id", sa.String(32), sa.ForeignKey("bucket_files.id", ondelete="RESTRICT")),
        sa.Column("content_signature", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["approval_id", "scenario_id"], ["workflow_approval_requests.id", "workflow_approval_requests.scenario_id"], name="fk_approval_evidence_request", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["scenario_id", "tenant_id"], ["business_scenarios.id", "business_scenarios.tenant_id"], name="fk_approval_evidence_scenario", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["asset_version_id", "tenant_id"], ["data_asset_versions.id", "data_asset_versions.tenant_id"], name="fk_approval_evidence_asset", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["dataset_version_id", "tenant_id"], ["dataset_versions.id", "dataset_versions.tenant_id"], name="fk_approval_evidence_dataset", ondelete="RESTRICT"),
        sa.CheckConstraint("(asset_version_id IS NOT NULL AND dataset_version_id IS NULL) OR (asset_version_id IS NULL AND dataset_version_id IS NOT NULL)", name="ck_approval_evidence_reference"),
        sa.UniqueConstraint("approval_id", "asset_version_id", name="uq_approval_evidence_asset"),
        sa.UniqueConstraint("approval_id", "dataset_version_id", name="uq_approval_evidence_dataset"),
    )
    op.create_index("ix_workflow_approval_evidence_asset_version_id", "workflow_approval_evidence", ["asset_version_id"])
    op.create_index("ix_workflow_approval_evidence_bucket_file_id", "workflow_approval_evidence", ["bucket_file_id"])
    from app.config import get_settings
    role = get_settings().postgresql_user.strip()
    if not role:
        raise RuntimeError("POSTGRESQL_USER must identify the approval runtime role")
    quoted = op.get_bind().dialect.identifier_preparer.quote(role)
    op.execute(f"REVOKE ALL ON public.workflow_approval_evidence FROM {quoted}")
    op.execute(f"GRANT SELECT, INSERT ON public.workflow_approval_evidence TO {quoted}")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT count(*) FROM (SELECT workflow_run_id, node_id FROM workflow_approval_requests GROUP BY workflow_run_id, node_id HAVING count(*) > 1) AS repeated")):
        raise RuntimeError("Cannot discard repeated approval execution history.")
    if connection.scalar(sa.text("SELECT count(*) FROM workflow_approval_requests WHERE decision_message_id IS NOT NULL OR evidence_refs::jsonb <> '[]'::jsonb")):
        raise RuntimeError("Cannot discard recorded approval messages or evidence; restore a pre-upgrade backup instead.")
    if connection.scalar(sa.text("SELECT count(*) FROM workflow_approval_evidence")):
        raise RuntimeError("Cannot discard retained approval evidence.")
    op.drop_table("workflow_approval_evidence")
    op.drop_constraint("uq_workflow_approval_id_scenario", "workflow_approval_requests", type_="unique")
    op.drop_constraint("uq_workflow_approval_decision_message", "workflow_approval_requests", type_="unique")
    op.drop_constraint("ck_workflow_approval_revision", "workflow_approval_requests", type_="check")
    op.drop_column("workflow_approval_requests", "evidence_refs")
    op.drop_column("workflow_approval_requests", "decision_message_id")
    op.drop_column("workflow_approval_requests", "decision_digest")
    op.drop_column("workflow_approval_requests", "revision")
    op.drop_index("uq_workflow_approvals_execution_node", table_name="workflow_approval_requests")
    op.create_index("uq_workflow_approvals_node", "workflow_approval_requests", ["workflow_run_id", "node_id"], unique=True)
    op.drop_column("workflow_approval_requests", "execution_key")
