"""Verify the full Alembic head/downgrade/head path in an isolated database."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import URL, create_engine, text


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

_DATABASE_PREFIX = "ontology_migration_verify_"
_DATABASE_NAME_RE = re.compile(r"^ontology_migration_verify_[0-9a-f]{12}$")
_ENVIRONMENT_KEYS = ("ALEMBIC_DATABASE_URL", "ALEMBIC_ROLE", "ALEMBIC_USE_ADMIN")
_DETACH_FUNCTION_SIGNATURE = (
    "public.detach_data_source_file_references(varchar,varchar,varchar[])"
)


def _database_url(settings, database: str) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username=settings.postgresql_admin_user.strip() or "postgres",
        password=settings.postgresql_admin_password or settings.postgresql_password,
        host=settings.postgresql_host,
        port=settings.postgresql_port,
        database=database,
    )


def _revision(database_url: URL) -> str:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            return str(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            )
    finally:
        engine.dispose()


def _verify_detach_function_contract(
    connection,
    *,
    runtime_role: str,
    expected_revision: str,
) -> None:
    function = connection.execute(
        text(
            """
            SELECT procedure.prosecdef, procedure.proconfig,
                   pg_get_functiondef(procedure.oid) AS definition,
                   EXISTS (
                       SELECT 1
                         FROM aclexplode(
                             COALESCE(
                                 procedure.proacl,
                                 acldefault('f', procedure.proowner)
                             )
                         ) AS acl
                        WHERE acl.grantee = 0
                          AND acl.privilege_type = 'EXECUTE'
                   ) AS public_execute
              FROM pg_proc AS procedure
             WHERE procedure.oid = to_regprocedure(:signature)
            """
        ),
        {"signature": _DETACH_FUNCTION_SIGNATURE},
    ).one_or_none()
    if function is None or not bool(function.prosecdef):
        raise RuntimeError(
            f"revision {expected_revision} detach function is missing or not SECURITY DEFINER"
        )
    if bool(function.public_execute):
        raise RuntimeError(
            f"revision {expected_revision} detach function is executable by PUBLIC"
        )
    if "search_path=pg_catalog, public" not in set(function.proconfig or []):
        raise RuntimeError(
            f"revision {expected_revision} detach function search_path is not fixed"
        )
    can_execute = connection.execute(
        text(
            "SELECT has_function_privilege("
            ":runtime_role, :signature, 'EXECUTE')"
        ),
        {
            "runtime_role": runtime_role,
            "signature": _DETACH_FUNCTION_SIGNATURE,
        },
    ).scalar_one()
    if not bool(can_execute):
        raise RuntimeError(
            f"runtime role cannot execute revision {expected_revision} detach function"
        )

    definition = str(function.definition)
    common_markers = (
        "UPDATE public.data_asset_versions AS version",
        "DELETE FROM public.dataset_fragments AS fragment",
    )
    revision_17_markers = (
        "UPDATE public.ingestion_runs AS run",
        "UPDATE public.derivation_runs AS run",
        "DELETE FROM public.derivation_evidence AS evidence",
    )
    if any(marker not in definition for marker in common_markers):
        raise RuntimeError(
            f"revision {expected_revision} detach function lost the v16 catalog behavior"
        )
    if expected_revision == "20260831_16":
        if any(marker in definition for marker in revision_17_markers):
            raise RuntimeError("revision 16 detach function still contains revision 17 behavior")
    elif any(marker not in definition for marker in revision_17_markers):
        raise RuntimeError("head detach function is missing revision 17 trace cleanup behavior")


def _verify_revision_16_contract(database_url: URL, *, runtime_role: str) -> None:
    expected_revision = "20260831_16"
    if _revision(database_url) != expected_revision:
        raise RuntimeError("isolated migration database did not reach revision 16")
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            _verify_detach_function_contract(
                connection,
                runtime_role=runtime_role,
                expected_revision=expected_revision,
            )
    finally:
        engine.dispose()


def _verify_revision_09_contract(database_url: URL) -> None:
    expected_revision = "20260829_09"
    if _revision(database_url) != expected_revision:
        raise RuntimeError("isolated migration database did not reach revision 09")
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            function = connection.execute(
                text("SELECT to_regprocedure(:signature)"),
                {"signature": _DETACH_FUNCTION_SIGNATURE},
            ).scalar_one()
            if function is not None:
                raise RuntimeError("revision 09 unexpectedly retains the revision 16 function")
    finally:
        engine.dispose()


def _constraint_names(connection: Any, table_names: tuple[str, ...]) -> set[str]:
    return {
        str(name)
        for name in connection.execute(
            text(
                """
                SELECT constraint_row.conname
                  FROM pg_constraint AS constraint_row
                  JOIN pg_class AS table_row
                    ON table_row.oid = constraint_row.conrelid
                  JOIN pg_namespace AS namespace_row
                    ON namespace_row.oid = table_row.relnamespace
                 WHERE namespace_row.nspname = 'public'
                   AND table_row.relname = ANY(:table_names)
                """
            ),
            {"table_names": list(table_names)},
        ).scalars()
    }


def _verify_revision_19_contract(database_url: URL) -> None:
    expected_revision = "20260904_19"
    if _revision(database_url) != expected_revision:
        raise RuntimeError("isolated migration database did not reach revision 19")
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            tables = {
                str(name)
                for name in connection.execute(
                    text(
                        """
                        SELECT table_name
                          FROM information_schema.tables
                         WHERE table_schema = 'public'
                           AND table_name IN (
                               'agent_turn_runs',
                               'agent_turn_events',
                               'managed_upload_runs'
                           )
                        """
                    )
                ).scalars()
            }
            if tables != {
                "agent_turn_runs",
                "agent_turn_events",
                "managed_upload_runs",
            }:
                raise RuntimeError("revision 19 durable run tables are incomplete")
            constraints = _constraint_names(
                connection,
                ("agent_turn_runs", "agent_turn_events", "managed_upload_runs"),
            )
            required = {
                "fk_agent_turn_runs_agent_tenant",
                "fk_agent_turn_runs_preparation_tenant",
                "fk_agent_turn_runs_parent_tenant",
                "fk_agent_turn_events_run_tenant",
                "fk_managed_upload_runs_source_tenant",
                "fk_managed_upload_runs_file_source",
                "fk_managed_upload_runs_asset_tenant",
                "fk_managed_upload_runs_version_tenant",
                "uq_agent_turn_runs_parent_retry",
            }
            if not required <= constraints:
                raise RuntimeError("revision 19 durable run constraints are incomplete")
    finally:
        engine.dispose()


def _verify_revision_20_contract(database_url: URL) -> None:
    expected_revision = "20260904_20"
    if _revision(database_url) != expected_revision:
        raise RuntimeError("isolated migration database did not reach revision 20")
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connector_columns = set(
                connection.execute(
                    text(
                        """
                        SELECT column_name
                          FROM information_schema.columns
                         WHERE table_schema = 'public'
                           AND table_name = 'connector_bindings'
                           AND column_name IN (
                               'structure_profile', 'structure_fingerprint'
                           )
                        """
                    )
                ).scalars()
            )
            if connector_columns:
                raise RuntimeError("revision 21 connector columns survived downgrade")
            constraints = _constraint_names(
                connection,
                (
                    "conversations",
                    "messages",
                    "data_asset_versions",
                    "agent_turn_runs",
                    "assistant_threads",
                    "assistant_messages",
                    "assistant_request_runs",
                    "managed_upload_runs",
                ),
            )
            revision_21_constraints = {
                "uq_conversations_id_agent",
                "uq_messages_id_conversation",
                "uq_asset_versions_id_asset_tenant",
                "fk_agent_turn_runs_conversation_agent",
                "fk_agent_turn_runs_user_message_conversation",
                "fk_agent_turn_runs_assistant_message_conversation",
                "uq_assistant_threads_id_tenant_user",
                "uq_assistant_messages_id_thread",
                "fk_assistant_request_runs_thread_principal",
                "fk_assistant_request_runs_user_message_thread",
                "fk_assistant_request_runs_assistant_message_thread",
                "ck_assistant_request_runs_lease_state",
                "fk_managed_upload_runs_version_asset_tenant",
            }
            if constraints & revision_21_constraints:
                raise RuntimeError("revision 21 ownership constraints survived downgrade")
            expected_release_statuses = {
                "r20release_dataset_dependency": "rolled_back",
                "r20release_semantic_dependency": "rolled_back",
                "r20release_malformed_dependency": "rolled_back",
                "r20release_zero_data": "released",
                "r20release_manual_contract": "released",
                "r20release_empty_mapping_list": "released",
            }
            release_rows = {
                str(row.id): row
                for row in connection.execute(
                    text(
                        """
                        SELECT id, status, withdrawn_at, withdraw_reason
                          FROM ontology_releases
                         WHERE id = ANY(:release_ids)
                        """
                    ),
                    {"release_ids": list(expected_release_statuses)},
                )
            }
            if set(release_rows) != set(expected_release_statuses):
                raise RuntimeError("revision 20 release isolation fixtures are incomplete")
            for release_id, expected_status in expected_release_statuses.items():
                row = release_rows[release_id]
                if str(row.status) != expected_status:
                    raise RuntimeError(
                        f"revision 20 source isolation failed for {release_id}"
                    )
                if expected_status == "rolled_back":
                    if row.withdrawn_at is None or not str(row.withdraw_reason).startswith(
                        "system_source_isolation_v1:"
                    ):
                        raise RuntimeError(
                            f"revision 20 withdrawal audit is incomplete for {release_id}"
                        )
                elif row.withdrawn_at is not None or str(row.withdraw_reason or ""):
                    raise RuntimeError(
                        f"revision 20 changed a source-independent release: {release_id}"
                    )
    finally:
        engine.dispose()


def _seed_revision_20_release_isolation_cases(database_url: URL) -> None:
    """Create immutable rev19 snapshots without relying on mutable authoring rows."""

    cases = {
        "dataset_dependency": {
            "capability_ports": [
                {"id": "deleted-port", "dataset_schema_hash": "a" * 64}
            ]
        },
        "semantic_dependency": {
            "functions": [
                {
                    "id": "function-with-deleted-mapping",
                    "runtime_config": {
                        "provider_config": {
                            "semantic_mapping_ids": ["deleted-mapping"]
                        }
                    },
                }
            ]
        },
        "malformed_dependency": {
            "functions": [
                {
                    "id": "function-with-malformed-mapping",
                    "runtime_config": {
                        "provider_config": {"semantic_mapping_ids": "invalid"}
                    },
                }
            ]
        },
        "zero_data": {"functions": [], "capability_ports": []},
        "manual_contract": {
            "capability_ports": [
                {
                    "id": "manual-contract-port",
                    "dataset_schema_hash": "",
                    "schema_document": {
                        "x-platform-input-contract": {
                            "version": "tabular-content/v1",
                            "relations": [
                                {"fields": [{"name": "record_id"}]}
                            ],
                        }
                    },
                }
            ]
        },
        "empty_mapping_list": {
            "functions": [
                {
                    "id": "function-without-mapping",
                    "runtime_config": {
                        "provider_config": {"semantic_mapping_ids": []}
                    },
                }
            ]
        },
    }
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO tenants (id, name, created_at)
                    VALUES ('r20tenant', 'Revision 20 tenant', CURRENT_TIMESTAMP)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO business_scenarios (
                        id, tenant_id, is_public, name, description, industry,
                        namespace, status, created_at, updated_at
                    ) VALUES (
                        'r20scenario', 'r20tenant', FALSE, 'Revision 20 scenario',
                        '', '', 'default', 'active', CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO ontology_branches (
                        id, tenant_id, scenario_id, name, description, status,
                        base_snapshot_id, head_snapshot_id, created_by_user_id,
                        created_at, updated_at
                    ) VALUES (
                        'r20branch', 'r20tenant', 'r20scenario', 'main', '',
                        'active', NULL, NULL, NULL, CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            for index, (case_name, content) in enumerate(cases.items(), start=1):
                snapshot_id = f"r20snapshot_{case_name}"
                release_id = f"r20release_{case_name}"
                connection.execute(
                    text(
                        """
                        INSERT INTO ontology_snapshots (
                            id, tenant_id, scenario_id, branch_id,
                            parent_snapshot_id, kind, content, content_hash,
                            created_by_user_id, created_at
                        ) VALUES (
                            :snapshot_id, 'r20tenant', 'r20scenario', 'r20branch',
                            NULL, 'merge', CAST(:content AS json), :content_hash,
                            NULL, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "snapshot_id": snapshot_id,
                        "content": json.dumps(content, sort_keys=True),
                        "content_hash": f"{index:064x}",
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO ontology_releases (
                            id, tenant_id, scenario_id, branch_id, snapshot_id,
                            proposal_id, environment, status, notes,
                            connector_audit, created_by_user_id, withdrawn_at,
                            withdrawn_by_user_id, withdraw_reason, created_at
                        ) VALUES (
                            :release_id, 'r20tenant', 'r20scenario', 'r20branch',
                            :snapshot_id, NULL, 'staging', 'released', '',
                            '[]'::json, NULL, NULL, NULL, '', CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {"release_id": release_id, "snapshot_id": snapshot_id},
                )
    finally:
        engine.dispose()


def _verify_head_contract(database_url: URL, *, runtime_role: str, head: str) -> None:
    if _revision(database_url) != head:
        raise RuntimeError("isolated migration database did not reach the expected head")
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            assistant_request_owner = connection.execute(
                text(
                    "SELECT tableowner FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename = 'assistant_request_runs'"
                )
            ).scalar_one()
            if str(assistant_request_owner) == runtime_role:
                raise RuntimeError("runtime role must not own assistant_request_runs")
            function = connection.execute(
                text(
                    """
                    SELECT procedure.prosecdef, procedure.proconfig,
                           EXISTS (
                               SELECT 1
                                 FROM aclexplode(
                                     COALESCE(
                                         procedure.proacl,
                                         acldefault('f', procedure.proowner)
                                     )
                                 ) AS acl
                                WHERE acl.grantee = 0
                                  AND acl.privilege_type = 'EXECUTE'
                           ) AS public_execute
                      FROM pg_proc AS procedure
                     WHERE procedure.oid = to_regprocedure(
                         'public.detach_expired_catalog_asset_blob(character varying)'
                     )
                    """
                )
            ).one_or_none()
            if function is None or not bool(function.prosecdef):
                raise RuntimeError("guarded attachment expiry function is missing")
            if bool(function.public_execute):
                raise RuntimeError("attachment expiry function is executable by PUBLIC")
            if "search_path=pg_catalog, public" not in set(function.proconfig or []):
                raise RuntimeError("attachment expiry function search_path is not fixed")
            runtime_contract = connection.execute(
                text(
                    """
                    SELECT
                      has_function_privilege(
                        :runtime_role,
                        'public.detach_expired_catalog_asset_blob(character varying)',
                        'EXECUTE'
                      ) AS can_execute,
                      has_table_privilege(
                        :runtime_role,
                        'public.data_asset_versions',
                        'UPDATE'
                      ) AS can_update_versions
                    """
                ),
                {"runtime_role": runtime_role},
            ).one()
            if not bool(runtime_contract.can_execute):
                raise RuntimeError("runtime role cannot execute guarded attachment expiry")
            if bool(runtime_contract.can_update_versions):
                raise RuntimeError("runtime role gained forbidden asset-version UPDATE")
            _verify_detach_function_contract(
                connection,
                runtime_role=runtime_role,
                expected_revision=head,
            )
            withdrawal_columns = set(
                connection.execute(
                    text(
                        """
                        SELECT column_name
                          FROM information_schema.columns
                         WHERE table_schema = 'public'
                           AND table_name = 'ontology_releases'
                           AND column_name IN (
                               'withdrawn_at',
                               'withdrawn_by_user_id',
                               'withdraw_reason'
                           )
                        """
                    )
                ).scalars()
            )
            if withdrawal_columns != {
                "withdrawn_at",
                "withdrawn_by_user_id",
                "withdraw_reason",
            }:
                raise RuntimeError("release withdrawal audit columns are incomplete")
            connector_columns = {
                str(row.column_name): (str(row.data_type), str(row.is_nullable))
                for row in connection.execute(
                    text(
                        """
                        SELECT column_name, data_type, is_nullable
                          FROM information_schema.columns
                         WHERE table_schema = 'public'
                           AND table_name = 'connector_bindings'
                           AND column_name IN (
                               'structure_profile', 'structure_fingerprint'
                           )
                        """
                    )
                )
            }
            if connector_columns != {
                "structure_profile": ("jsonb", "NO"),
                "structure_fingerprint": ("character varying", "NO"),
            }:
                raise RuntimeError("connector structure profile columns are incomplete")
            constraints = _constraint_names(
                connection,
                (
                    "connector_bindings",
                    "conversations",
                    "messages",
                    "data_asset_versions",
                    "agent_turn_runs",
                    "managed_upload_runs",
                ),
            )
            required_constraints = {
                "ck_connector_bindings_structure_fingerprint",
                "uq_conversations_id_agent",
                "uq_messages_id_conversation",
                "uq_asset_versions_id_asset_tenant",
                "fk_agent_turn_runs_conversation_agent",
                "fk_agent_turn_runs_user_message_conversation",
                "fk_agent_turn_runs_assistant_message_conversation",
                "fk_managed_upload_runs_version_asset_tenant",
            }
            if not required_constraints <= constraints:
                raise RuntimeError("durable ownership constraints are incomplete")
            if head in {"20260912_32", "20260912_33"}:
                from scripts.verify_postgresql_runtime import (
                    _verify_agent_scope_contract,
                )
                _verify_agent_scope_contract(connection)
            if head == "20260912_33":
                from scripts.verify_postgresql_runtime import (
                    _verify_scenario_audit_purge_contract,
                )
                _verify_scenario_audit_purge_contract(connection)
            privilege_rows = {
                table_name: {
                    privilege: bool(
                        connection.execute(
                            text(
                                "SELECT has_table_privilege("
                                ":runtime_role, :table_name, :privilege)"
                            ),
                            {
                                "runtime_role": runtime_role,
                                "table_name": f"public.{table_name}",
                                "privilege": privilege.upper(),
                            },
                        ).scalar_one()
                    )
                    for privilege in (
                        "select",
                        "insert",
                        "update",
                        "delete",
                        "truncate",
                        "references",
                        "trigger",
                    )
                }
                for table_name in (
                    "agent_turn_runs",
                    "agent_turn_events",
                    "assistant_request_runs",
                    "managed_upload_runs",
                )
            }
            expected_privileges = {
                "agent_turn_runs": {
                    "select": True,
                    "insert": True,
                    "update": True,
                    "delete": False,
                    "truncate": False,
                    "references": False,
                    "trigger": False,
                },
                "agent_turn_events": {
                    "select": True,
                    "insert": True,
                    "update": False,
                    "delete": False,
                    "truncate": False,
                    "references": False,
                    "trigger": False,
                },
                "assistant_request_runs": {
                    "select": True,
                    "insert": True,
                    "update": True,
                    "delete": True,
                    "truncate": False,
                    "references": False,
                    "trigger": False,
                },
                "managed_upload_runs": {
                    "select": True,
                    "insert": True,
                    "update": True,
                    "delete": False,
                    "truncate": False,
                    "references": False,
                    "trigger": False,
                },
            }
            if privilege_rows != expected_privileges:
                raise RuntimeError("durable run runtime privileges are not least-privilege")
    finally:
        engine.dispose()


def main() -> int:
    from app.config import get_settings

    settings = get_settings()
    runtime_role = settings.postgresql_user.strip()
    if not runtime_role:
        raise RuntimeError("POSTGRESQL_USER must identify the runtime role")

    database_name = _DATABASE_PREFIX + uuid4().hex[:12]
    if _DATABASE_NAME_RE.fullmatch(database_name) is None:
        raise RuntimeError("refusing an invalid migration verification database name")
    control_url = _database_url(settings, "postgres")
    target_url = _database_url(settings, database_name)
    created = False
    previous_environment = {key: os.environ.get(key) for key in _ENVIRONMENT_KEYS}
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    head = ScriptDirectory.from_config(config).get_current_head()
    if not head:
        raise RuntimeError("Alembic has no single head")
    control_engine = create_engine(control_url, isolation_level="AUTOCOMMIT")

    try:
        with control_engine.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": database_name},
            ).scalar_one_or_none()
            if exists is not None:
                raise RuntimeError("isolated migration database unexpectedly already exists")
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
            created = True

        os.environ["ALEMBIC_DATABASE_URL"] = target_url.render_as_string(
            hide_password=False
        )
        os.environ.pop("ALEMBIC_ROLE", None)
        os.environ.pop("ALEMBIC_USE_ADMIN", None)

        # Exercise the reversible 18/19 chain before crossing irreversible
        # revision 20, then verify the current head and revision 21 downgrade
        # independently without attempting to reconstruct retired metadata.
        command.upgrade(config, "20260904_19")
        _verify_revision_19_contract(target_url)
        command.downgrade(config, "20260831_16")
        _verify_revision_16_contract(target_url, runtime_role=runtime_role)
        command.downgrade(config, "20260829_09")
        _verify_revision_09_contract(target_url)
        command.upgrade(config, "20260904_19")
        _verify_revision_19_contract(target_url)
        _seed_revision_20_release_isolation_cases(target_url)
        command.upgrade(config, "20260907_26")
        command.downgrade(config, "20260904_20")
        _verify_revision_20_contract(target_url)
        fixture_engine = create_engine(target_url)
        try:
            with fixture_engine.begin() as connection:
                # The rev20 isolation fixture deliberately publishes three
                # alternatives. Explicitly retire two before removing that
                # old dimension; production migration never chooses for users.
                connection.execute(text("UPDATE ontology_releases SET status = 'rolled_back', withdrawn_at = CURRENT_TIMESTAMP, withdraw_reason = 'roundtrip_fixture_explicit_retirement' WHERE id IN ('r20release_manual_contract', 'r20release_empty_mapping_list')"))
        finally:
            fixture_engine.dispose()
        command.upgrade(config, head)
        _verify_head_contract(target_url, runtime_role=runtime_role, head=head)

        # Revision 48 deliberately refuses downgrade because restoring the
        # previous purge function would reintroduce a protected-history
        # deletion failure. Older revisions also retain their own refusal
        # boundary at revision 33; the current head must be the first refusal
        # observed when the full path starts from head.
        try:
            command.downgrade(config, "20260908_27")
        except RuntimeError as exc:
            message = str(exc).lower()
            if "downgrade is refused" not in message:
                raise
            if _revision(target_url) != head:
                raise RuntimeError(
                    "Current head downgrade refusal did not preserve the head"
                ) from exc
        else:
            raise RuntimeError(
                "The current head must refuse a downgrade that would restore "
                "an unsafe scenario-purge function"
            )
        command.upgrade(config, head)
        _verify_head_contract(target_url, runtime_role=runtime_role, head=head)

        try:
            command.downgrade(config, "20260907_26")
        except RuntimeError as exc:
            # The command still starts at the current head and therefore
            # reaches the current irreversible boundary first.
            if "downgrade is refused" not in str(exc).lower():
                raise
        else:
            raise RuntimeError(
                "The current head must refuse a downgrade crossing the "
                "irreversible scenario-purge hardening boundary"
            )
        if _revision(target_url) != head:
            raise RuntimeError("Repeated downgrade refusal did not preserve the head")
        command.upgrade(config, head)
        _verify_head_contract(target_url, runtime_role=runtime_role, head=head)
        print(f"Alembic isolated round-trip passed at {head}")
        return 0
    finally:
        for key, value in previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            if created:
                with control_engine.connect() as connection:
                    connection.execute(
                        text(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = :name AND pid <> pg_backend_pid()"
                        ),
                        {"name": database_name},
                    )
                    connection.exec_driver_sql(f'DROP DATABASE "{database_name}"')
        finally:
            control_engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
