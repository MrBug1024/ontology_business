"""Real concurrent decisions and evidence retention in a self-created database."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from threading import Barrier

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.approval_models import WorkflowApprovalEvidence
from app.channel_interaction_schemas import ChannelReplyIn, EvidenceReference
from app.models import BucketFile, DataAsset, DataAssetVersion, DataSource, WorkflowApprovalRequest, WorkflowRun
from app.services import capability_application_service, channel_interaction_service, operations_service
from app.services.policies import PolicyViolation
from .access_postgresql import isolated_access_database
from .test_channel_interactions import approval_world


pytestmark = pytest.mark.skipif(os.environ.get("RUN_POSTGRESQL_INTEGRATION_TESTS") != "1", reason="requires a self-created PostgreSQL database")


def test_concurrent_messages_resolve_one_decision_and_old_message_cannot_approve_next_node():
    with isolated_access_database() as (url, _admin):
        engine = create_engine(url)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            with factory() as db:
                world = approval_world(db, two_nodes=True)
                actor = world.runtime._actor()
                approval_id = world.approval.id
                db.commit()
            barrier = Barrier(2)

            def reply(message_id):
                with factory() as db:
                    db.info.update(tenant_id=actor.tenant_id, user_id=actor.user_id)
                    barrier.wait(timeout=15)
                    try:
                        return channel_interaction_service.reply_approval(db, actor, approval_id,
                            ChannelReplyIn(text="同意", message_id=message_id, expected_revision=1))["status"]
                    except channel_interaction_service.ChannelInteractionError:
                        db.rollback()
                        return "conflict"

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(reply, ["message-a", "message-b"]))
            assert sorted(outcomes) == ["approved", "conflict"]
            with factory() as db:
                db.info.update(tenant_id=actor.tenant_id, user_id=actor.user_id)
                operations_service.process_available_runs(db)
                pending = db.scalar(select(WorkflowApprovalRequest).where(WorkflowApprovalRequest.status == "pending"))
                assert pending.id != approval_id
                with pytest.raises(channel_interaction_service.ChannelInteractionError):
                    channel_interaction_service.reply_approval(db, actor, approval_id,
                        ChannelReplyIn(text="驳回", message_id="late-message", expected_revision=1))
                db.rollback()
                assert db.get(WorkflowApprovalRequest, pending.id).status == "pending"
                assert db.scalar(select(func.count(WorkflowApprovalRequest.id)).where(WorkflowApprovalRequest.status == "approved")) == 1
        finally:
            engine.dispose()


def test_approved_evidence_survives_attachment_expiry_and_runtime_cannot_erase_it():
    with isolated_access_database() as (url, _admin):
        engine = create_engine(url)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            with factory() as db:
                world = approval_world(db, evidence=True)
                source = DataSource(tenant_id=world.tenant.id, name="Evidence storage", type="file_bucket", config={})
                db.add(source)
                db.flush()
                file = BucketFile(data_source_id=source.id, filename="evidence.txt", stored_path="minio://test-evidence/evidence.txt", content_sha256="c" * 64, size=8)
                asset = DataAsset(tenant_id=world.tenant.id, key="pg-evidence", name="Evidence", usage_plane="invocation_input",
                    created_by_user_id=world.user.id, labels={"catalog_purpose": "invocation_attachment"})
                db.add_all([file, asset])
                db.flush()
                version = DataAssetVersion(tenant_id=world.tenant.id, asset_id=asset.id, version_number=1, status="ready", content_sha256=file.content_sha256,
                    bucket_file_id=file.id, bucket_data_source_id=source.id, byte_size=8,
                    version_document={"lifecycle": {"purpose": "invocation_attachment", "temporary": True,
                        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}})
                db.add(version)
                db.commit()
                accepted = channel_interaction_service.reply_approval(db, world.runtime._actor(), world.approval.id,
                    ChannelReplyIn(text="同意", message_id="evidence-message", expected_revision=1,
                        evidence=[EvidenceReference(asset_version_id=version.id, expected_signature=version.content_sha256)]))
                assert accepted["status"] == "approved"
                assert operations_service.purge_expired_catalog_attachments(db, now=datetime.now(timezone.utc) + timedelta(days=1)) == 0
                db.commit()
                assert db.get(DataAssetVersion, version.id).bucket_file_id == file.id
                assert db.scalar(select(WorkflowApprovalEvidence.bucket_file_id)) == file.id
                for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
                    assert not db.scalar(text("SELECT has_table_privilege(current_user, 'workflow_approval_evidence', :privilege)"), {"privilege": privilege})
                with pytest.raises(IntegrityError):
                    with db.begin_nested():
                        db.delete(file)
                        db.flush()
                db.rollback()
                assert db.get(BucketFile, file.id) is not None
        finally:
            engine.dispose()


def test_concurrent_retries_preserve_original_receipt_and_create_one_new_execution():
    with isolated_access_database() as (url, _admin):
        engine = create_engine(url)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            with factory() as db:
                world = approval_world(db)
                operations_service.cancel_run(db, world.run)
                actor = world.runtime._actor()
                run_id, old_key = world.run.id, world.run.execution_key
                invocation_id = world.receipt["invocation_id"]
                original = capability_application_service.get_receipt(db, actor, invocation_id)
            barrier = Barrier(2)

            def retry(_):
                with factory() as db:
                    db.info.update(tenant_id=actor.tenant_id, user_id=actor.user_id)
                    run = db.get(WorkflowRun, run_id)
                    barrier.wait(timeout=15)
                    try:
                        return operations_service.retry_run(db, run).status
                    except PolicyViolation:
                        db.rollback()
                        return "conflict"

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(retry, range(2)))
            assert sorted(outcomes) == ["conflict", "queued"]
            with factory() as db:
                db.info.update(tenant_id=actor.tenant_id, user_id=actor.user_id)
                run = db.get(WorkflowRun, run_id)
                assert run.execution_key != old_key
                operations_service.process_available_runs(db)
                after = capability_application_service.get_receipt(db, actor, invocation_id)
                assert after["status"] == original["status"] == "cancelled"
                assert after["delivery"] == original["delivery"]
                approvals = db.scalars(select(WorkflowApprovalRequest).where(WorkflowApprovalRequest.workflow_run_id == run_id)).all()
                assert sorted(item.status for item in approvals) == ["cancelled", "pending"]
                assert len({item.execution_key for item in approvals}) == 2
        finally:
            engine.dispose()
