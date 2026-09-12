"""Regression coverage for Agent-owned uploads and downward deletion."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import (
    Agent,
    AgentTurnEvent,
    AgentTurnRun,
    BusinessScenario,
    Conversation,
    ConnectorBinding,
    DataAsset,
    DataSource,
    DatasetSchema,
    DatasetVersion,
    IngestionRun,
    LogicalDataset,
    ManagedUploadRun,
    Message,
    Tenant,
    User,
)
from app.services import (
    agent_deletion_service,
    agent_turn_service,
    catalog_service,
    permission_service,
)


class AgentDeletionIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        Base.metadata.create_all(self.engine)
        with self.Session() as db:
            tenant = Tenant(id="tenant-agent-delete", name="Agent deletion")
            user = User(
                id="user-agent-delete",
                tenant_id=tenant.id,
                email="agent-delete@example.test",
                password_hash="test-only",
                status="active",
            )
            scenario = BusinessScenario(
                id="scenario-agent-delete",
                tenant_id=tenant.id,
                name="Bound scenario",
                status="draft",
            )
            agent_a = Agent(
                id="agent-delete-a",
                tenant_id=tenant.id,
                scenario_id=scenario.id,
                name="Agent A",
            )
            agent_b = Agent(
                id="agent-delete-b",
                tenant_id=tenant.id,
                scenario_id=scenario.id,
                name="Agent B",
            )
            shared_source = DataSource(
                id="source-agent-delete",
                tenant_id=tenant.id,
                name="managed uploads",
                type="file_bucket",
                resource_scope="agent_runtime",
                config={"storage_backend": "minio"},
            )
            db.add_all([tenant, user, scenario, agent_a, agent_b, shared_source])
            db.flush()
            permission_service.ensure_organization(
                db, tenant.id, owner_user_id=user.id
            )
            db.add_all(
                [
                    DataAsset(
                        id="asset-agent-a",
                        tenant_id=tenant.id,
                        owner_agent_id=agent_a.id,
                        key="agent.a.asset",
                        name="A upload",
                        kind="file",
                        usage_plane="invocation_input",
                        lifecycle_status="active",
                        labels={"catalog_purpose": "validation_asset"},
                    ),
                    DataAsset(
                        id="asset-agent-b",
                        tenant_id=tenant.id,
                        owner_agent_id=agent_b.id,
                        key="agent.b.asset",
                        name="B upload",
                        kind="file",
                        usage_plane="invocation_input",
                        lifecycle_status="active",
                        labels={"catalog_purpose": "validation_asset"},
                    ),
                    ManagedUploadRun(
                        id="upload-agent-a",
                        tenant_id=tenant.id,
                        owner_agent_id=agent_a.id,
                        requested_by_user_id=user.id,
                        data_source_id=shared_source.id,
                        idempotency_key="upload-agent-a",
                        request_fingerprint="a" * 64,
                        purpose="validation_asset",
                        filename="a.txt",
                        declared_byte_size=1,
                        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                    ),
                ]
            )
            conversation = Conversation(
                id="conversation-agent-a",
                agent_id=agent_a.id,
                created_by_user_id=user.id,
                title="A chat",
            )
            db.add(conversation)
            db.flush()
            db.add(
                Message(
                    id="message-agent-a",
                    conversation_id=conversation.id,
                    role="user",
                    content="private upload context",
                )
            )
            db.commit()

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_catalog_assets_are_scoped_to_the_requested_agent(self) -> None:
        with self.Session() as db:
            db.info.update(
                tenant_id="tenant-agent-delete", user_id="user-agent-delete"
            )
            for agent_id, expected in (
                ("agent-delete-a", {"asset-agent-a"}),
                ("agent-delete-b", {"asset-agent-b"}),
            ):
                rows = catalog_service.list_assets(
                    db,
                    usage_plane="invocation_input",
                    owner_agent_id=agent_id,
                )
                self.assertEqual({row.id for row in rows}, expected)

            # The unscoped catalog view must not leak Agent-owned validation
            # uploads into another page or Agent's data-source picker.
            self.assertEqual(
                catalog_service.list_assets(
                    db, usage_plane="invocation_input", owner_agent_id=None
                ),
                [],
            )

    def test_agent_cleanup_deletes_children_but_keeps_bound_scenario_and_other_agent(self) -> None:
        with self.Session() as db:
            agent = db.get(Agent, "agent-delete-a")
            assert agent is not None
            result = agent_deletion_service.cleanup_agent_owned_records(db, agent)
            db.commit()
            self.assertEqual(result.conversations_deleted, 1)
            self.assertEqual(result.upload_runs_deleted, 1)
            self.assertEqual(result.upload_runs_cancelled, 1)
            self.assertEqual(result.assets_deleted, 1)
            self.assertEqual(result.assets_retired, 1)

            self.assertIsNone(db.get(Agent, "agent-delete-a"))
            self.assertIsNone(db.get(Conversation, "conversation-agent-a"))
            self.assertIsNone(db.get(Message, "message-agent-a"))
            # PostgreSQL keeps immutable/audit rows.  They are removed from
            # active ownership and all readable object pointers are cleared.
            retired_asset = db.get(DataAsset, "asset-agent-a")
            self.assertIsNotNone(retired_asset)
            assert retired_asset is not None
            self.assertEqual(retired_asset.lifecycle_status, "retired")
            self.assertIsNone(retired_asset.owner_agent_id)
            cancelled_upload = db.get(ManagedUploadRun, "upload-agent-a")
            self.assertIsNotNone(cancelled_upload)
            assert cancelled_upload is not None
            self.assertEqual(cancelled_upload.status, "cancelled")
            self.assertEqual(cancelled_upload.error_code, "agent_deleted")
            self.assertIsNone(cancelled_upload.owner_agent_id)
            self.assertIsNone(cancelled_upload.asset_id)
            self.assertIsNone(cancelled_upload.asset_version_id)
            self.assertIsNone(cancelled_upload.bucket_file_id)

            # Downward-only invariant: deleting Agent A cannot remove Agent B,
            # the shared source, or the scenario to which both were bound.
            self.assertIsNotNone(db.get(Agent, "agent-delete-b"))
            self.assertIsNotNone(db.get(DataAsset, "asset-agent-b"))
            self.assertIsNotNone(db.get(DataSource, "source-agent-delete"))
            self.assertIsNotNone(db.get(BusinessScenario, "scenario-agent-delete"))

    def test_agent_cleanup_keeps_same_source_binding_in_another_scenario(self) -> None:
        with self.Session() as db:
            agent = db.get(Agent, "agent-delete-a")
            scenario = db.get(BusinessScenario, "scenario-agent-delete")
            assert agent is not None
            assert scenario is not None
            other_scenario = BusinessScenario(
                id="scenario-agent-delete-other",
                tenant_id=scenario.tenant_id,
                name="Other scenario",
                status="draft",
            )
            source = DataSource(
                id="source-agent-delete-owned",
                tenant_id=scenario.tenant_id,
                scenario_id=scenario.id,
                resource_scope="agent_runtime",
                owner_agent_id=agent.id,
                name="Agent-owned runtime source",
                type="postgres",
                config={},
            )
            binding_key = f"agent:{agent.id}:database:{source.id}"
            current_binding = ConnectorBinding(
                id="binding-agent-delete-current",
                tenant_id=scenario.tenant_id,
                scenario_id=scenario.id,
                binding_key=binding_key,
                connector_kind="data_source",
                connector_id=source.id,
            )
            other_binding = ConnectorBinding(
                id="binding-agent-delete-other",
                tenant_id=scenario.tenant_id,
                scenario_id=other_scenario.id,
                binding_key=binding_key,
                connector_kind="data_source",
                connector_id=source.id,
            )
            db.add_all([other_scenario, source, current_binding, other_binding])
            db.commit()

            agent_deletion_service.cleanup_agent_owned_records(db, agent)
            db.commit()

            self.assertIsNone(db.get(ConnectorBinding, current_binding.id))
            self.assertIsNotNone(db.get(ConnectorBinding, other_binding.id))
            retained_source = db.get(DataSource, source.id)
            self.assertIsNotNone(retained_source)
            assert retained_source is not None
            self.assertIsNone(retained_source.owner_agent_id)

    def test_agent_cleanup_retires_owned_validation_dataset_and_cancels_job(self) -> None:
        with self.Session() as db:
            agent = db.get(Agent, "agent-delete-a")
            assert agent is not None
            dataset = LogicalDataset(
                id="validation-dataset-a",
                tenant_id=agent.tenant_id,
                key="validation.agent.a",
                name="Agent A validation data",
                usage_plane="invocation_input",
                lifecycle_status="active",
                labels={
                    "catalog_purpose": "validation_dataset",
                    "owner_agent_id": agent.id,
                    "input_hash": "a" * 64,
                },
            )
            schema = DatasetSchema(
                id="validation-schema-a",
                tenant_id=agent.tenant_id,
                dataset_id=dataset.id,
                schema_version=1,
                schema_hash="b" * 64,
                compatibility="none",
                schema_document={},
            )
            dataset_version = DatasetVersion(
                id="validation-version-a",
                tenant_id=agent.tenant_id,
                dataset_id=dataset.id,
                schema_id=schema.id,
                version_number=1,
                status="ready",
                record_count=0,
                fragment_count=0,
                byte_size=0,
                content_hash="c" * 64,
                manifest={},
            )
            run = IngestionRun(
                id="validation-job-a",
                tenant_id=agent.tenant_id,
                dataset_id=dataset.id,
                pipeline_kind="validation_dataset",
                pipeline_version="v1",
                idempotency_key="validation-job-a",
                status="pending",
            )
            db.add_all([dataset, schema, dataset_version, run])
            db.commit()

            result = agent_deletion_service.cleanup_agent_owned_records(db, agent)
            db.commit()

            retired = db.get(LogicalDataset, dataset.id)
            self.assertIsNotNone(retired)
            assert retired is not None
            self.assertEqual(retired.lifecycle_status, "retired")
            self.assertEqual(retired.labels.get("lifecycle"), "agent_deleted")
            self.assertEqual(retired.labels.get("owner_agent_id"), agent.id)
            retired_version = db.get(DatasetVersion, dataset_version.id)
            self.assertIsNotNone(retired_version)
            assert retired_version is not None
            self.assertEqual(retired_version.status, "retired")
            cancelled = db.get(IngestionRun, run.id)
            self.assertIsNotNone(cancelled)
            assert cancelled is not None
            self.assertEqual(cancelled.status, "cancelled")
            self.assertEqual(result.validation_datasets_retired, 1)
            self.assertEqual(result.validation_jobs_cancelled, 1)

            db.info.update(tenant_id=agent.tenant_id, user_id="user-agent-delete")
            self.assertNotIn(dataset.id, {item.id for item in catalog_service.list_datasets(db)})

    def test_detached_turn_audit_is_not_readable_through_public_turn_api(self) -> None:
        with self.Session() as db:
            agent = db.get(Agent, "agent-delete-a")
            conversation = db.get(Conversation, "conversation-agent-a")
            assert agent is not None
            assert conversation is not None
            assistant = Message(
                id="message-agent-a-assistant",
                conversation_id=conversation.id,
                role="assistant",
                content="private result",
            )
            db.add(assistant)
            db.flush()
            run = AgentTurnRun(
                id="turn-agent-a-deleted",
                tenant_id=agent.tenant_id,
                requested_by_user_id="user-agent-delete",
                agent_id=agent.id,
                conversation_id=conversation.id,
                user_message_id="message-agent-a",
                assistant_message_id=assistant.id,
                idempotency_key="turn-agent-a-deleted",
                request_fingerprint="a" * 64,
                request_payload={"sealed": True},
                request_summary={"attachment_count": 1},
                request_digest="b" * 64,
                status="succeeded",
                revision=1,
                result_document={
                    "answer": "private result",
                    "attachments": [{"asset_version_id": "private"}],
                },
            )
            db.add(run)
            db.flush()
            db.add(
                AgentTurnEvent(
                    tenant_id=agent.tenant_id,
                    run_id=run.id,
                    revision=1,
                    event_type="succeeded",
                    data={"result": run.result_document},
                )
            )
            db.commit()

            agent_deletion_service.cleanup_agent_owned_records(db, agent)
            db.commit()

            retained = db.get(AgentTurnRun, run.id)
            self.assertIsNotNone(retained)
            assert retained is not None
            self.assertIsNone(retained.agent_id)
            db.info.update(
                tenant_id="tenant-agent-delete", user_id="user-agent-delete"
            )
            with self.assertRaises(agent_turn_service.AgentTurnError) as unavailable:
                agent_turn_service.get_turn(db, run.id)
            self.assertEqual(unavailable.exception.status_code, 404)
            with self.assertRaises(agent_turn_service.AgentTurnError):
                agent_turn_service.list_turn_events(db, run.id)


if __name__ == "__main__":
    unittest.main()
