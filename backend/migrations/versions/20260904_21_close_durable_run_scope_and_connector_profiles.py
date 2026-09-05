"""close durable run ownership and persist connector structure profiles

Revision ID: 20260904_21
Revises: 20260904_20
Create Date: 2026-09-04
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260904_21"
down_revision: Union[str, None] = "20260904_20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


UNIQUE_CONSTRAINTS = (
    ("uq_conversations_id_agent", "conversations", ("id", "agent_id")),
    (
        "uq_messages_id_conversation",
        "messages",
        ("id", "conversation_id"),
    ),
    (
        "uq_asset_versions_id_asset_tenant",
        "data_asset_versions",
        ("id", "asset_id", "tenant_id"),
    ),
)

FOREIGN_KEYS = (
    (
        "fk_agent_turn_runs_conversation_agent",
        "agent_turn_runs",
        "conversations",
        ("conversation_id", "agent_id"),
        ("id", "agent_id"),
    ),
    (
        "fk_agent_turn_runs_user_message_conversation",
        "agent_turn_runs",
        "messages",
        ("user_message_id", "conversation_id"),
        ("id", "conversation_id"),
    ),
    (
        "fk_agent_turn_runs_assistant_message_conversation",
        "agent_turn_runs",
        "messages",
        ("assistant_message_id", "conversation_id"),
        ("id", "conversation_id"),
    ),
    (
        "fk_managed_upload_runs_version_asset_tenant",
        "managed_upload_runs",
        "data_asset_versions",
        ("asset_version_id", "asset_id", "tenant_id"),
        ("id", "asset_id", "tenant_id"),
    ),
)

# Each query returns one aggregate only. A migration failure reports counts and
# never emits customer identifiers or attempts an ambiguous ownership repair.
OWNERSHIP_MISMATCH_QUERIES = (
    (
        "turn_conversation",
        """
        SELECT count(*)
          FROM agent_turn_runs AS run
          LEFT JOIN conversations AS conversation
            ON conversation.id = run.conversation_id
           AND conversation.agent_id = run.agent_id
         WHERE run.conversation_id IS NOT NULL
           AND conversation.id IS NULL
        """,
    ),
    (
        "turn_user_message",
        """
        SELECT count(*)
          FROM agent_turn_runs AS run
          LEFT JOIN messages AS message
            ON message.id = run.user_message_id
           AND message.conversation_id = run.conversation_id
         WHERE run.user_message_id IS NOT NULL
           AND message.id IS NULL
        """,
    ),
    (
        "turn_assistant_message",
        """
        SELECT count(*)
          FROM agent_turn_runs AS run
          LEFT JOIN messages AS message
            ON message.id = run.assistant_message_id
           AND message.conversation_id = run.conversation_id
         WHERE run.assistant_message_id IS NOT NULL
           AND message.id IS NULL
        """,
    ),
    (
        "upload_asset_version",
        """
        SELECT count(*)
          FROM managed_upload_runs AS run
          LEFT JOIN data_asset_versions AS version
            ON version.id = run.asset_version_id
           AND version.asset_id = run.asset_id
           AND version.tenant_id = run.tenant_id
         WHERE run.asset_version_id IS NOT NULL
           AND version.id IS NULL
        """,
    ),
)


def _json_document_type():
    return sa.JSON().with_variant(
        postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
        "postgresql",
    )


def _sha256_check(column_name: str) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column_name}) = 64 AND {column_name} = lower({column_name}) "
        f"AND {remainder} = ''"
    )


def _assert_existing_ownership_integrity(connection) -> None:
    mismatches = {
        label: int(connection.execute(sa.text(query)).scalar_one())
        for label, query in OWNERSHIP_MISMATCH_QUERIES
    }
    invalid = {label: count for label, count in mismatches.items() if count}
    if invalid:
        summary = ", ".join(
            f"{label}_mismatches={invalid[label]}" for label in sorted(invalid)
        )
        raise RuntimeError(
            "20260904_21 ownership precondition failed; " + summary
        )


def _runtime_role_statement(*, hardened: bool) -> sa.TextClause:
    if hardened:
        grants = """
            GRANT SELECT, INSERT, UPDATE ON TABLE
              public.agent_turn_runs, public.managed_upload_runs
              TO ontology_app;
            GRANT SELECT, INSERT ON TABLE public.agent_turn_events
              TO ontology_app;
        """
    else:
        # Restore the exact privileges established by revisions 18 and 19.
        grants = """
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
              public.agent_turn_runs, public.agent_turn_events,
              public.managed_upload_runs
              TO ontology_app;
        """
    return sa.text(
        f"""DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ontology_app') THEN
            REVOKE ALL PRIVILEGES ON TABLE
              public.agent_turn_runs, public.agent_turn_events,
              public.managed_upload_runs
              FROM ontology_app;
            {grants}
          END IF;
        END
        $$"""
    )


def upgrade() -> None:
    connection = op.get_bind()
    _assert_existing_ownership_integrity(connection)

    op.add_column(
        "connector_bindings",
        sa.Column(
            "structure_profile",
            _json_document_type(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.add_column(
        "connector_bindings",
        sa.Column(
            "structure_fingerprint",
            sa.String(length=64),
            server_default="",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_connector_bindings_structure_fingerprint",
        "connector_bindings",
        "structure_fingerprint = '' OR ("
        + _sha256_check("structure_fingerprint")
        + ")",
    )

    for name, table_name, columns in UNIQUE_CONSTRAINTS:
        op.create_unique_constraint(name, table_name, list(columns))
    for name, source, target, local_columns, remote_columns in FOREIGN_KEYS:
        op.create_foreign_key(
            name,
            source,
            target,
            list(local_columns),
            list(remote_columns),
        )

    if connection.dialect.name == "postgresql":
        op.execute(_runtime_role_statement(hardened=True))


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute(_runtime_role_statement(hardened=False))

    for name, source, _target, _local, _remote in reversed(FOREIGN_KEYS):
        op.drop_constraint(name, source, type_="foreignkey")
    for name, table_name, _columns in reversed(UNIQUE_CONSTRAINTS):
        op.drop_constraint(name, table_name, type_="unique")

    op.drop_constraint(
        "ck_connector_bindings_structure_fingerprint",
        "connector_bindings",
        type_="check",
    )
    op.drop_column("connector_bindings", "structure_fingerprint")
    op.drop_column("connector_bindings", "structure_profile")
