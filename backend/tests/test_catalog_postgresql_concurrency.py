"""Real PostgreSQL acceptance coverage for DatasetHead compare-and-set.

This test is deliberately opt-in: it needs the migrated PostgreSQL database
configured for the backend process and creates short-lived, uniquely scoped
catalog rows.  It never logs or otherwise exposes the configured database URL.
"""
from __future__ import annotations

import hashlib
import os
import queue
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, select, text

from app.config import get_settings
from app.database import SessionLocal, engine
from app.models import (
    BusinessScenario,
    CapabilityInvocation,
    DatasetHead,
    DatasetSchema,
    DatasetVersion,
    LogicalDataset,
    Organization,
    OrganizationMember,
    OrganizationRole,
    Tenant,
    User,
)
from app.routers import scenarios
from app.services import catalog_service


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRESQL_INTEGRATION_TESTS") != "1",
    reason="set RUN_POSTGRESQL_INTEGRATION_TESTS=1 to use the configured PostgreSQL database",
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_scenario_retirement_preserves_restricted_invocation_audit() -> None:
    """Retirement is an UPDATE, so RESTRICT audit anchors remain intact."""
    assert engine.dialect.name == "postgresql"

    tenant_id = uuid4().hex
    user_id = uuid4().hex
    organization_id = uuid4().hex
    role_id = uuid4().hex
    member_id = uuid4().hex
    scenario_id = uuid4().hex
    invocation_id = uuid4().hex
    request_id = uuid4().hex
    digest = _digest(f"scenario-retirement:{scenario_id}")

    def cleanup() -> None:
        settings = get_settings()
        cleanup_engine = engine
        owns_cleanup_engine = False
        if settings.postgresql_admin_user and settings.postgresql_admin_password:
            cleanup_engine = create_engine(
                engine.url.set(
                    username=settings.postgresql_admin_user,
                    password=settings.postgresql_admin_password,
                ),
                pool_pre_ping=True,
                connect_args={"application_name": "ontology-platform-retirement-test-cleanup"},
            )
            owns_cleanup_engine = True
        try:
            with cleanup_engine.begin() as connection:
                connection.execute(
                    delete(CapabilityInvocation).where(
                        CapabilityInvocation.id == invocation_id,
                        CapabilityInvocation.tenant_id == tenant_id,
                    )
                )
                connection.execute(
                    delete(BusinessScenario).where(
                        BusinessScenario.id == scenario_id,
                        BusinessScenario.tenant_id == tenant_id,
                    )
                )
                connection.execute(
                    delete(OrganizationMember).where(
                        OrganizationMember.id == member_id,
                        OrganizationMember.organization_id == organization_id,
                        OrganizationMember.user_id == user_id,
                    )
                )
                connection.execute(
                    delete(OrganizationRole).where(
                        OrganizationRole.id == role_id,
                        OrganizationRole.organization_id == organization_id,
                    )
                )
                connection.execute(
                    delete(Organization).where(
                        Organization.id == organization_id,
                        Organization.tenant_id == tenant_id,
                    )
                )
                connection.execute(
                    delete(User).where(User.id == user_id, User.tenant_id == tenant_id)
                )
                connection.execute(delete(Tenant).where(Tenant.id == tenant_id))
        finally:
            if owns_cleanup_engine:
                cleanup_engine.dispose()

    try:
        with SessionLocal() as db:
            db.add(Tenant(id=tenant_id, name=f"Scenario retirement {tenant_id}"))
            db.flush()
            db.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    email=f"scenario-retirement-{tenant_id}@example.test",
                    display_name="Scenario retirement",
                    password_hash="test-only-not-a-login-secret",
                    status="active",
                )
            )
            db.add(
                Organization(
                    id=organization_id,
                    tenant_id=tenant_id,
                    name="Scenario retirement",
                )
            )
            db.flush()
            db.add(
                OrganizationRole(
                    id=role_id,
                    organization_id=organization_id,
                    key="owner",
                    name="Test owner",
                    description="Ephemeral retirement test principal",
                    is_system=True,
                )
            )
            db.flush()
            db.add(
                OrganizationMember(
                    id=member_id,
                    organization_id=organization_id,
                    user_id=user_id,
                    role_id=role_id,
                    status="active",
                )
            )
            db.add(
                BusinessScenario(
                    id=scenario_id,
                    tenant_id=tenant_id,
                    name="PostgreSQL retirement contract",
                    status="active",
                )
            )
            db.flush()
            db.add(
                CapabilityInvocation(
                    id=invocation_id,
                    tenant_id=tenant_id,
                    scenario_id=scenario_id,
                    requested_by_user_id=user_id,
                    environment="dev",
                    capability_kind="function",
                    capability_key="retirement-contract",
                    definition_hash=digest,
                    deployment_fingerprint=digest,
                    data_context_fingerprint=digest,
                    correlation_id=f"retirement:{scenario_id}",
                    principal_type="user",
                    principal_id=user_id,
                    invocation_source="internal",
                    request_id=request_id,
                    input_hash=digest,
                    status="succeeded",
                    request_document={},
                    result_document={"retained": True},
                )
            )
            db.commit()

            db.info["tenant_id"] = tenant_id
            db.info["user_id"] = user_id
            result = scenarios.delete_scenario(scenario_id, db)
            assert result.message == "已退役"

        with SessionLocal() as verification_db:
            retired = verification_db.get(BusinessScenario, scenario_id)
            audit = verification_db.get(CapabilityInvocation, invocation_id)
            assert retired is not None
            assert retired.status == "retired"
            assert audit is not None
            assert audit.scenario_id == scenario_id
            assert audit.result_document == {"retained": True}
    finally:
        cleanup()


