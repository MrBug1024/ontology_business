"""Make temporary distillation inputs collaborative within a project.

Revision ID: 20260920_41
Revises: 20260919_40
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260920_41"
down_revision = "20260919_40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # Older rows were written before the attachment carried an explicit
    # scenario scope.  Recover the only deterministic value from its owning
    # project, then fail closed if any historical row disagrees with that
    # scope instead of creating a constraint that would leave an unusable
    # collaboration record behind.
    bind.execute(sa.text("""
        UPDATE distillation_attachments AS attachment
           SET scenario_id = project.scenario_id
          FROM distillation_projects AS project
         WHERE attachment.project_id = project.id
           AND attachment.tenant_id = project.tenant_id
           AND attachment.scenario_id IS NULL
           AND project.scenario_id IS NOT NULL
    """))
    mismatch = bind.scalar(sa.text("""
        SELECT 1
          FROM distillation_attachments AS attachment
          JOIN distillation_projects AS project
            ON project.id = attachment.project_id
           AND project.tenant_id = attachment.tenant_id
         WHERE attachment.scenario_id IS DISTINCT FROM project.scenario_id
         LIMIT 1
    """))
    if mismatch:
        raise RuntimeError(
            "Distillation attachments have a scenario scope different from their project; reconcile them before migration"
        )
    # The historical owner unique keys remain so a downgrade can restore the
    # old composite foreign keys. They are not used as an access boundary.
    op.create_unique_constraint(
        "uq_distillation_turn_scope",
        "distillation_conversation_turns",
        ["id", "project_id", "tenant_id"],
    )
    op.create_unique_constraint(
        "uq_distillation_attachment_scope",
        "distillation_attachments",
        ["id", "project_id", "tenant_id"],
    )
    op.drop_constraint(
        "fk_distillation_turn_attachment_turn_owner",
        "distillation_turn_attachments",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_distillation_turn_attachment_input_owner",
        "distillation_turn_attachments",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_distillation_turn_attachment_turn_scope",
        "distillation_turn_attachments",
        "distillation_conversation_turns",
        ["turn_id", "project_id", "tenant_id"],
        ["id", "project_id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_distillation_turn_attachment_input_scope",
        "distillation_turn_attachments",
        "distillation_attachments",
        ["attachment_id", "project_id", "tenant_id"],
        ["id", "project_id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_distillation_turn_attachment_actor_tenant",
        "distillation_turn_attachments",
        "users",
        ["user_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    bind = op.get_bind()
    cross_user_link = bind.scalar(sa.text("""
        SELECT 1
          FROM distillation_turn_attachments AS link
          JOIN distillation_conversation_turns AS turn
            ON turn.id = link.turn_id
           AND turn.project_id = link.project_id
           AND turn.tenant_id = link.tenant_id
          JOIN distillation_attachments AS attachment
            ON attachment.id = link.attachment_id
           AND attachment.project_id = link.project_id
           AND attachment.tenant_id = link.tenant_id
         WHERE link.user_id <> turn.created_by
            OR link.user_id <> attachment.created_by
         LIMIT 1
    """))
    if cross_user_link:
        raise RuntimeError(
            "Shared attachment links exist; archive them before downgrading"
        )

    op.drop_constraint(
        "fk_distillation_turn_attachment_actor_tenant",
        "distillation_turn_attachments",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_distillation_turn_attachment_turn_scope",
        "distillation_turn_attachments",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_distillation_turn_attachment_input_scope",
        "distillation_turn_attachments",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_distillation_turn_attachment_turn_owner",
        "distillation_turn_attachments",
        "distillation_conversation_turns",
        ["turn_id", "project_id", "tenant_id", "user_id"],
        ["id", "project_id", "tenant_id", "created_by"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_distillation_turn_attachment_input_owner",
        "distillation_turn_attachments",
        "distillation_attachments",
        ["attachment_id", "project_id", "tenant_id", "user_id"],
        ["id", "project_id", "tenant_id", "created_by"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "uq_distillation_attachment_scope",
        "distillation_attachments",
        type_="unique",
    )
    op.drop_constraint(
        "uq_distillation_turn_scope",
        "distillation_conversation_turns",
        type_="unique",
    )
