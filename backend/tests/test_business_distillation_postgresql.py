"""Real PostgreSQL acceptance for discovery concurrency and publication ownership."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from app.distillation_models import DistillationProject, DistillationPublication
from app.distillation_schemas import DistillationDocument, Evidence, ProjectCreate, ProjectUpdate
from app.external_api_models import ExternalApiKey
from app.routers import business_distillation
from app.models import (
    AuthorizationGrant,
    BucketFile,
    BusinessScenario,
    DataSource,
    OrganizationMember,
    OrganizationRole,
    User,
)
from app.services import connector_service, distillation_analysis_service, distillation_connector_service, distillation_handoff_service, distillation_service, scenario_purge_plan_service
from isolated_postgresql import (
    IsolatedPostgreSQL,
    isolated_database,
    isolated_postgresql,
    seed_workspace,
    tenant_session,
)


def _document() -> DistillationDocument:
    return DistillationDocument(
        beneficiary="Service requester", pain="Repeated intake delays resolution",
        desired_outcome="A verified outcome", success_metric="Time to verified resolution",
        decision="continue", decision_reason="A bounded pilot can verify the proposed benefit",
    )


def _create_project(isolated: IsolatedPostgreSQL, workspace: dict[str, str]) -> str:
    with tenant_session(isolated.runtime_engine, workspace) as db:
        row = distillation_service.create_project(db, ProjectCreate(
            name="Evidence acceptance", scenario_id=workspace["scenario_id"], document=_document(),
        ))
        db.commit()
        return row.id


def test_concurrent_project_updates_keep_one_revision_and_report_conflict(isolated_postgresql):
    workspace = seed_workspace(isolated_postgresql.admin_engine)
    project_id = _create_project(isolated_postgresql, workspace)
    barrier = Barrier(2)
    def update(name: str) -> tuple[int, str]:
        with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
            barrier.wait(timeout=10)
            try:
                row = distillation_service.update_project(db, project_id, ProjectUpdate(
                    name=name, scenario_id=workspace["scenario_id"], expected_revision=1, document=_document(),
                ))
                db.commit()
                return row.revision, row.name
            except HTTPException as exc:
                db.rollback()
                return exc.status_code, name

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(update, name) for name in ("First editor", "Second editor")]
        outcomes = [future.result(timeout=20) for future in futures]
    assert sorted(status for status, _ in outcomes) == [2, 409]
    with Session(isolated_postgresql.admin_engine) as db:
        persisted = db.get(DistillationProject, project_id)
        assert persisted.revision == 2
        assert persisted.name == next(name for status, name in outcomes if status == 2)


def test_concurrent_publication_prevents_project_rebinding_to_another_scenario(isolated_postgresql):
    workspace = seed_workspace(isolated_postgresql.admin_engine)
    project_id = _create_project(isolated_postgresql, workspace)
    reached_lock = Event()

    def rebind() -> int:
        with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
            def before_sql(_connection, _cursor, statement, _parameters, _context, _many):
                # The old implementation reached UPDATE after checking for a
                # publication it could not yet see. Both forms must encounter
                # the publisher's lock before that transaction is committed.
                if (statement.startswith("UPDATE distillation_projects")
                        or "FROM distillation_projects" in statement and "FOR UPDATE" in statement):
                    reached_lock.set()

            event.listen(db.connection(), "before_cursor_execute", before_sql)
            try:
                distillation_service.update_project(db, project_id, ProjectUpdate(
                    name="Rebound project", scenario_id=workspace["other_scenario_id"],
                    expected_revision=1, document=_document(),
                ))
                db.commit()
                return 200
            except HTTPException as exc:
                db.rollback()
                return exc.status_code

    with tenant_session(isolated_postgresql.runtime_engine, workspace) as publishing:
        publication = distillation_service.publish(publishing, project_id, 1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(rebind)
            try:
                assert reached_lock.wait(timeout=10)
                assert not future.done()
                publishing.commit()
            finally:
                publishing.rollback()
            assert future.result(timeout=15) == 409
        assert publication.project_id == project_id
    with Session(isolated_postgresql.admin_engine) as db:
        row = db.get(DistillationProject, project_id)
        assert row.scenario_id == workspace["scenario_id"]
        assert row.revision == 1


def test_concurrent_publication_is_idempotent_and_draft_edits_preserve_artifacts(isolated_postgresql):
    workspace = seed_workspace(isolated_postgresql.admin_engine)
    project_id = _create_project(isolated_postgresql, workspace)
    barrier = Barrier(2)

    def publish() -> tuple[str, str]:
        with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
            barrier.wait(timeout=10)
            publication = distillation_service.publish(db, project_id, 1)
            db.commit()
            return publication.id, publication.data_source_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert results[0] == results[1]
    with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
        publication = db.get(DistillationPublication, results[0][0])
        before = publication.document.copy(), list(publication.artifacts)
        assert len(publication.artifacts) == 7
        assert all(hashlib.sha256(item["content"].encode()).hexdigest() == item["sha256"] for item in publication.artifacts)
        distillation_service.update_project(db, project_id, ProjectUpdate(
            name="Updated investigation", scenario_id=workspace["scenario_id"], expected_revision=1,
            document=_document().model_copy(update={"pain": "A new observation"}),
        ))
        db.commit()
        db.expire_all()
        retained = db.get(DistillationPublication, results[0][0])
        assert (retained.document, retained.artifacts) == before
        assert db.scalar(select(func.count()).select_from(DistillationPublication).where(
            DistillationPublication.project_id == project_id,
        )) == 1
        assert db.scalar(select(func.count()).select_from(DataSource).where(
            DataSource.config["distillation_project_id"].as_string() == project_id,
        )) == 1


def test_publication_rollback_leaves_no_catalog_entry(isolated_postgresql):
    workspace = seed_workspace(isolated_postgresql.admin_engine)
    project_id = _create_project(isolated_postgresql, workspace)
    with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
        publication = distillation_service.publish(db, project_id, 1)
        source_id, publication_id = publication.data_source_id, publication.id
        db.rollback()
    with Session(isolated_postgresql.admin_engine) as db:
        assert db.get(DataSource, source_id) is None
        assert db.get(DistillationPublication, publication_id) is None
    with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
        publication = distillation_service.publish(db, project_id, 1)
        db.commit()
        assert publication.project_revision == 1


def test_postgresql_rejects_cross_tenant_project_and_publication_references(isolated_postgresql):
    first = seed_workspace(isolated_postgresql.admin_engine)
    second = seed_workspace(isolated_postgresql.admin_engine)
    with Session(isolated_postgresql.admin_engine) as db:
        db.add(DistillationProject(
            tenant_id=first["tenant_id"], scenario_id=second["scenario_id"], name="Invalid scope",
            document={}, created_by=first["user_id"], updated_by=first["user_id"],
        ))
        with pytest.raises(IntegrityError) as failure:
            db.flush()
        assert failure.value.orig.diag.constraint_name == "fk_distillation_project_scenario_tenant"
        db.rollback()
    first_project = _create_project(isolated_postgresql, first)
    with Session(isolated_postgresql.admin_engine) as db:
        foreign = DataSource(tenant_id=second["tenant_id"], name="Foreign source", type="distillation", config={})
        db.add(foreign)
        db.flush()
        db.add(DistillationPublication(
            tenant_id=first["tenant_id"], project_id=first_project, project_revision=1,
            data_source_id=foreign.id, document={}, artifacts=[], created_by=first["user_id"],
        ))
        with pytest.raises(IntegrityError) as failure:
            db.flush()
        assert failure.value.orig.diag.constraint_name == "fk_distillation_publication_source_tenant"
        db.rollback()


def test_runtime_role_cannot_mutate_published_handoff(isolated_postgresql):
    workspace = seed_workspace(isolated_postgresql.admin_engine)
    project_id = _create_project(isolated_postgresql, workspace)
    with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
        publication = distillation_service.publish(db, project_id, 1)
        db.commit()
        publication_id = publication.id
        source_id = publication.data_source_id
    for statement in (
        "UPDATE distillation_publications SET document = '{}'::jsonb WHERE id = :id",
        "DELETE FROM distillation_publications WHERE id = :id",
    ):
        with pytest.raises(ProgrammingError) as failure:
            with isolated_postgresql.runtime_engine.begin() as connection:
                connection.execute(text(statement), {"id": publication_id})
        assert failure.value.orig.sqlstate == "42501"
    with pytest.raises(DBAPIError) as failure:
        with isolated_postgresql.runtime_engine.begin() as connection:
            connection.execute(text("UPDATE data_sources SET is_public = true WHERE id = :id"),
                               {"id": source_id})
    assert failure.value.orig.sqlstate == "P0001"


def test_distillation_reads_and_writes_require_live_tenant_membership(isolated_postgresql):
    first = seed_workspace(isolated_postgresql.admin_engine)
    foreign = seed_workspace(isolated_postgresql.admin_engine)
    project_id = _create_project(isolated_postgresql, first)
    with tenant_session(isolated_postgresql.runtime_engine, foreign) as db:
        for operation in (
            lambda: distillation_service.project(db, project_id),
            lambda: distillation_service.publish(db, project_id, 1),
        ):
            with pytest.raises(HTTPException) as failure:
                operation()
            assert failure.value.status_code == 404
    with Session(isolated_postgresql.runtime_engine) as db:
        with pytest.raises(HTTPException) as failure:
            distillation_service.project(db, project_id)
        assert failure.value.status_code == 401
    with Session(isolated_postgresql.admin_engine) as db:
        viewer = OrganizationRole(organization_id=first["organization_id"], key="viewer", name="Viewer")
        db.add(viewer)
        db.flush()
        db.get(OrganizationMember, first["member_id"]).role_id = viewer.id
        db.commit()
    with tenant_session(isolated_postgresql.runtime_engine, first) as db:
        assert distillation_service.project(db, project_id).id == project_id
        with pytest.raises(HTTPException) as failure:
            distillation_service.publish(db, project_id, 1)
        assert failure.value.status_code == 403


def test_scenario_read_grant_lists_distillation_without_workspace_read(isolated_postgresql):
    workspace = seed_workspace(isolated_postgresql.admin_engine)
    project_id = _create_project(isolated_postgresql, workspace)
    collaborator_id = uuid4().hex
    role_id = uuid4().hex
    with Session(isolated_postgresql.admin_engine) as db:
        db.add(User(
            id=collaborator_id,
            tenant_id=workspace["tenant_id"],
            email=f"{collaborator_id}@acceptance.invalid",
            password_hash="unusable",
            status="active",
        ))
        db.add(OrganizationRole(
            id=role_id,
            organization_id=workspace["organization_id"],
            key="scenario_collaborator",
            name="Scenario collaborator",
            is_system=False,
        ))
        db.flush()
        db.add(OrganizationMember(
            organization_id=workspace["organization_id"],
            user_id=collaborator_id,
            role_id=role_id,
            status="active",
        ))
        db.add(AuthorizationGrant(
            organization_id=workspace["organization_id"],
            role_id=role_id,
            resource_type="scenario",
            resource_id=workspace["scenario_id"],
            verb="read",
            effect="allow",
        ))
        db.commit()

    collaborator = {**workspace, "user_id": collaborator_id}
    with tenant_session(isolated_postgresql.runtime_engine, collaborator) as db:
        rows = business_distillation.list_projects(
            limit=50,
            offset=0,
            scenario_id=workspace["scenario_id"],
            db=db,
        )
        assert [row.id for row in rows] == [project_id]
        with pytest.raises(HTTPException) as failure:
            distillation_service.publish(db, project_id, 1)
        assert failure.value.status_code == 403


def test_distillation_evidence_cannot_read_other_scenario_or_runtime_inputs(isolated_postgresql):
    workspace = seed_workspace(isolated_postgresql.admin_engine)
    with Session(isolated_postgresql.admin_engine) as db:
        other = DataSource(tenant_id=workspace["tenant_id"], scenario_id=workspace["other_scenario_id"],
                           name="Other scenario evidence", type="file_bucket", config={})
        runtime = DataSource(tenant_id=workspace["tenant_id"], name="Runtime attachment", type="file_bucket",
                             resource_scope="agent_runtime", config={})
        db.add_all([other, runtime])
        db.commit()
        cases = ((other.id, 422), (runtime.id, 404))
    for source_id, expected_status in cases:
        with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
            document = _document().model_copy(update={"evidence": [Evidence(
                key="source", title="Bounded evidence", kind="material", data_source_id=source_id,
            )]})
            with pytest.raises(HTTPException) as failure:
                distillation_service.create_project(db, ProjectCreate(
                    name="Rejected evidence", scenario_id=workspace["scenario_id"], document=document,
                ))
            assert failure.value.status_code == expected_status


def test_downgrade_refuses_to_destroy_existing_distillation_projects(isolated_postgresql):
    workspace = seed_workspace(isolated_postgresql.admin_engine)
    project_id = _create_project(isolated_postgresql, workspace)
    try:
        with pytest.raises(RuntimeError, match="export and archive"):
            isolated_postgresql.migrate("20260918_35", downgrade=True)
        with isolated_postgresql.admin_engine.connect() as connection:
            # Alembic commits each revision separately: the empty conversation
            # table can be removed, then the draft-owning revision refuses loss.
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20260918_36"
            assert connection.execute(text("SELECT id FROM distillation_projects WHERE id = :id"), {"id": project_id}).scalar_one() == project_id
    finally:
        isolated_postgresql.migrate(isolated_postgresql.head)


def test_advisor_reads_latest_explicit_scenario_handoff_and_rechecks_citations(isolated_postgresql):
    workspace = seed_workspace(isolated_postgresql.admin_engine)
    publications = []
    with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
        for scenario_id in (workspace["scenario_id"], workspace["other_scenario_id"], None):
            project = distillation_service.create_project(db, ProjectCreate(
                name="Handoff scope", scenario_id=scenario_id, document=_document(),
            ))
            publication = distillation_service.publish(db, project.id, 1)
            publications.append(publication)
        target_project_id = publications[0].project_id
        distillation_service.update_project(db, target_project_id, ProjectUpdate(
            name="Current handoff", scenario_id=workspace["scenario_id"], expected_revision=1, document=_document(),
        ))
        latest = distillation_service.publish(db, target_project_id, 2)
        db.commit()
        documents = distillation_service.modeling_documents(db, workspace["scenario_id"])
        assert [document["publication_id"] for document in documents] == [latest.id]
        assert documents[0]["semantic_role"] == "business_distillation_handoff"
        assert documents[0]["business_decision"] == "continue"
        for publication in [*publications, latest]:
            metadata = {
                "data_source_id": publication.data_source_id,
                "publication_id": publication.id,
                "file_content_hash": distillation_service.artifact_content(publication, "brief")["sha256"],
            }
            source = distillation_handoff_service.current_source(db, workspace["scenario_id"], metadata)
            assert (source is not None) == (publication.project_id == target_project_id)
            metadata["file_content_hash"] = "0" * 64
            assert distillation_handoff_service.current_source(db, workspace["scenario_id"], metadata) is None


def test_published_provenance_preserves_evidence_identity_after_reparse(isolated_postgresql):
    workspace = seed_workspace(isolated_postgresql.admin_engine)
    original_text = "Observed intake completed; final resolution remains unverified."
    original_hash = hashlib.sha256(original_text.encode()).hexdigest()
    with Session(isolated_postgresql.admin_engine) as db:
        source = DataSource(tenant_id=workspace["tenant_id"], scenario_id=workspace["scenario_id"],
                            name="Synthetic evidence", type="file_bucket", config={}, connector_revision=7)
        db.add(source)
        db.flush()
        file = BucketFile(data_source_id=source.id, filename="evidence.txt", stored_path="minio://acceptance/evidence",
                          parsed_text=original_text, status="parsed", content_sha256="a" * 64,
                          indexed_content_hash=original_hash, index_version="synthetic-index-v1")
        db.add(file)
        db.commit()
        source_id, file_id = source.id, file.id
    with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
        document = _document().model_copy(update={"evidence": [Evidence(
            key="material", title="Intake evidence", kind="material", data_source_id=source_id, bucket_file_id=file_id,
        )]})
        project = distillation_service.create_project(db, ProjectCreate(
            name="Frozen evidence", scenario_id=workspace["scenario_id"], document=document,
        ))
        publication = distillation_service.publish(db, project.id, 1)
        db.commit()
        publication_id = publication.id
        before = distillation_service.artifact_content(publication, "provenance")
        evidence = json.loads(before["content"])["evidence"][0]
        assert evidence["connector_revision"] == 7
        assert evidence["files"][0]["parsed_text_sha256"] == original_hash
        assert evidence["files"][0]["content_sha256"] == "a" * 64
        assert evidence["files"][0]["indexed_content_hash"] == original_hash
    with Session(isolated_postgresql.admin_engine) as db:
        db.get(DataSource, source_id).connector_revision = 8
        file = db.get(BucketFile, file_id)
        file.parsed_text = "A later parse produced a different observation."
        file.content_sha256 = "b" * 64
        file.indexed_content_hash = hashlib.sha256(file.parsed_text.encode()).hexdigest()
        db.commit()
    with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
        publication = db.get(DistillationPublication, publication_id)
        assert distillation_service.artifact_content(publication, "provenance") == before


def test_large_evidence_is_sliced_in_postgresql_without_loading_full_file(isolated_postgresql):
    workspace = seed_workspace(isolated_postgresql.admin_engine)
    with Session(isolated_postgresql.admin_engine) as db:
        source = DataSource(tenant_id=workspace["tenant_id"], scenario_id=workspace["scenario_id"],
                            name="Large synthetic evidence", type="file_bucket", config={})
        db.add(source)
        db.flush()
        file = BucketFile(data_source_id=source.id, filename="large.txt", stored_path="minio://acceptance/large",
                          parsed_text="bounded evidence " * 150_000, status="parsed", content_sha256="c" * 64)
        assert len(file.parsed_text.encode()) > 2_000_000
        db.add(file)
        db.commit()
        source_id, file_id = source.id, file.id
    document = _document().model_copy(update={"evidence": [Evidence(
        key="large", title="Large evidence", kind="material", data_source_id=source_id, bucket_file_id=file_id,
    )]})

    def reject_full_load(_target, _context):
        raise AssertionError("Bounded evidence collection loaded a complete BucketFile ORM row")

    event.listen(BucketFile, "load", reject_full_load)
    try:
        with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
            distillation_service.validate_document(db, document, workspace["scenario_id"])
            materials, databases, limitations = distillation_analysis_service._collect_materials(
                db, document, workspace["scenario_id"],
            )
            assert not databases
            snippet = materials[0]["files"][0]
            assert 0 < len(snippet["content"]) <= 12_000
            assert snippet["complete"] is False
            assert any("有界节选" in limitation for limitation in limitations)
    finally:
        event.remove(BucketFile, "load", reject_full_load)


def test_postgresql_discovery_reads_only_visible_schema_metadata(isolated_postgresql):
    runtime_url = isolated_postgresql.runtime_engine.url
    schema_name = "discovery_probe_" + uuid4().hex[:12]
    quote = isolated_postgresql.admin_engine.dialect.identifier_preparer.quote
    quoted_schema, role, database = quote(schema_name), quote(runtime_url.username), quote(runtime_url.database)
    with isolated_postgresql.admin_engine.begin() as connection:
        connection.exec_driver_sql(f"CREATE SCHEMA {quoted_schema}")
        connection.exec_driver_sql(f"CREATE TABLE {quoted_schema}.visible_result (request_id text, resolved boolean)")
        connection.exec_driver_sql(f"CREATE TABLE {quoted_schema}.hidden_result (private_value text)")
        connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA {quoted_schema} TO {role}")
        connection.exec_driver_sql(f"GRANT SELECT ON {quoted_schema}.visible_result TO {role}")
        connection.exec_driver_sql(f"ALTER ROLE {role} IN DATABASE {database} SET search_path = {quoted_schema}, pg_catalog")
    try:
        # These credentials exist only in the detached adapter input; they are
        # neither persisted to a fixture row nor serialized into a prompt.
        source = DataSource(type="postgres", config={
            "host": runtime_url.host, "port": runtime_url.port, "database": runtime_url.database,
            "user": runtime_url.username, "password": runtime_url.password,
        })
        schema = distillation_connector_service.postgres_schema(source)
        assert schema == [{"name": "visible_result", "row_count": -1, "columns": [
            {"name": "request_id", "type": "text", "pk": False},
            {"name": "resolved", "type": "boolean", "pk": False},
        ]}]
    finally:
        with isolated_postgresql.admin_engine.begin() as connection:
            connection.exec_driver_sql(f"ALTER ROLE {role} IN DATABASE {database} RESET search_path")
            connection.exec_driver_sql(f"DROP SCHEMA {quoted_schema} CASCADE")


def test_scenario_purge_reports_retained_distillation_before_database_delete(isolated_postgresql):
    workspace = seed_workspace(isolated_postgresql.admin_engine)
    _create_project(isolated_postgresql, workspace)
    with Session(isolated_postgresql.admin_engine) as db:
        scenario = db.get(BusinessScenario, workspace["scenario_id"])
        scenario.status = "retired"
        db.commit()
    with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
        plan = scenario_purge_plan_service.build_purge_plan(db, db.get(BusinessScenario, workspace["scenario_id"]))
        assert not plan.can_purge
        assert plan.retained["distillation_projects"] == 1
        assert any("业务蒸馏" in blocker for blocker in plan.blockers)


def test_external_key_scenario_is_required_and_tenant_fenced(isolated_postgresql):
    first = seed_workspace(isolated_postgresql.admin_engine)
    second = seed_workspace(isolated_postgresql.admin_engine)
    for scenario_id, constraint in (
        (None, "ck_external_api_keys_bound_active"),
        (second["scenario_id"], "fk_external_api_keys_scenario_tenant"),
    ):
        with Session(isolated_postgresql.admin_engine) as db:
            db.add(ExternalApiKey(
                tenant_id=first["tenant_id"], user_id=first["user_id"], scenario_id=scenario_id,
                name="Synthetic invalid boundary", key_prefix="synthetic", token_hint="test",
                token_hash=hashlib.sha256(uuid4().bytes).hexdigest(), scopes=["scenarios:read"],
                status="active", expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            ))
            with pytest.raises(IntegrityError) as failure:
                db.flush()
            assert failure.value.orig.diag.constraint_name == constraint
            db.rollback()


def test_published_recognition_is_not_an_executable_connector(isolated_postgresql):
    workspace = seed_workspace(isolated_postgresql.admin_engine)
    project_id = _create_project(isolated_postgresql, workspace)
    with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
        publication = distillation_service.publish(db, project_id, 1)
        db.commit()
        scenario = db.get(BusinessScenario, workspace["scenario_id"])
        assert publication.data_source_id not in {
            item["id"] for item in connector_service.list_catalog(db, scenario)
        }
        with pytest.raises(connector_service.ConnectorBindingConflictError):
            connector_service.require_connector_target(
                db, scenario, kind="data_source", connector_id=publication.data_source_id,
            )


def test_migration_rollback_revokes_scoped_keys_and_upgrade_audits_legacy_keys():
    with isolated_database() as isolated:
        workspace = seed_workspace(isolated.admin_engine)
        key_id = uuid4().hex
        with Session(isolated.admin_engine) as db:
            db.add(ExternalApiKey(
                id=key_id, tenant_id=workspace["tenant_id"], user_id=workspace["user_id"],
                scenario_id=workspace["scenario_id"], name="Synthetic rollback key",
                key_prefix="synthetic", token_hint="test", token_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
                scopes=["scenarios:read"], status="active",
                expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            ))
            db.commit()
        isolated.migrate("20260918_35", downgrade=True)
        with isolated.admin_engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20260918_35"
            assert connection.execute(text("SELECT to_regclass('public.distillation_projects')")).scalar_one() is None
        isolated.migrate("20260912_34", downgrade=True)
        with isolated.admin_engine.begin() as connection:
            status = connection.execute(text("SELECT status FROM external_api_keys WHERE id = :id"), {"id": key_id}).scalar_one()
            assert status == "revoked"
            connection.execute(text("UPDATE external_api_keys SET status = 'active', revoked_at = NULL WHERE id = :id"), {"id": key_id})
        isolated.migrate(isolated.head)
        with isolated.admin_engine.connect() as connection:
            row = connection.execute(text("SELECT status, scenario_id FROM external_api_keys WHERE id = :id"), {"id": key_id}).one()
            assert row == ("revoked", None)
            audit = connection.execute(text("SELECT details FROM external_api_key_audit_events WHERE api_key_id = :id AND event_type = 'revoked'"), {"id": key_id}).scalars().all()
            assert sorted(event["reason"] for event in audit) == ["scenario_binding_required", "scenario_binding_rollback"]