def test_dataset_head_compare_and_set_serializes_real_concurrent_writers() -> None:
    """Two transactions based on Head=A cannot both move the same Head."""
    assert engine.dialect.name == "postgresql"

    tenant_id = uuid4().hex
    user_id = uuid4().hex
    organization_id = uuid4().hex
    role_id = uuid4().hex
    member_id = uuid4().hex
    dataset_id = uuid4().hex
    schema_id = uuid4().hex
    version_a_id = uuid4().hex
    version_b_id = uuid4().hex
    version_c_id = uuid4().hex
    head_id = uuid4().hex
    waiter_application_name = f"dataset-head-cas-{uuid4().hex}"
    now = datetime.now(timezone.utc)

    lock_held = threading.Event()
    loser_entered_cas = threading.Event()
    winner_flushed = threading.Event()
    allow_winner_commit = threading.Event()
    start_barrier = threading.Barrier(2)
    outcomes: queue.Queue[tuple[str, str, str]] = queue.Queue()
    workers: list[threading.Thread] = []

    def session_context(db) -> None:
        db.info["tenant_id"] = tenant_id
        db.info["user_id"] = user_id

    def winner() -> None:
        with SessionLocal() as db:
            session_context(db)
            try:
                dataset = db.get(LogicalDataset, dataset_id)
                assert dataset is not None
                locked_head = db.execute(
                    select(DatasetHead)
                    .where(DatasetHead.id == head_id)
                    .with_for_update()
                ).scalar_one()
                assert locked_head.dataset_version_id == version_a_id
                lock_held.set()
                start_barrier.wait(timeout=10)

                moved = catalog_service.set_head(
                    db,
                    dataset,
                    "dev",
                    version_b_id,
                    expected_version_id=version_a_id,
                )
                assert moved.dataset_version_id == version_b_id
                winner_flushed.set()
                if not allow_winner_commit.wait(timeout=15):
                    raise AssertionError("timed out waiting to release the winning transaction")
                db.commit()
                outcomes.put(("winner", "success", version_b_id))
            except BaseException as exc:  # noqa: BLE001 - return thread failures to pytest.
                db.rollback()
                outcomes.put(("winner", type(exc).__name__, str(exc)))
                lock_held.set()
                winner_flushed.set()
                allow_winner_commit.set()

    def loser() -> None:
        with SessionLocal() as db:
            session_context(db)
            try:
                if not lock_held.wait(timeout=10):
                    raise AssertionError("winning transaction did not acquire the Head lock")
                dataset = db.get(LogicalDataset, dataset_id)
                assert dataset is not None
                db.execute(
                    text("SELECT set_config('application_name', :name, true)"),
                    {"name": waiter_application_name},
                )
                start_barrier.wait(timeout=10)
                loser_entered_cas.set()
                catalog_service.set_head(
                    db,
                    dataset,
                    "dev",
                    version_c_id,
                    expected_version_id=version_a_id,
                )
                db.commit()
                outcomes.put(("loser", "unexpected_success", version_c_id))
            except catalog_service.CatalogError as exc:
                db.rollback()
                outcomes.put(("loser", "stale", str(exc)))
            except BaseException as exc:  # noqa: BLE001 - return thread failures to pytest.
                db.rollback()
                outcomes.put(("loser", type(exc).__name__, str(exc)))
                loser_entered_cas.set()
                allow_winner_commit.set()

    def cleanup() -> None:
        """Delete only rows bearing this test's explicit, unique identifiers."""
        settings = get_settings()
        cleanup_engine = engine
        owns_cleanup_engine = False
        if settings.postgresql_admin_user and settings.postgresql_admin_password:
            cleanup_engine = create_engine(
                engine.url.set(
                    username=settings.postgresql_admin_user,
                    password=settings.postgresql_admin_password,
                ),
                pool_pre_ping=True,
                connect_args={"application_name": "ontology-platform-cas-test-cleanup"},
            )
            owns_cleanup_engine = True
        try:
            with cleanup_engine.begin() as connection:
                connection.execute(
                    delete(DatasetHead).where(
                        DatasetHead.id == head_id,
                        DatasetHead.dataset_id == dataset_id,
                        DatasetHead.tenant_id == tenant_id,
                    )
                )
                for version_id in (version_b_id, version_c_id, version_a_id):
                    connection.execute(
                        delete(DatasetVersion).where(
                            DatasetVersion.id == version_id,
                            DatasetVersion.dataset_id == dataset_id,
                            DatasetVersion.tenant_id == tenant_id,
                        )
                    )
                connection.execute(
                    delete(DatasetSchema).where(
                        DatasetSchema.id == schema_id,
                        DatasetSchema.dataset_id == dataset_id,
                        DatasetSchema.tenant_id == tenant_id,
                    )
                )
                connection.execute(
                    delete(LogicalDataset).where(
                        LogicalDataset.id == dataset_id,
                        LogicalDataset.tenant_id == tenant_id,
                    )
                )
                connection.execute(
                    delete(OrganizationMember).where(
                        OrganizationMember.id == member_id,
                        OrganizationMember.organization_id == organization_id,
                        OrganizationMember.user_id == user_id,
                    )
                )
                connection.execute(
                    delete(OrganizationRole).where(
                        OrganizationRole.id == role_id,
                        OrganizationRole.organization_id == organization_id,
                    )
                )
                connection.execute(
                    delete(Organization).where(
                        Organization.id == organization_id,
                        Organization.tenant_id == tenant_id,
                    )
                )
                connection.execute(
                    delete(User).where(User.id == user_id, User.tenant_id == tenant_id)
                )
                connection.execute(delete(Tenant).where(Tenant.id == tenant_id))
        finally:
            if owns_cleanup_engine:
                cleanup_engine.dispose()

    try:
        with SessionLocal() as db:
            db.add(Tenant(id=tenant_id, name=f"Dataset Head CAS {tenant_id}"))
            db.flush()
            db.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    email=f"dataset-head-cas-{tenant_id}@example.test",
                    display_name="Dataset Head CAS",
                    password_hash="test-only-not-a-login-secret",
                    status="active",
                    email_verified_at=now,
                )
            )
            db.add(
                Organization(
                    id=organization_id,
                    tenant_id=tenant_id,
                    name="Dataset Head CAS",
                )
            )
            db.flush()
            db.add(
                OrganizationRole(
                    id=role_id,
                    organization_id=organization_id,
                    key="owner",
                    name="Test owner",
                    description="Ephemeral PostgreSQL CAS test principal",
                    is_system=True,
                )
            )
            db.flush()
            db.add(
                OrganizationMember(
                    id=member_id,
                    organization_id=organization_id,
                    user_id=user_id,
                    role_id=role_id,
                    status="active",
                )
            )
            db.add(
                LogicalDataset(
                    id=dataset_id,
                    tenant_id=tenant_id,
                    key=f"postgresql.cas.{dataset_id}",
                    name="PostgreSQL DatasetHead CAS",
                    lifecycle_status="active",
                    labels={"test_scope": tenant_id},
                    created_by_user_id=user_id,
                )
            )
            db.flush()
            db.add(
                DatasetSchema(
                    id=schema_id,
                    tenant_id=tenant_id,
                    dataset_id=dataset_id,
                    schema_version=1,
                    schema_hash=_digest(f"schema:{schema_id}"),
                    compatibility="none",
                    schema_document={"test_scope": tenant_id},
                    created_by_user_id=user_id,
                )
            )
            db.flush()
            db.add(
                DatasetVersion(
                    id=version_a_id,
                    tenant_id=tenant_id,
                    dataset_id=dataset_id,
                    schema_id=schema_id,
                    version_number=1,
                    status="ready",
                    content_hash=_digest(f"version-a:{version_a_id}"),
                    manifest={"test_scope": tenant_id, "version": "A"},
                    created_by_user_id=user_id,
                    ready_at=now,
                )
            )
            db.flush()
            db.add_all(
                [
                    DatasetVersion(
                        id=version_b_id,
                        tenant_id=tenant_id,
                        dataset_id=dataset_id,
                        schema_id=schema_id,
                        version_number=2,
                        parent_version_id=version_a_id,
                        status="ready",
                        content_hash=_digest(f"version-b:{version_b_id}"),
                        manifest={"test_scope": tenant_id, "version": "B"},
                        created_by_user_id=user_id,
                        ready_at=now,
                    ),
                    DatasetVersion(
                        id=version_c_id,
                        tenant_id=tenant_id,
                        dataset_id=dataset_id,
                        schema_id=schema_id,
                        version_number=3,
                        parent_version_id=version_a_id,
                        status="ready",
                        content_hash=_digest(f"version-c:{version_c_id}"),
                        manifest={"test_scope": tenant_id, "version": "C"},
                        created_by_user_id=user_id,
                        ready_at=now,
                    ),
                ]
            )
            db.flush()
            db.add(
                DatasetHead(
                    id=head_id,
                    tenant_id=tenant_id,
                    dataset_id=dataset_id,
                    environment="dev",
                    dataset_version_id=version_a_id,
                    updated_by_user_id=user_id,
                )
            )
            db.commit()

        workers = [
            threading.Thread(target=winner, name="dataset-head-cas-winner"),
            threading.Thread(target=loser, name="dataset-head-cas-loser"),
        ]
        for worker in workers:
            worker.start()

        assert winner_flushed.wait(timeout=10)
        assert loser_entered_cas.wait(timeout=10)

        blocked_waiter_seen = False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with engine.connect() as connection:
                blocked_waiter_seen = bool(
                    connection.scalar(
                        text(
                            "SELECT EXISTS ("
                            "SELECT 1 FROM pg_stat_activity "
                            "WHERE application_name = :name "
                            "AND state = 'active' "
                            "AND wait_event_type = 'Lock'"
                            ")"
                        ),
                        {"name": waiter_application_name},
                    )
                )
            if blocked_waiter_seen:
                break
            time.sleep(0.05)

        # Release only after the loser has entered PostgreSQL and is visibly
        # waiting on the transaction that still owns the DatasetHead row lock.
        allow_winner_commit.set()
        for worker in workers:
            worker.join(timeout=15)

        assert blocked_waiter_seen, "the competing PostgreSQL transaction never waited on a lock"
        assert all(not worker.is_alive() for worker in workers)
        received = [outcomes.get(timeout=1) for _ in range(2)]
        assert len(received) == 2
        assert outcomes.empty()
        assert sum(status == "success" for _, status, _ in received) == 1
        assert sum(status == "stale" for _, status, _ in received) == 1
        stale = next(item for item in received if item[1] == "stale")
        assert stale[0] == "loser"
        assert stale[2] == "数据集 Head 已由其他操作更新，请刷新后重试"

        with SessionLocal() as db:
            final_head = db.execute(
                select(DatasetHead).where(
                    DatasetHead.id == head_id,
                    DatasetHead.dataset_id == dataset_id,
                    DatasetHead.tenant_id == tenant_id,
                )
            ).scalar_one()
            assert final_head.dataset_version_id == version_b_id
            historical_ids = set(
                db.scalars(
                    select(DatasetVersion.id).where(
                        DatasetVersion.dataset_id == dataset_id,
                        DatasetVersion.tenant_id == tenant_id,
                        DatasetVersion.id.in_([version_a_id, version_b_id, version_c_id]),
                    )
                ).all()
            )
            assert historical_ids == {version_a_id, version_b_id, version_c_id}
    finally:
        allow_winner_commit.set()
        lock_held.set()
        if start_barrier.n_waiting:
            start_barrier.abort()
        for worker in workers:
            worker.join(timeout=15)
        cleanup()
