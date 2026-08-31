"""Regression tests for permanently deleting retired business scenarios."""
from __future__ import annotations

from types import SimpleNamespace
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    ActionExecutionLog,
    Assertion,
    BusinessScenario,
    DerivationEvidence,
    ReasoningTerm,
    Tenant,
    User,
)
from app.routers import scenarios as scenarios_router
from app.services import permission_service
from app.services.auth_service import get_current_user


class ScenarioPurgeTests(unittest.TestCase):
    """Scenario purge must remove RESTRICT audit edges before their parents."""

    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        event.listen(
            self.engine,
            "connect",
            lambda connection, _record: connection.execute("PRAGMA foreign_keys=ON"),
        )
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        Base.metadata.create_all(self.engine)

        db = self.Session()
        self.tenant = Tenant(id="tenant-purge", name="永久删除测试租户")
        self.user = User(
            id="user-purge",
            tenant_id=self.tenant.id,
            email="purge-owner@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-purge",
            tenant_id=self.tenant.id,
            name="含推理证据的退役场景",
            status="retired",
        )
        db.add_all([self.tenant, self.user, self.scenario])
        db.flush()
        subject = ReasoningTerm(
            id="term-purge-subject",
            tenant_id=self.tenant.id,
            kind="literal",
            literal_value="subject",
            canonical_hash="a" * 64,
        )
        object_term = ReasoningTerm(
            id="term-purge-object",
            tenant_id=self.tenant.id,
            kind="literal",
            literal_value="object",
            canonical_hash="b" * 64,
        )
        assertion = Assertion(
            id="assertion-purge",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            subject_term_id=subject.id,
            object_term_id=object_term.id,
            predicate_key="purge.evidence",
            assertion_kind="observed",
            canonical_hash="c" * 64,
        )
        action_log = ActionExecutionLog(
            id="action-log-purge",
            scenario_id=self.scenario.id,
            target_id="action-purge",
        )
        evidence = DerivationEvidence(
            id="evidence-purge",
            tenant_id=self.tenant.id,
            assertion_id=assertion.id,
            ordinal=0,
            action_execution_log_id=action_log.id,
            action_scenario_id=self.scenario.id,
            content_hash="d" * 64,
        )
        db.add_all([subject, object_term])
        db.flush()
        db.add_all([assertion, action_log])
        db.flush()
        db.add(evidence)
        permission_service.ensure_organization(
            db, self.tenant.id, owner_user_id=self.user.id
        )
        db.commit()
        db.close()

        self.app = FastAPI()
        self.app.include_router(scenarios_router.router, prefix="/api")

        def override_current_user():
            return SimpleNamespace(id=self.user.id, tenant_id=self.tenant.id)

        def override_db():
            request_db = self.Session()
            request_db.info["user_id"] = self.user.id
            request_db.info["tenant_id"] = self.tenant.id
            try:
                yield request_db
            finally:
                request_db.close()

        self.app.dependency_overrides[get_current_user] = override_current_user
        self.app.dependency_overrides[get_db] = override_db
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()

    def test_purge_deletes_evidence_before_restricted_audit_parents(self) -> None:
        plan = self.client.get(
            f"/api/scenarios/{self.scenario.id}/purge-plan"
        )
        self.assertEqual(plan.status_code, 200, plan.text)
        self.assertEqual(plan.json()["counts"]["derivation_evidence"], 1)
        self.assertTrue(plan.json()["requires_audit_confirmation"])

        response = self.client.post(
            f"/api/scenarios/{self.scenario.id}/purge",
            json={
                "expected_name": self.scenario.name,
                "confirmed": True,
                "delete_audit_history": True,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

        db = self.Session()
        try:
            self.assertIsNone(db.get(BusinessScenario, self.scenario.id))
            self.assertIsNone(db.get(DerivationEvidence, "evidence-purge"))
            self.assertIsNone(db.get(Assertion, "assertion-purge"))
            self.assertIsNone(db.get(ActionExecutionLog, "action-log-purge"))
            self.assertEqual(
                {term.id for term in db.scalars(select(ReasoningTerm)).all()},
                {"term-purge-subject", "term-purge-object"},
            )
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
