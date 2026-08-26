from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models import (
    AssistantCompilationJob,
    BusinessScenario,
    LLMConfig,
    ScenarioModelDraftResource,
    Tenant,
    User,
)
from app.routers import assistant
from app.schemas import AssistantChatRequest
from app.services import (
    assistant_orchestrator,
    permission_service,
    release_service,
    scenario_model_compiler,
    scenario_model_draft_service,
)


class ScenarioModelDraftContinuationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        database_path = Path(self.temp_dir.name) / "draft-continuation.sqlite3"
        self.engine = create_engine(
            f"sqlite:///{database_path.as_posix()}",
            connect_args={"check_same_thread": False, "timeout": 15},
        )
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        self.db = self.factory()
        self.tenant = Tenant(
            id="tenant-draft-continuation",
            name="Draft continuation tenant",
        )
        self.user = User(
            id="user-draft-continuation",
            tenant_id=self.tenant.id,
            email="draft-continuation@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-draft-continuation",
            tenant_id=self.tenant.id,
            name="Draft continuation scenario",
            namespace="draft-continuation",
            status="draft",
        )
        self.llm = LLMConfig(
            id="llm-draft-continuation",
            tenant_id=self.tenant.id,
            name="Draft continuation model",
            provider="openai",
            base_url="https://model.example.test/v1",
            api_key="test-only",
            model="test-model",
            is_default=True,
            enabled=True,
            capabilities=["chat"],
        )
        self.db.add_all([self.tenant, self.user, self.scenario, self.llm])
        self.db.commit()
        permission_service.ensure_organization(
            self.db,
            self.tenant.id,
            owner_user_id=self.user.id,
        )
        self.db.commit()
        self.db.info["tenant_id"] = self.tenant.id
        self.db.info["user_id"] = self.user.id

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()
        self.temp_dir.cleanup()

    @staticmethod
    def _candidate(key: str, name: str, *, secret: str = "") -> dict:
        payload = {
            "key": key,
            "name": name,
            "description": f"Description for {name}",
            "properties": [],
            "evidence_refs": ["source-document:p0001"],
        }
        if secret:
            payload["api_key"] = secret
        return {
            "resource_kind": "entity",
            "resource_key": key,
            "task_id": "ontology",
            "payload": payload,
            "evidence_refs": ["source-document:p0001"],
            "validation_issues": [],
            "validation_status": "ready_for_review",
        }

    @classmethod
    def _proposal(
        cls,
        proposal_id: str,
        resources: list[tuple[str, str]],
        *,
        secret: str = "",
    ) -> dict:
        return {
            "kind": "scenario_model",
            "proposal_id": proposal_id,
            "payload": {
                "draft_candidates": [
                    cls._candidate(key, name, secret=secret)
                    for key, name in resources
                ],
                "entities": [],
                "relations": [],
                "functions": [],
                "actions": [],
                "rules": [],
                "events": [],
                "workflows": [],
                "mappings": [],
                "relation_mappings": [],
                "unresolved": [],
                "coverage": [],
                "changes": [],
                "tasks": [],
            },
        }

    def _materialize(
        self,
        proposal: dict,
        *,
        started_at: datetime,
        consumed: dict[str, int] | None = None,
    ) -> None:
        scenario_model_draft_service.materialize_draft_resources(
            self.db,
            self.scenario,
            proposal,
            source_thread_id="thread-draft-continuation",
            source_message_id="message-draft-continuation",
            compilation_job_id="job-draft-continuation",
            created_by_user_id=self.user.id,
            lineage_started_at=started_at,
            consumed_draft_revisions=consumed,
        )
        self.db.commit()

    def _row(self, proposal_id: str, resource_key: str) -> ScenarioModelDraftResource:
        return self.db.scalars(
            select(ScenarioModelDraftResource).where(
                ScenarioModelDraftResource.proposal_id == proposal_id,
                ScenarioModelDraftResource.resource_key == resource_key,
            )
        ).one()

    def _update(
        self,
        row: ScenarioModelDraftResource,
        *,
        expected_revision: int,
        payload: dict,
    ) -> ScenarioModelDraftResource:
        return scenario_model_draft_service.update_working_draft_atomic(
            self.db,
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            draft_id=row.id,
            created_by_user_id=self.user.id,
            expected_revision=expected_revision,
            payload=payload,
        )

    @staticmethod
    async def _consume(response) -> str:
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(
                chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
            )
        return "".join(chunks)

    def _wait_for_terminal_job(
        self,
        *,
        expected_count: int = 1,
        timeout: float = 10.0,
    ) -> AssistantCompilationJob:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.db.expire_all()
            jobs = list(self.db.scalars(
                select(AssistantCompilationJob)
                .order_by(
                    AssistantCompilationJob.created_at.desc(),
                    AssistantCompilationJob.id.desc(),
                )
            ).all())
            if (
                len(jobs) >= expected_count
                and all(job.status in {"succeeded", "failed"} for job in jobs)
            ):
                return jobs[0]
            time.sleep(0.01)
        self.fail("background compilation did not reach a terminal state")

    @staticmethod
    def _compiled_payload() -> dict:
        return {
            "schema_version": "scenario_model.v1",
            "source_manifest": [],
            "entities": [],
            "relations": [],
            "functions": [],
            "actions": [],
            "rules": [],
            "events": [],
            "workflows": [],
            "mappings": [],
            "relation_mappings": [],
            "unresolved": [],
            "coverage": [],
            "coverage_summary": {
                "total": 0,
                "modeled": 0,
                "context": 0,
                "irrelevant": 0,
                "ambiguous": 0,
            },
            "changes": [],
            "fingerprint": "continuation-compiled-result",
        }

    def test_active_context_is_exact_sanitized_revisioned_and_fingerprinted(self) -> None:
        started_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        proposal = self._proposal(
            "proposal-working-context",
            [("entity.project", "Project")],
            secret="source-secret-must-not-leak",
        )
        self._materialize(proposal, started_at=started_at)
        row = self._row(proposal["proposal_id"], "entity.project")
        row = self._update(
            row,
            expected_revision=0,
            payload={
                "key": "entity.project",
                "name": "User working revision one",
                "description": "Exact user-authored definition",
                "properties": [{"name": "project_code", "data_type": "string"}],
                "nested": {"password": "working-secret-must-not-leak"},
            },
        )
        self.db.commit()

        active = scenario_model_draft_service.active_working_draft_context(
            self.db,
            self.scenario,
        )
        self.assertEqual(len(active), 1)
        item = active[0]
        self.assertEqual(item["draft_id"], row.id)
        self.assertEqual(item["proposal_id"], proposal["proposal_id"])
        self.assertEqual(item["task_id"], "ontology")
        self.assertEqual(item["resource_kind"], "entity")
        self.assertEqual(item["resource_key"], "entity.project")
        self.assertEqual(item["revision"], 1)
        self.assertEqual(item["draft_status"], "needs_validation")
        self.assertEqual(item["source_thread_id"], "thread-draft-continuation")
        self.assertEqual(item["source_message_id"], "message-draft-continuation")
        self.assertEqual(item["source_refs"], ["source-document:p0001"])
        self.assertEqual(item["payload"]["name"], "User working revision one")
        self.assertEqual(
            item["payload"]["description"],
            "Exact user-authored definition",
        )
        self.assertEqual(
            item["payload"]["nested"]["password"],
            {"__release_secret__": "preserve"},
        )
        serialized = json.dumps(item, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("working-secret-must-not-leak", serialized)
        self.assertNotIn("source-secret-must-not-leak", serialized)
        sanitized_source = release_service.safe_snapshot_content(row.source_payload)
        expected_source_hash = hashlib.sha256(json.dumps(
            sanitized_source,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        self.assertEqual(item["source_payload_hash"], expected_source_hash)

        first_context = scenario_model_compiler.prepare_compilation_context(
            self.db,
            self.scenario,
        )
        self.assertEqual(first_context["working_drafts"], active)
        self.assertEqual(
            first_context["consumed_draft_revisions"],
            {row.id: 1},
        )

        row = self._update(
            row,
            expected_revision=1,
            payload={
                **row.payload,
                "name": "User working revision two",
            },
        )
        self.db.commit()
        second_context = scenario_model_compiler.prepare_compilation_context(
            self.db,
            self.scenario,
        )
        self.assertNotEqual(first_context["fingerprint"], second_context["fingerprint"])
        self.assertEqual(
            second_context["consumed_draft_revisions"],
            {row.id: 2},
        )
        self.assertEqual(
            second_context["working_drafts"][0]["payload"]["name"],
            "User working revision two",
        )

        scenario_model_draft_service.resolve_draft_atomic(
            self.db,
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            draft_id=row.id,
            created_by_user_id=self.user.id,
            expected_revision=2,
            resolved_resource_id="formal-project-entity",
        )
        self.db.commit()
        empty_context = scenario_model_compiler.prepare_compilation_context(
            self.db,
            self.scenario,
        )
        self.assertEqual(empty_context["working_drafts"], [])
        self.assertEqual(empty_context["consumed_draft_revisions"], {})

    def test_successor_consumes_exact_revision_but_preserves_concurrent_patch(self) -> None:
        first_started = datetime.now(timezone.utc) - timedelta(minutes=2)
        first = self._proposal(
            "proposal-lineage-first",
            [
                ("entity.consumed", "Consumed old"),
                ("entity.concurrent", "Concurrent old"),
            ],
        )
        self._materialize(first, started_at=first_started)
        consumed_old = self._row(first["proposal_id"], "entity.consumed")
        concurrent_old = self._row(first["proposal_id"], "entity.concurrent")
        for row in (consumed_old, concurrent_old):
            self._update(
                row,
                expected_revision=0,
                payload={**row.payload, "name": f"Working {row.resource_key}"},
            )
        self.db.commit()

        compilation_snapshot = (
            scenario_model_draft_service.active_working_draft_context(
                self.db,
                self.scenario,
            )
        )
        consumed_revisions = {
            item["draft_id"]: item["revision"]
            for item in compilation_snapshot
        }
        self.assertEqual(
            consumed_revisions,
            {consumed_old.id: 1, concurrent_old.id: 1},
        )

        concurrent_old = self._update(
            concurrent_old,
            expected_revision=1,
            payload={**concurrent_old.payload, "name": "Concurrent edit after claim"},
        )
        self.db.commit()
        successor = self._proposal(
            "proposal-lineage-successor",
            [
                ("entity.consumed", "Consumed successor"),
                ("entity.concurrent", "Concurrent successor"),
            ],
        )
        self._materialize(
            successor,
            started_at=datetime.now(timezone.utc),
            consumed=consumed_revisions,
        )

        self.db.expire_all()
        consumed_old = self._row(first["proposal_id"], "entity.consumed")
        concurrent_old = self._row(first["proposal_id"], "entity.concurrent")
        consumed_successor = self._row(
            successor["proposal_id"],
            "entity.consumed",
        )
        concurrent_successor = self._row(
            successor["proposal_id"],
            "entity.concurrent",
        )
        self.assertEqual(consumed_old.draft_status, "superseded")
        self.assertEqual(
            consumed_old.superseded_by_proposal_id,
            successor["proposal_id"],
        )
        self.assertEqual(consumed_successor.predecessor_draft_id, consumed_old.id)
        self.assertEqual(consumed_successor.predecessor_revision, 1)
        self.assertNotEqual(consumed_successor.draft_status, "superseded")

        self.assertEqual(concurrent_old.revision, 2)
        self.assertEqual(concurrent_old.draft_status, "needs_validation")
        self.assertEqual(concurrent_old.payload["name"], "Concurrent edit after claim")
        self.assertEqual(concurrent_old.superseded_by_proposal_id, "")
        self.assertEqual(concurrent_successor.draft_status, "superseded")
        self.assertEqual(
            concurrent_successor.superseded_by_proposal_id,
            first["proposal_id"],
        )
        self.assertIn(
            "CONSUMED_DRAFT_CHANGED_DURING_COMPILATION",
            {
                issue["code"]
                for issue in concurrent_successor.validation_issues
            },
        )
        active = scenario_model_draft_service.active_working_draft_context(
            self.db,
            self.scenario,
        )
        by_key = {item["resource_key"]: item for item in active}
        self.assertEqual(
            by_key["entity.concurrent"]["draft_id"],
            concurrent_old.id,
        )
        self.assertEqual(
            by_key["entity.consumed"]["draft_id"],
            consumed_successor.id,
        )

    def test_key_drift_keeps_only_the_provenance_linked_successor_active(self) -> None:
        original = self._proposal(
            "proposal-key-drift-original",
            [("entity.original_key", "Original name")],
        )
        self._materialize(
            original,
            started_at=datetime.now(timezone.utc) - timedelta(minutes=2),
        )
        predecessor = self._row(
            original["proposal_id"],
            "entity.original_key",
        )
        with self.assertRaises(ValueError):
            self._update(
                predecessor,
                expected_revision=0,
                payload={
                    **predecessor.payload,
                    "key": "entity.user_renamed_key",
                    "name": "Rejected key mutation",
                },
            )
        predecessor = self._update(
            predecessor,
            expected_revision=0,
            payload={
                **predecessor.payload,
                "name": "User-renamed model object",
            },
        )
        self.db.commit()
        frozen = scenario_model_compiler.prepare_compilation_context(
            self.db,
            self.scenario,
        )
        self.assertEqual(
            frozen["consumed_draft_revisions"],
            {predecessor.id: 1},
        )

        successor = self._proposal(
            "proposal-key-drift-successor",
            [("entity.compiler_canonical_key", "Compiler canonical name")],
        )
        source_ref = f"working-draft:{predecessor.id}:r1:p0001"
        candidate = successor["payload"]["draft_candidates"][0]
        candidate["evidence_refs"] = [source_ref]
        candidate["payload"]["evidence_refs"] = [source_ref]
        self._materialize(
            successor,
            started_at=datetime.now(timezone.utc),
            consumed=frozen["consumed_draft_revisions"],
        )

        self.db.expire_all()
        predecessor = self._row(
            original["proposal_id"],
            "entity.original_key",
        )
        successor_row = self._row(
            successor["proposal_id"],
            "entity.compiler_canonical_key",
        )
        self.assertEqual(predecessor.draft_status, "superseded")
        self.assertEqual(
            predecessor.superseded_by_proposal_id,
            successor["proposal_id"],
        )
        self.assertEqual(successor_row.predecessor_draft_id, predecessor.id)
        self.assertEqual(successor_row.predecessor_revision, 1)

        next_context = scenario_model_compiler.prepare_compilation_context(
            self.db,
            self.scenario,
        )
        self.assertEqual(
            next_context["consumed_draft_revisions"],
            {successor_row.id: 0},
        )
        self.assertEqual(len(next_context["working_drafts"]), 1)
        self.assertEqual(
            next_context["working_drafts"][0]["resource_key"],
            "entity.compiler_canonical_key",
        )
        serialized = json.dumps(
            next_context["working_drafts"],
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertNotIn("entity.original_key", serialized)
        self.assertNotIn("User-renamed model object", serialized)

    def test_entity_and_expanded_property_use_their_own_lineage_refs(self) -> None:
        started_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        original = self._proposal(
            "proposal-parent-child-original",
            [("entity.parent_old", "Parent old")],
        )
        original_candidate = original["payload"]["draft_candidates"][0]
        original_candidate["payload"]["properties"] = [{
            "key": "stable_property",
            "name": "Stable property",
            "type": "string",
        }]
        self._materialize(original, started_at=started_at)
        old_entity = self._row(
            original["proposal_id"],
            "entity.parent_old",
        )
        old_property = self._row(
            original["proposal_id"],
            "entity.parent_old:property:stable_property",
        )

        entity_source_id = f"working-draft:{old_entity.id}:r0"
        property_source_id = f"working-draft:{old_property.id}:r0"
        entity_ref = f"{entity_source_id}:p0001"
        property_ref = f"{property_source_id}:p0001"
        successor = self._proposal(
            "proposal-parent-child-successor",
            [("entity.parent_renamed", "Parent renamed")],
        )
        successor_candidate = successor["payload"]["draft_candidates"][0]
        successor_candidate["payload"]["properties"] = [{
            "key": "stable_property",
            "name": "Stable property",
            "type": "string",
        }]
        # Deliberately put the property ref first. Matching must use manifest
        # kind/key metadata, never the first working-draft ref in the list.
        successor_candidate["evidence_refs"] = [property_ref, entity_ref]
        successor_candidate["payload"]["evidence_refs"] = [
            property_ref,
            entity_ref,
        ]
        successor["payload"]["source_manifest"] = [
            {
                "source_id": property_source_id,
                "source_kind": "working_draft",
                "resource_kind": "property",
                "resource_key": old_property.resource_key,
                "draft_id": old_property.id,
                "revision": 0,
            },
            {
                "source_id": entity_source_id,
                "source_kind": "working_draft",
                "resource_kind": "entity",
                "resource_key": old_entity.resource_key,
                "draft_id": old_entity.id,
                "revision": 0,
            },
        ]
        self._materialize(
            successor,
            started_at=started_at + timedelta(minutes=1),
            consumed={old_entity.id: 0, old_property.id: 0},
        )

        self.db.expire_all()
        new_entity = self._row(
            successor["proposal_id"],
            "entity.parent_renamed",
        )
        new_property = self._row(
            successor["proposal_id"],
            "entity.parent_renamed:property:stable_property",
        )
        self.assertEqual(new_entity.predecessor_draft_id, old_entity.id)
        self.assertEqual(new_property.predecessor_draft_id, old_property.id)
        self.assertEqual(new_entity.source_refs, [entity_ref])
        self.assertEqual(new_property.source_refs, [property_ref])
        self.assertEqual(
            self._row(original["proposal_id"], old_entity.resource_key).draft_status,
            "superseded",
        )
        self.assertEqual(
            self._row(original["proposal_id"], old_property.resource_key).draft_status,
            "superseded",
        )

    def test_lineage_ref_survives_more_than_one_hundred_ordinary_refs(self) -> None:
        original = self._proposal(
            "proposal-source-ref-bound-original",
            [("entity.source_ref_old", "Source ref old")],
        )
        self._materialize(
            original,
            started_at=datetime.now(timezone.utc) - timedelta(minutes=2),
        )
        predecessor = self._row(
            original["proposal_id"],
            "entity.source_ref_old",
        )
        predecessor = self._update(
            predecessor,
            expected_revision=0,
            payload={**predecessor.payload, "name": "Source ref working"},
        )
        self.db.commit()
        successor = self._proposal(
            "proposal-source-ref-bound-successor",
            [("entity.source_ref_new", "Source ref new")],
        )
        ordinary_refs = [f"attachment:p{index:04d}" for index in range(1, 102)]
        lineage_ref = f"working-draft:{predecessor.id}:r1:p0001"
        all_refs = [*ordinary_refs, lineage_ref]
        candidate = successor["payload"]["draft_candidates"][0]
        candidate["evidence_refs"] = all_refs
        candidate["payload"]["evidence_refs"] = all_refs
        self._materialize(
            successor,
            started_at=datetime.now(timezone.utc),
            consumed={predecessor.id: 1},
        )

        self.db.expire_all()
        predecessor = self._row(
            original["proposal_id"],
            "entity.source_ref_old",
        )
        successor_row = self._row(
            successor["proposal_id"],
            "entity.source_ref_new",
        )
        self.assertEqual(predecessor.draft_status, "superseded")
        self.assertEqual(successor_row.predecessor_draft_id, predecessor.id)
        self.assertIn(lineage_ref, successor_row.source_refs)
        self.assertEqual(len(successor_row.source_refs), 101)
        self.assertIn(ordinary_refs[99], successor_row.source_refs)
        self.assertNotIn(ordinary_refs[100], successor_row.source_refs)
        self.assertIn(
            "SOURCE_REFS_TRUNCATED",
            {issue["code"] for issue in successor_row.validation_issues},
        )

    def test_missing_lineage_ref_keeps_both_renamed_drafts_visible(self) -> None:
        original = self._proposal(
            "proposal-implicit-lineage-original",
            [("entity.implicit_old", "Original business name")],
        )
        self._materialize(
            original,
            started_at=datetime.now(timezone.utc) - timedelta(minutes=2),
        )
        predecessor = self._row(
            original["proposal_id"],
            "entity.implicit_old",
        )
        predecessor = self._update(
            predecessor,
            expected_revision=0,
            payload={**predecessor.payload, "name": "User changed business name"},
        )
        self.db.commit()
        successor = self._proposal(
            "proposal-implicit-lineage-successor",
            [("entity.ai_changed_key", "AI changed name again")],
        )
        candidate = successor["payload"]["draft_candidates"][0]
        candidate["evidence_refs"] = ["attachment:p0001", "user-request:p0001"]
        candidate["payload"]["evidence_refs"] = candidate["evidence_refs"]
        self._materialize(
            successor,
            started_at=datetime.now(timezone.utc),
            consumed={predecessor.id: 1},
        )

        self.db.expire_all()
        predecessor = self._row(
            original["proposal_id"],
            "entity.implicit_old",
        )
        successor_row = self._row(
            successor["proposal_id"],
            "entity.ai_changed_key",
        )
        active = scenario_model_draft_service.active_working_draft_context(
            self.db,
            self.scenario,
        )
        active_ids = {item["draft_id"] for item in active}
        self.assertEqual(
            active_ids.intersection({predecessor.id, successor_row.id}),
            {predecessor.id, successor_row.id},
        )
        self.assertEqual(predecessor.draft_status, "needs_validation")
        self.assertEqual(successor_row.draft_status, "needs_attention")
        self.assertFalse(successor_row.predecessor_draft_id)
        self.assertIn(
            "AMBIGUOUS_WORKING_DRAFT_LINEAGE",
            {issue["code"] for issue in predecessor.validation_issues},
        )
        self.assertIn(
            "AMBIGUOUS_WORKING_DRAFT_SUCCESSOR",
            {issue["code"] for issue in successor_row.validation_issues},
        )
        self.assertIn(successor_row.id, active_ids)

    def test_same_kind_new_resource_does_not_replace_existing_draft(self) -> None:
        started_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        original = self._proposal(
            "proposal-customer-original",
            [("entity.customer", "Customer")],
        )
        self._materialize(original, started_at=started_at)
        customer = self._row(original["proposal_id"], "entity.customer")

        addition = self._proposal(
            "proposal-order-addition",
            [("entity.order", "Order")],
        )
        candidate = addition["payload"]["draft_candidates"][0]
        candidate["evidence_refs"] = ["user-request:p0001"]
        candidate["payload"]["evidence_refs"] = ["user-request:p0001"]
        self._materialize(
            addition,
            started_at=started_at + timedelta(minutes=1),
            consumed={customer.id: 0},
        )

        self.db.expire_all()
        customer = self._row(original["proposal_id"], "entity.customer")
        order = self._row(addition["proposal_id"], "entity.order")
        active = scenario_model_draft_service.active_working_draft_context(
            self.db,
            self.scenario,
        )
        self.assertEqual(
            {item["resource_key"] for item in active},
            {"entity.customer", "entity.order"},
        )
        self.assertEqual(customer.draft_status, "needs_attention")
        self.assertEqual(order.draft_status, "needs_attention")
        self.assertFalse(order.predecessor_draft_id)
        self.assertFalse(customer.superseded_by_proposal_id)

    def test_concurrent_successors_are_ordered_by_job_start_not_completion(self) -> None:
        base_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        for index, completion_order in enumerate(
            ("earlier_then_later", "later_then_earlier"),
            1,
        ):
            with self.subTest(completion_order=completion_order):
                resource_key = f"entity.started_at_wins_{index}"
                original = self._proposal(
                    f"proposal-start-order-original-{index}",
                    [(resource_key, "Original")],
                )
                self._materialize(
                    original,
                    started_at=base_time,
                )
                predecessor = self._row(original["proposal_id"], resource_key)
                predecessor = self._update(
                    predecessor,
                    expected_revision=0,
                    payload={**predecessor.payload, "name": "Working revision"},
                )
                self.db.commit()
                consumed = {predecessor.id: 1}
                source_ref = f"working-draft:{predecessor.id}:r1:p0001"

                earlier = self._proposal(
                    f"proposal-start-order-earlier-{index}",
                    [(resource_key, "Earlier job result")],
                )
                later = self._proposal(
                    f"proposal-start-order-later-{index}",
                    [(resource_key, "Later job result")],
                )
                for successor in (earlier, later):
                    candidate = successor["payload"]["draft_candidates"][0]
                    candidate["evidence_refs"] = [source_ref]
                    candidate["payload"]["evidence_refs"] = [source_ref]
                started = {
                    earlier["proposal_id"]: base_time + timedelta(minutes=1),
                    later["proposal_id"]: base_time + timedelta(minutes=2),
                }
                by_name = {"earlier": earlier, "later": later}
                order = (
                    ("earlier", "later")
                    if completion_order == "earlier_then_later"
                    else ("later", "earlier")
                )
                for name in order:
                    successor = by_name[name]
                    self._materialize(
                        successor,
                        started_at=started[successor["proposal_id"]],
                        consumed=consumed,
                    )

                self.db.expire_all()
                earlier_row = self._row(earlier["proposal_id"], resource_key)
                later_row = self._row(later["proposal_id"], resource_key)
                predecessor = self._row(original["proposal_id"], resource_key)
                self.assertEqual(earlier_row.draft_status, "superseded")
                self.assertEqual(
                    earlier_row.superseded_by_proposal_id,
                    later["proposal_id"],
                )
                self.assertNotEqual(later_row.draft_status, "superseded")
                self.assertEqual(
                    predecessor.superseded_by_proposal_id,
                    later["proposal_id"],
                )
                active = scenario_model_draft_service.active_working_draft_context(
                    self.db,
                    self.scenario,
                )
                winners = [
                    item for item in active
                    if item["resource_key"] == resource_key
                ]
                self.assertEqual(len(winners), 1)
                self.assertEqual(winners[0]["draft_id"], later_row.id)

    def test_two_sessions_serialize_same_predecessor_and_new_identity(self) -> None:
        base_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        original = self._proposal(
            "proposal-concurrent-original",
            [("entity.concurrent_original", "Concurrent original")],
        )
        self._materialize(original, started_at=base_time)
        predecessor = self._row(
            original["proposal_id"],
            "entity.concurrent_original",
        )
        source_ref = f"working-draft:{predecessor.id}:r0:p0001"
        earlier = self._proposal(
            "proposal-concurrent-earlier",
            [("entity.concurrent_successor", "Concurrent successor")],
        )
        later = self._proposal(
            "proposal-concurrent-later",
            [("entity.concurrent_successor", "Concurrent successor")],
        )
        for proposal in (earlier, later):
            candidate = proposal["payload"]["draft_candidates"][0]
            candidate["evidence_refs"] = [source_ref]
            candidate["payload"]["evidence_refs"] = [source_ref]
        self.db.commit()

        start_barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def materialize(proposal: dict, started_at: datetime) -> None:
            session = self.factory()
            session.info["tenant_id"] = self.tenant.id
            session.info["user_id"] = self.user.id
            scenario_ref = BusinessScenario(
                id=self.scenario.id,
                tenant_id=self.tenant.id,
                name="Concurrent scenario reference",
                namespace="concurrent-scenario-reference",
                status="draft",
            )
            try:
                start_barrier.wait(timeout=5)
                scenario_model_draft_service.materialize_draft_resources(
                    session,
                    scenario_ref,
                    copy.deepcopy(proposal),
                    source_thread_id="thread-concurrent-lineage",
                    source_message_id=f"message-{proposal['proposal_id']}",
                    compilation_job_id=f"job-{proposal['proposal_id']}",
                    created_by_user_id=self.user.id,
                    lineage_started_at=started_at,
                    consumed_draft_revisions={predecessor.id: 0},
                )
                session.commit()
            except BaseException as exc:  # noqa: BLE001
                session.rollback()
                errors.append(exc)
            finally:
                session.close()

        earlier_thread = threading.Thread(
            target=materialize,
            args=(earlier, base_time + timedelta(minutes=1)),
            name="earlier-lineage-job",
        )
        later_thread = threading.Thread(
            target=materialize,
            args=(later, base_time + timedelta(minutes=2)),
            name="later-lineage-job",
        )
        earlier_thread.start()
        later_thread.start()
        earlier_thread.join(timeout=20)
        later_thread.join(timeout=20)

        self.assertFalse(earlier_thread.is_alive())
        self.assertFalse(later_thread.is_alive())
        self.assertEqual(errors, [])
        self.db.expire_all()
        earlier_row = self._row(
            earlier["proposal_id"],
            "entity.concurrent_successor",
        )
        later_row = self._row(
            later["proposal_id"],
            "entity.concurrent_successor",
        )
        predecessor = self._row(
            original["proposal_id"],
            "entity.concurrent_original",
        )
        self.assertEqual(earlier_row.draft_status, "superseded")
        self.assertEqual(
            earlier_row.superseded_by_proposal_id,
            later["proposal_id"],
        )
        self.assertNotEqual(later_row.draft_status, "superseded")
        self.assertEqual(
            predecessor.superseded_by_proposal_id,
            later["proposal_id"],
        )
        active = scenario_model_draft_service.active_working_draft_context(
            self.db,
            self.scenario,
        )
        winners = [
            item for item in active
            if item["resource_key"] == "entity.concurrent_successor"
        ]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["proposal_id"], later["proposal_id"])

    def test_late_successor_cannot_revive_resolved_or_applied_lineage(self) -> None:
        base_time = datetime.now(timezone.utc) - timedelta(minutes=4)
        for index, decision in enumerate(("resolved", "applied"), 1):
            with self.subTest(decision=decision):
                resource_key = f"entity.closed_lineage_{index}"
                original = self._proposal(
                    f"proposal-closed-original-{index}",
                    [(resource_key, "Closed original")],
                )
                self._materialize(original, started_at=base_time)
                predecessor = self._row(original["proposal_id"], resource_key)
                predecessor = self._update(
                    predecessor,
                    expected_revision=0,
                    payload={**predecessor.payload, "name": "Closed working"},
                )
                self.db.commit()
                consumed = {predecessor.id: 1}
                source_ref = f"working-draft:{predecessor.id}:r1:p0001"
                decided = self._proposal(
                    f"proposal-closed-decided-{index}",
                    [(resource_key, "Decided successor")],
                )
                decided_candidate = decided["payload"]["draft_candidates"][0]
                decided_candidate["evidence_refs"] = [source_ref]
                decided_candidate["payload"]["evidence_refs"] = [source_ref]
                self._materialize(
                    decided,
                    started_at=base_time + timedelta(minutes=1),
                    consumed=consumed,
                )
                decided_row = self._row(decided["proposal_id"], resource_key)
                if decision == "resolved":
                    scenario_model_draft_service.resolve_draft_atomic(
                        self.db,
                        tenant_id=self.tenant.id,
                        scenario_id=self.scenario.id,
                        draft_id=decided_row.id,
                        created_by_user_id=self.user.id,
                        expected_revision=0,
                        resolved_resource_id=f"formal-closed-{index}",
                    )
                else:
                    scenario_model_draft_service.mark_task_outcome(
                        self.db,
                        tenant_id=self.tenant.id,
                        scenario_id=self.scenario.id,
                        proposal_id=decided["proposal_id"],
                        task_id="ontology",
                        created_by_user_id=self.user.id,
                        task_status="applied",
                        applied_change_keys=[resource_key],
                    )
                self.db.commit()

                late = self._proposal(
                    f"proposal-closed-late-{index}",
                    [(resource_key, "Late result")],
                )
                late_candidate = late["payload"]["draft_candidates"][0]
                late_candidate["evidence_refs"] = [source_ref]
                late_candidate["payload"]["evidence_refs"] = [source_ref]
                # This job started before the user decision but finishes after it.
                self._materialize(
                    late,
                    started_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                    consumed=consumed,
                )

                self.db.expire_all()
                decided_row = self._row(decided["proposal_id"], resource_key)
                late_row = self._row(late["proposal_id"], resource_key)
                self.assertEqual(decided_row.draft_status, decision)
                self.assertEqual(late_row.draft_status, "superseded")
                self.assertEqual(
                    late_row.superseded_by_proposal_id,
                    decided["proposal_id"],
                )
                self.assertIn(
                    "LINEAGE_RESOLVED_DURING_COMPILATION",
                    {issue["code"] for issue in late_row.validation_issues},
                )
                active = scenario_model_draft_service.active_working_draft_context(
                    self.db,
                    self.scenario,
                )
                self.assertNotIn(late_row.id, {item["draft_id"] for item in active})

    def test_natural_language_continuation_requires_an_active_draft(self) -> None:
        proposal = self._proposal(
            "proposal-natural-continuation",
            [("entity.rule_target", "Rule target")],
        )
        self._materialize(
            proposal,
            started_at=datetime.now(timezone.utc) - timedelta(seconds=30),
        )
        row = self._row(proposal["proposal_id"], "entity.rule_target")
        row = self._update(
            row,
            expected_revision=0,
            payload={**row.payload, "name": "User-edited rule target"},
        )
        self.db.commit()

        continuation = assistant_orchestrator.AssistantSemanticDecision(
            goal="continue_work",
            scope="scenario_model",
            confidence="high",
            reason="继续当前活动草稿",
        )
        self.assertEqual(
            assistant_orchestrator.route_assistant_decision(
                continuation,
                has_active_model_drafts=True,
            ).intent,
            "scenario_model",
        )

        scenario_model_draft_service.resolve_draft_atomic(
            self.db,
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            draft_id=row.id,
            created_by_user_id=self.user.id,
            expected_revision=1,
            resolved_resource_id="resolved-rule-target",
        )
        self.db.commit()
        self.assertNotEqual(
            assistant_orchestrator.route_assistant_decision(
                continuation,
                has_active_model_drafts=False,
            ).intent,
            "scenario_model",
        )

    def test_sync_and_stream_compile_the_exact_active_revision(self) -> None:
        proposal = self._proposal(
            "proposal-transport-continuation",
            [("entity.transport", "Transport source")],
            secret="source-transport-secret",
        )
        self._materialize(
            proposal,
            started_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        row = self._row(proposal["proposal_id"], "entity.transport")
        row = self._update(
            row,
            expected_revision=0,
            payload={
                **row.payload,
                "name": "Sync working revision",
                "api_key": "sync-working-secret",
            },
        )
        self.db.commit()
        first_expected = scenario_model_compiler.prepare_compilation_context(
            self.db,
            self.scenario,
        )
        captured: list[dict] = []

        def fake_compile(*_args, prepared_context=None, **kwargs):
            captured.append({
                "message": kwargs.get("message"),
                "prepared_context": copy.deepcopy(prepared_context),
            })
            return self._compiled_payload()

        continuation_plan = assistant_orchestrator.AssistantRoutePlan(
            intent="scenario_model",
            decision=assistant_orchestrator.AssistantSemanticDecision(
                goal="continue_work",
                scope="scenario_model",
                confidence="high",
                reason="测试明确续作当前活动草稿",
            ),
            source="model",
        )

        with (
            patch.object(assistant, "SessionLocal", self.factory),
            patch.object(assistant, "_llm", return_value=self.llm),
            patch.object(
                assistant,
                "_request_route_plan",
                return_value=continuation_plan,
            ),
            patch.object(
                assistant.scenario_model_compiler,
                "compile_scenario_model",
                side_effect=fake_compile,
            ),
        ):
            sync_reply = assistant.chat(
                AssistantChatRequest(
                    message="新增一个订单对象",
                    request_id="working-draft-sync-r1",
                    scenario_id=self.scenario.id,
                    path=f"/scenarios/{self.scenario.id}",
                    mode="ask",
                    draft_kind="auto",
                ),
                self.db,
            )
            self.assertEqual(sync_reply.proposal.get("kind"), "scenario_model")
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]["message"], "新增一个订单对象")
            self.assertEqual(captured[0]["prepared_context"], first_expected)

            self.db.expire_all()
            first_job = self.db.scalars(select(AssistantCompilationJob)).one()
            self.assertEqual(
                first_job.mapping_context_fingerprint,
                first_expected["fingerprint"],
            )

            row = self._row(proposal["proposal_id"], "entity.transport")
            row = self._update(
                row,
                expected_revision=1,
                payload={
                    **row.payload,
                    "name": "Stream working revision",
                    "api_key": "stream-working-secret",
                },
            )
            self.db.commit()
            second_expected = scenario_model_compiler.prepare_compilation_context(
                self.db,
                self.scenario,
            )
            self.assertNotEqual(
                first_expected["fingerprint"],
                second_expected["fingerprint"],
            )

            response = assistant.stream_chat(
                AssistantChatRequest(
                    message="修正规则草稿",
                    request_id="working-draft-stream-r2",
                    scenario_id=self.scenario.id,
                    path=f"/scenarios/{self.scenario.id}",
                    mode="ask",
                    draft_kind="auto",
                ),
                self.db,
            )
            body = asyncio.run(self._consume(response))
            terminal = self._wait_for_terminal_job(expected_count=2)

        self.assertEqual(terminal.status, "succeeded")
        self.assertIn('"type": "compilation_job"', body)
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[1]["message"], "修正规则草稿")
        self.assertEqual(captured[1]["prepared_context"], second_expected)
        working = captured[1]["prepared_context"]["working_drafts"]
        self.assertEqual(len(working), 1)
        self.assertEqual(working[0]["draft_id"], row.id)
        self.assertEqual(working[0]["revision"], 2)
        self.assertEqual(working[0]["payload"]["name"], "Stream working revision")
        self.assertEqual(working[0]["source_thread_id"], "thread-draft-continuation")
        self.assertEqual(working[0]["source_message_id"], "message-draft-continuation")
        self.assertEqual(
            working[0]["payload"]["api_key"],
            {"__release_secret__": "preserve"},
        )
        serialized = json.dumps(working, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("stream-working-secret", serialized)
        self.assertNotIn("source-transport-secret", serialized)
        self.db.expire_all()
        jobs = list(self.db.scalars(select(AssistantCompilationJob)).all())
        self.assertEqual(len(jobs), 2)
        self.assertEqual(
            {job.mapping_context_fingerprint for job in jobs},
            {first_expected["fingerprint"], second_expected["fingerprint"]},
        )
        self.assertEqual(len({job.request_fingerprint for job in jobs}), 2)


if __name__ == "__main__":
    unittest.main()
