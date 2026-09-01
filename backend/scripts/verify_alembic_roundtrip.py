"""Verify the full Alembic head/downgrade/head path in an isolated database."""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys
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


def _verify_head_contract(database_url: URL, *, runtime_role: str, head: str) -> None:
    if _revision(database_url) != head:
        raise RuntimeError("isolated migration database did not reach the expected head")
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
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

        command.upgrade(config, head)
        _verify_head_contract(target_url, runtime_role=runtime_role, head=head)
        command.downgrade(config, "20260831_16")
        _verify_revision_16_contract(target_url, runtime_role=runtime_role)
        command.downgrade(config, "20260829_09")
        _verify_revision_09_contract(target_url)
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
