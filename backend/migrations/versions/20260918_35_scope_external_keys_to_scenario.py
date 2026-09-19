"""Bind external credentials and temporary uploads to one business scenario.

Unbound historical keys cannot be backfilled without guessing their intended
scenario. Revoke them and retain an explicit lifecycle audit event.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260918_35"
down_revision = "20260912_34"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("external_api_keys", sa.Column("scenario_id", sa.String(32), nullable=True))
    op.execute(sa.text("""
        INSERT INTO external_api_key_audit_events
            (id, api_key_id, tenant_id, subject_user_id, actor_user_id, event_type, details, created_at)
        SELECT md5('scenario-key-migration/v1:' || id), id, tenant_id, user_id, NULL, 'revoked',
            '{"reason":"scenario_binding_required","migration":"20260918_35"}'::json, CURRENT_TIMESTAMP
        FROM external_api_keys WHERE status = 'active'
    """))
    op.execute(sa.text("UPDATE external_api_keys SET status = 'revoked', revoked_at = CURRENT_TIMESTAMP WHERE status = 'active'"))
    op.create_foreign_key("fk_external_api_keys_scenario_tenant", "external_api_keys", "business_scenarios",
                          ["scenario_id", "tenant_id"], ["id", "tenant_id"], ondelete="CASCADE")
    op.create_check_constraint("ck_external_api_keys_bound_active", "external_api_keys",
                               "status <> 'active' OR scenario_id IS NOT NULL")
    op.create_index("ix_external_api_keys_scenario_id", "external_api_keys", ["scenario_id"])
    op.create_table("external_scenario_assets",
        sa.Column("asset_id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("scenario_id", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(["asset_id", "tenant_id"], ["data_assets.id", "data_assets.tenant_id"],
                                name="fk_external_scenario_assets_asset", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["scenario_id", "tenant_id"], ["business_scenarios.id", "business_scenarios.tenant_id"],
                                name="fk_external_scenario_assets_scenario", ondelete="RESTRICT"),
    )
    op.create_index("ix_external_scenario_assets_scenario_id", "external_scenario_assets", ["scenario_id"])
    op.execute(sa.text("""
        REVOKE ALL ON TABLE external_scenario_assets FROM PUBLIC;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
                GRANT SELECT, INSERT, DELETE ON TABLE external_scenario_assets TO ontology_app;
            END IF;
        END $$;
    """))


def downgrade() -> None:
    # Removing a restriction must never silently turn active scoped keys into
    # workspace-wide credentials. A rollback requires explicit reissuance.
    op.execute(sa.text("""
        INSERT INTO external_api_key_audit_events
            (id, api_key_id, tenant_id, subject_user_id, actor_user_id, event_type, details, created_at)
        SELECT md5('scenario-key-rollback/v1:' || id), id, tenant_id, user_id, NULL, 'revoked',
            '{"reason":"scenario_binding_rollback","migration":"20260918_35"}'::json, CURRENT_TIMESTAMP
        FROM external_api_keys WHERE status = 'active'
    """))
    op.execute(sa.text("UPDATE external_api_keys SET status = 'revoked', revoked_at = CURRENT_TIMESTAMP WHERE status = 'active'"))
    op.drop_table("external_scenario_assets")
    op.drop_index("ix_external_api_keys_scenario_id", table_name="external_api_keys")
    op.drop_constraint("ck_external_api_keys_bound_active", "external_api_keys", type_="check")
    op.drop_constraint("fk_external_api_keys_scenario_tenant", "external_api_keys", type_="foreignkey")
    op.drop_column("external_api_keys", "scenario_id")
