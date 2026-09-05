from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.managed_upload_schemas import ManagedUploadCreateIn
from app.models import (
    Agent,
    AgentTurnRun,
    BucketFile,
    BusinessScenario,
    DataAsset,
    DataAssetVersion,
    ManagedUploadRun,
    OrganizationMember,
    OrganizationRole,
    Tenant,
    User,
)
from app.schemas import AgentChatAttachmentIn, ChatRequest
from app.routers import managed_uploads
from app.services import (
    agent_turn_service,
    datasource_service,
    managed_upload_run_service,
    object_storage_service,
    permission_service,
)
from app.services.auth_service import get_current_user, get_tenant_db


@pytest.fixture()
def upload_database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    Base.metadata.create_all(engine)
    with session_factory() as db:
        tenant = Tenant(id="upload-run-tenant", name="Upload run tenant")
        user = User(
            id="upload-run-user",
            tenant_id=tenant.id,
            email="upload-run@example.test",
            password_hash="test-only",
            status="active",
        )
        scenario = BusinessScenario(
            id="upload-run-scenario",
            tenant_id=tenant.id,
            name="Upload run scenario",
            status="active",
        )
        agent = Agent(
            id="upload-run-agent",
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            name="Upload run agent",
            runtime_binding_mode="capability_only",
            capability_scope={},
        )
        db.add_all([tenant, user, scenario, agent])
        db.commit()
        permission_service.ensure_organization(db, tenant.id, owner_user_id=user.id)
        db.commit()
    configuration = SimpleNamespace(bucket_name="catalog-test", prefix="platform")
    with patch.object(
        object_storage_service,
        "require_configuration",
        return_value=configuration,
    ):
        try:
            yield session_factory
        finally:
            engine.dispose()


def _database(session_factory):
    db = session_factory()
    db.info["tenant_id"] = "upload-run-tenant"
    db.info["user_id"] = "upload-run-user"
    return db


def _create_payload(*, idempotency_key: str = "upload-intent-1") -> ManagedUploadCreateIn:
    return ManagedUploadCreateIn(
        filename="records.csv",
        byte_size=18,
        media_type="text/csv",
        purpose="validation_asset",
        idempotency_key=idempotency_key,
    )


def test_upload_intent_is_committed_without_contacting_object_storage(
    upload_database,
) -> None:
    with patch.object(
        datasource_service,
        "ensure_file_bucket_storage",
        side_effect=AssertionError("intent creation must not perform storage I/O"),
    ):
        with _database(upload_database) as db:
            created = managed_upload_run_service.create_upload_run(
                db,
                _create_payload(),
            )
            replay = managed_upload_run_service.create_upload_run(
                db,
                _create_payload(),
            )

            assert created["id"] == replay["id"]
            assert created["status"] == "awaiting_upload"
            assert created["result"] is None
            assert db.get(ManagedUploadRun, created["id"]) is not None


def test_upload_intent_idempotency_and_content_claim_use_cas(upload_database) -> None:
    with _database(upload_database) as db:
        created = managed_upload_run_service.create_upload_run(db, _create_payload())
        with pytest.raises(managed_upload_run_service.ManagedUploadConflict):
            managed_upload_run_service.create_upload_run(
                db,
                ManagedUploadCreateIn(
                    **{
                        **_create_payload().model_dump(),
                        "filename": "different.csv",
                    }
                ),
            )
        lease = managed_upload_run_service.claim_content_upload(
            db,
            created["id"],
            expected_revision=created["revision"],
        )
        assert lease.generation == 1
        with pytest.raises(managed_upload_run_service.ManagedUploadConflict):
            managed_upload_run_service.claim_content_upload(
                db,
                created["id"],
                expected_revision=created["revision"],
            )


def test_owner_can_cancel_unfinished_invocation_upload_with_revision_cas(
    upload_database,
) -> None:
    with _database(upload_database) as db:
        created = managed_upload_run_service.create_upload_run(
            db,
            ManagedUploadCreateIn(
                filename="assistant-notes.txt",
                byte_size=12,
                media_type="text/plain",
                purpose="invocation_attachment",
                idempotency_key="assistant-cancel-1",
                expires_in_seconds=3600,
            ),
        )
        cancelled = managed_upload_run_service.cancel_upload_run(
            db,
            created["id"],
            expected_revision=created["revision"],
        )

        assert cancelled["status"] == "cancelled"
        assert cancelled["revision"] == created["revision"] + 1
        assert cancelled["error"] == {
            "code": "managed_upload_cancelled",
            "message": "附件上传已取消",
        }
        with pytest.raises(managed_upload_run_service.ManagedUploadConflict):
            managed_upload_run_service.retry_upload_run(
                db,
                created["id"],
                expected_revision=cancelled["revision"],
                idempotency_key="cancelled-upload-retry",
            )


def test_manual_retry_creates_immutable_child_and_replays_by_identity(
    upload_database,
) -> None:
    with _database(upload_database) as db:
        created = managed_upload_run_service.create_upload_run(
            db,
            _create_payload(idempotency_key="immutable-upload-parent"),
        )
        parent = db.get(ManagedUploadRun, created["id"])
        parent.status = "failed"
        parent.revision += 1
        parent.error_code = "profiling_failed"
        parent.error_message = "test failure"
        parent.finished_at = datetime.now(timezone.utc)
        db.commit()
        parent_revision = parent.revision

        retried = managed_upload_run_service.retry_upload_run(
            db,
            parent.id,
            expected_revision=parent_revision,
            idempotency_key="immutable-upload-retry-1",
        )

        db.refresh(parent)
        assert retried["id"] != parent.id
        assert retried["parent_run_id"] == parent.id
        assert retried["status"] == "awaiting_upload"
        assert parent.status == "failed"
        assert parent.revision == parent_revision
        assert parent.error_code == "profiling_failed"
        replay = managed_upload_run_service.retry_upload_run(
            db,
            parent.id,
            expected_revision=1,
            idempotency_key="immutable-upload-retry-1",
        )
        assert replay["id"] == retried["id"]
        with pytest.raises(managed_upload_run_service.ManagedUploadConflict):
            managed_upload_run_service.retry_upload_run(
                db,
                parent.id,
                expected_revision=parent_revision,
                idempotency_key="immutable-upload-retry-2",
            )

        child = db.get(ManagedUploadRun, retried["id"])
        child.status = "failed"
        child.revision += 1
        child.error_code = "retry_failed"
        child.error_message = "test retry failure"
        child.finished_at = datetime.now(timezone.utc)
        db.commit()
        grandchild = managed_upload_run_service.retry_upload_run(
            db,
            child.id,
            expected_revision=child.revision,
            idempotency_key="immutable-upload-retry-3",
        )
        assert grandchild["parent_run_id"] == child.id


def test_manual_retry_recovers_child_after_commit_integrity_signal(
    upload_database,
) -> None:
    with _database(upload_database) as db:
        created = managed_upload_run_service.create_upload_run(
            db,
            _create_payload(idempotency_key="uncertain-upload-parent"),
        )
        parent = db.get(ManagedUploadRun, created["id"])
        parent.status = "failed"
        parent.revision += 1
        parent.error_code = "profiling_failed"
        parent.finished_at = datetime.now(timezone.utc)
        db.commit()
        committed = db.commit

        def commit_then_signal_integrity() -> None:
            committed()
            raise IntegrityError("simulated concurrent insert", {}, RuntimeError())

        with patch.object(db, "commit", side_effect=commit_then_signal_integrity):
            replay = managed_upload_run_service.retry_upload_run(
                db,
                parent.id,
                expected_revision=parent.revision,
                idempotency_key="uncertain-upload-retry",
            )

        persisted = db.get(ManagedUploadRun, replay["id"])
        assert persisted.parent_run_id == parent.id
        assert persisted.idempotency_key == "uncertain-upload-retry"


def test_invocation_upload_run_uses_the_requested_asset_lifetime(
    upload_database,
) -> None:
    with _database(upload_database) as db:
        created = managed_upload_run_service.create_upload_run(
            db,
            ManagedUploadCreateIn(
                filename="short-lived.txt",
                byte_size=12,
                media_type="text/plain",
                purpose="invocation_attachment",
                idempotency_key="short-lived-upload",
                expires_in_seconds=300,
            ),
        )
        run = db.get(ManagedUploadRun, created["id"])

        assert run.expires_at - run.created_at == timedelta(seconds=300)


def test_assistant_attachment_resolution_rechecks_asset_lifecycle(
    upload_database,
) -> None:
    digest = "c" * 64
    with _database(upload_database) as db:
        created = managed_upload_run_service.create_upload_run(
            db,
            ManagedUploadCreateIn(
                filename="expired-assistant.txt",
                byte_size=5,
                media_type="text/plain",
                purpose="invocation_attachment",
                idempotency_key="expired-assistant-upload",
                expires_in_seconds=3600,
            ),
        )
        run = db.get(ManagedUploadRun, created["id"])
        bucket_file = BucketFile(
            id="expired-assistant-file",
            data_source_id=run.data_source_id,
            filename=run.filename,
            stored_path="minio://catalog-test/expired-assistant-file",
            storage_provider="minio",
            bucket_name="catalog-test",
            object_key="platform/expired-assistant-file",
            object_version_id="v1",
            etag="etag",
            object_url="minio://catalog-test/expired-assistant-file",
            size=5,
            mime="text/plain",
            content_sha256=digest,
            status="parsed",
            parsed_text="expired",
        )
        asset = DataAsset(
            id="expired-assistant-asset",
            tenant_id="upload-run-tenant",
            key="expired.assistant.asset",
            name=run.filename,
            kind="file",
            media_type="text/plain",
            usage_plane="invocation_input",
            labels={"catalog_purpose": "invocation_attachment"},
            created_by_user_id="upload-run-user",
        )
        version = DataAssetVersion(
            id="expired-assistant-version",
            tenant_id="upload-run-tenant",
            asset_id=asset.id,
            version_number=1,
            bucket_file_id=bucket_file.id,
            bucket_data_source_id=run.data_source_id,
            provenance_kind="upload",
            status="ready",
            content_sha256=digest,
            byte_size=5,
            source_locator={},
            version_document={
                "profile": {"category": "document"},
                "lifecycle": {
                    "purpose": "invocation_attachment",
                    "temporary": True,
                    "expires_at": (
                        datetime.now(timezone.utc) - timedelta(seconds=1)
                    ).isoformat(),
                },
            },
            created_by_user_id="upload-run-user",
        )
        db.add_all([bucket_file, asset, version])
        db.flush()
        run.status = "ready"
        run.asset_id = asset.id
        run.asset_version_id = version.id
        run.bucket_file_id = bucket_file.id
        run.byte_size = 5
        run.content_sha256 = digest
        run.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        db.commit()

        with pytest.raises(managed_upload_run_service.ManagedUploadError) as exc_info:
            managed_upload_run_service.invocation_attachment_documents(
                db,
                [run.id],
            )

        assert exc_info.value.code == "managed_upload_expired"
        assert exc_info.value.status_code == 410


def test_content_upload_rechecks_write_permission_before_read_or_storage(
    upload_database,
    tmp_path: Path,
) -> None:
    content = b"x" * 18
    digest = hashlib.sha256(content).hexdigest()
    staged = tmp_path / "payload.bin"
    staged.write_bytes(content)
    with _database(upload_database) as db:
        created = managed_upload_run_service.create_upload_run(
            db,
            _create_payload(idempotency_key="permission-recheck"),
        )
        lease = managed_upload_run_service.claim_content_upload(
            db,
            created["id"],
            expected_revision=created["revision"],
        )
        member = db.query(OrganizationMember).filter_by(
            user_id="upload-run-user"
        ).one()
        viewer = db.query(OrganizationRole).filter_by(
            organization_id=member.organization_id,
            key="viewer",
        ).one()
        member.role_id = viewer.id
        db.commit()
        db.info.pop("permission_cache", None)

        with pytest.raises(HTTPException) as preflight_error:
            managed_upload_run_service.preflight_content_upload(
                db,
                created["id"],
                expected_revision=created["revision"] + 1,
            )
        assert preflight_error.value.status_code == 403

    with (
        patch.object(managed_upload_run_service, "SessionLocal", upload_database),
        patch.object(
            managed_upload_run_service.object_deletion_service,
            "prepare_bucket_file_upload",
        ) as prepare_storage,
    ):
        with pytest.raises(HTTPException) as storage_error:
            managed_upload_run_service.store_uploaded_content(
                created["id"],
                lease,
                staged,
                content_sha256=digest,
                byte_size=len(content),
            )
    assert storage_error.value.status_code == 403
    prepare_storage.assert_not_called()


def test_agent_turn_accepts_pending_upload_and_resolves_only_after_ready(
    upload_database,
) -> None:
    with _database(upload_database) as db:
        upload = managed_upload_run_service.create_upload_run(db, _create_payload())
        turn = agent_turn_service.enqueue_turn(
            db,
            "upload-run-agent",
            ChatRequest(
                message="分析刚上传的数据",
                idempotency_key="turn-with-upload-1",
                attachments=[AgentChatAttachmentIn(upload_run_id=upload["id"])],
            ),
        )

    executor = Mock()
    with patch.object(agent_turn_service, "SessionLocal", upload_database):
        assert agent_turn_service.process_turn(turn["id"], executor)
    executor.assert_not_called()

    digest = "a" * 64
    with _database(upload_database) as db:
        upload_row = db.get(ManagedUploadRun, upload["id"])
        source_id = upload_row.data_source_id
        bucket_file = BucketFile(
            id="ready-upload-file",
            data_source_id=source_id,
            filename="server-name.csv",
            stored_path="minio://catalog-test/ready-upload-file",
            storage_provider="minio",
            bucket_name="catalog-test",
            object_key="platform/ready-upload-file",
            object_version_id="v1",
            etag="etag",
            object_url="minio://catalog-test/ready-upload-file",
            size=18,
            mime="text/csv",
            content_sha256=digest,
            status="parsed",
        )
        asset = DataAsset(
            id="ready-upload-asset",
            tenant_id="upload-run-tenant",
            key="ready.upload.asset",
            name="renamed-without-contract-dependence.bin",
            kind="file",
            media_type="text/csv",
            usage_plane="invocation_input",
            labels={"catalog_purpose": "validation_asset"},
            created_by_user_id="upload-run-user",
        )
        version = DataAssetVersion(
            id="ready-upload-version",
            tenant_id="upload-run-tenant",
            asset_id=asset.id,
            version_number=1,
            bucket_file_id=bucket_file.id,
            bucket_data_source_id=source_id,
            provenance_kind="upload",
            status="ready",
            content_sha256=digest,
            byte_size=18,
            source_locator={},
            version_document={
                "profile": {"category": "document"},
                "lifecycle": {"purpose": "validation_asset", "temporary": False},
            },
            created_by_user_id="upload-run-user",
        )
        db.add_all([bucket_file, asset, version])
        db.flush()
        upload_row.status = "ready"
        upload_row.asset_id = asset.id
        upload_row.asset_version_id = version.id
        upload_row.content_sha256 = digest
        upload_row.byte_size = 18
        upload_row.finished_at = datetime.now(timezone.utc)
        queued = db.get(AgentTurnRun, turn["id"])
        queued.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    observed: dict[str, str] = {}

    def execute(_agent_id, *, attachments, conversation_id, **kwargs):
        observed["asset_version_id"] = attachments[0].asset_version_id
        return {
            "answer": "done",
            "conversation_id": conversation_id,
            "assistant_message_id": kwargs["assistant_message_id"],
            "trace_id": "upload-turn-trace",
            "input_snapshot": {},
            "evidence_refs": [],
            "runtime": {},
        }

    with patch.object(agent_turn_service, "SessionLocal", upload_database):
        assert agent_turn_service.process_turn(turn["id"], execute)
    assert observed == {"asset_version_id": "ready-upload-version"}


@pytest.mark.parametrize(
    ("purpose", "expects_assistant_text"),
    [
        ("validation_asset", False),
        ("invocation_attachment", True),
    ],
)
def test_background_worker_profiles_stored_content_and_publishes_ready_reference(
    upload_database,
    tmp_path: Path,
    purpose: str,
    expects_assistant_text: bool,
) -> None:
    content = b"id,name\n1,Alice\n"
    digest = hashlib.sha256(content).hexdigest()
    with _database(upload_database) as db:
        upload = managed_upload_run_service.create_upload_run(
            db,
            ManagedUploadCreateIn(
                filename="renamed.csv",
                byte_size=len(content),
                media_type="text/csv",
                purpose=purpose,
                idempotency_key=f"upload-worker-{purpose}",
                expires_in_seconds=3600 if purpose == "invocation_attachment" else None,
            ),
        )
        run = db.get(ManagedUploadRun, upload["id"])
        bucket_file = BucketFile(
            id="stored-upload-file",
            data_source_id=run.data_source_id,
            filename=run.filename,
            stored_path="minio://catalog-test/platform/stored-upload-file",
            storage_provider="minio",
            bucket_name="catalog-test",
            object_key="platform/stored-upload-file",
            object_version_id="v1",
            etag="etag",
            object_url="minio://catalog-test/platform/stored-upload-file",
            size=len(content),
            mime="text/csv",
            content_sha256=digest,
            status="pending",
        )
        db.add(bucket_file)
        db.flush()
        run.bucket_file_id = bucket_file.id
        run.byte_size = len(content)
        run.content_sha256 = digest
        run.status = "stored"
        run.revision += 1
        run.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    def download(_bucket, _key, destination, **_kwargs):
        Path(destination).write_bytes(content)
        return SimpleNamespace(size=len(content))

    with (
        patch.object(managed_upload_run_service, "SessionLocal", upload_database),
        patch.object(
            object_storage_service,
            "download_object_to_file",
            side_effect=download,
        ),
    ):
        assert managed_upload_run_service.process_upload_run(upload["id"])

    with _database(upload_database) as db:
        run = db.get(ManagedUploadRun, upload["id"])
        assert run.status == "ready"
        assert run.asset_version_id
        asset = db.get(DataAsset, run.asset_id)
        version = db.get(DataAssetVersion, run.asset_version_id)
        bucket_file = db.get(BucketFile, version.bucket_file_id)
        assert asset.usage_plane == "invocation_input"
        assert version.version_document["profile"]["category"] == "table"
        assert version.version_document["profile"]["tables"][0]["columns"][0]["name"] == "id"
        assert bool((bucket_file.parsed_text or "").strip()) is expects_assistant_text


def test_expired_body_lease_is_recovered_without_overwriting_live_upload(
    upload_database,
) -> None:
    now = datetime.now(timezone.utc)
    with _database(upload_database) as db:
        first = managed_upload_run_service.create_upload_run(
            db,
            _create_payload(idempotency_key="expired-upload-lease"),
        )
        managed_upload_run_service.claim_content_upload(
            db,
            first["id"],
            expected_revision=first["revision"],
        )
        second = managed_upload_run_service.create_upload_run(
            db,
            _create_payload(idempotency_key="live-upload-lease"),
        )
        managed_upload_run_service.claim_content_upload(
            db,
            second["id"],
            expected_revision=second["revision"],
        )
        expired = db.get(ManagedUploadRun, first["id"])
        expired.lease_expires_at = now - timedelta(seconds=1)
        live = db.get(ManagedUploadRun, second["id"])
        live.lease_expires_at = now + timedelta(minutes=5)
        db.commit()

    with patch.object(managed_upload_run_service, "SessionLocal", upload_database):
        assert managed_upload_run_service.recover_upload_runs() == 1

    with _database(upload_database) as db:
        expired = db.get(ManagedUploadRun, first["id"])
        live = db.get(ManagedUploadRun, second["id"])
        assert expired.status == "awaiting_upload"
        assert expired.error_code == "managed_upload_interrupted"
        assert managed_upload_run_service._as_utc(expired.available_at) > now
        assert expired.lease_token == ""
        assert expired.revision == first["revision"] + 2
        assert live.status == "uploading"
        assert live.lease_token


def test_expired_upload_lease_cannot_be_renewed_or_write_failure(
    upload_database,
) -> None:
    now = datetime.now(timezone.utc)
    with _database(upload_database) as db:
        body_upload = managed_upload_run_service.create_upload_run(
            db,
            _create_payload(idempotency_key="expired-upload-heartbeat"),
        )
        body_lease = managed_upload_run_service.claim_content_upload(
            db,
            body_upload["id"],
            expected_revision=body_upload["revision"],
        )
        body_run = db.get(ManagedUploadRun, body_upload["id"])
        body_run.lease_expires_at = now - timedelta(seconds=1)

        processing_upload = managed_upload_run_service.create_upload_run(
            db,
            _create_payload(idempotency_key="expired-processing-failure"),
        )
        processing_run = db.get(ManagedUploadRun, processing_upload["id"])
        processing_run.status = "stored"
        processing_run.revision += 1
        db.commit()
        processing_lease = managed_upload_run_service._claim_processing_run(
            db,
            processing_upload["id"],
        )
        assert processing_lease is not None
        processing_run = db.get(ManagedUploadRun, processing_upload["id"])
        processing_run.lease_expires_at = now - timedelta(seconds=1)
        db.commit()

    with patch.object(managed_upload_run_service, "SessionLocal", upload_database):
        assert not managed_upload_run_service._renew_lease(
            body_upload["id"],
            body_lease,
            "uploading",
            30,
        )
        with patch.object(
            managed_upload_run_service,
            "_load_processing_snapshot",
            side_effect=managed_upload_run_service.ManagedUploadError(
                "late_worker_failure",
                "stale worker must not finalize",
            ),
        ):
            assert not managed_upload_run_service._profile_stored_upload(
                processing_upload["id"],
                processing_lease,
            )

    with _database(upload_database) as db:
        body_run = db.get(ManagedUploadRun, body_upload["id"])
        processing_run = db.get(ManagedUploadRun, processing_upload["id"])
        assert managed_upload_run_service._as_utc(body_run.lease_expires_at) <= now
        assert processing_run.status == "processing"
        assert processing_run.error_code == ""


def test_failed_content_upload_stops_waiting_turn_after_retry_window(
    upload_database,
) -> None:
    with _database(upload_database) as db:
        upload = managed_upload_run_service.create_upload_run(
            db,
            _create_payload(idempotency_key="failed-content-upload"),
        )
        turn = agent_turn_service.enqueue_turn(
            db,
            "upload-run-agent",
            ChatRequest(
                message="处理上传失败的附件",
                idempotency_key="turn-failed-content-upload",
                attachments=[AgentChatAttachmentIn(upload_run_id=upload["id"])],
            ),
        )
        run = db.get(ManagedUploadRun, upload["id"])
        run.error_code = "managed_upload_content_failed"
        run.error_message = "文件内容上传失败，请重试"
        run.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    executor = Mock()
    with patch.object(agent_turn_service, "SessionLocal", upload_database):
        assert agent_turn_service.process_turn(turn["id"], executor)
    executor.assert_not_called()
    with _database(upload_database) as db:
        failed_turn = db.get(AgentTurnRun, turn["id"])
        assert failed_turn.status == "failed"
        assert failed_turn.error_code == "attachment_upload_failed"


def test_expired_temporary_asset_is_rejected_before_turn_acceptance(
    upload_database,
) -> None:
    digest = "b" * 64
    with _database(upload_database) as db:
        upload = managed_upload_run_service.create_upload_run(
            db,
            _create_payload(idempotency_key="temporary-expiry-source"),
        )
        source_id = db.get(ManagedUploadRun, upload["id"]).data_source_id
        bucket_file = BucketFile(
            id="expired-temporary-file",
            data_source_id=source_id,
            filename="expired.txt",
            stored_path="minio://catalog-test/expired-temporary-file",
            storage_provider="minio",
            bucket_name="catalog-test",
            object_key="platform/expired-temporary-file",
            object_version_id="v1",
            etag="etag",
            object_url="minio://catalog-test/expired-temporary-file",
            size=5,
            mime="text/plain",
            content_sha256=digest,
            status="parsed",
        )
        asset = DataAsset(
            id="expired-temporary-asset",
            tenant_id="upload-run-tenant",
            key="expired.temporary.asset",
            name="expired.txt",
            kind="file",
            media_type="text/plain",
            usage_plane="invocation_input",
            labels={"catalog_purpose": "invocation_attachment"},
            created_by_user_id="upload-run-user",
        )
        version = DataAssetVersion(
            id="expired-temporary-version",
            tenant_id="upload-run-tenant",
            asset_id=asset.id,
            version_number=1,
            bucket_file_id=bucket_file.id,
            bucket_data_source_id=source_id,
            provenance_kind="upload",
            status="ready",
            content_sha256=digest,
            byte_size=5,
            source_locator={},
            version_document={
                "profile": {"category": "document"},
                "lifecycle": {
                    "purpose": "invocation_attachment",
                    "temporary": True,
                    "expires_at": (
                        datetime.now(timezone.utc) - timedelta(seconds=1)
                    ).isoformat(),
                },
            },
            created_by_user_id="upload-run-user",
        )
        db.add_all([bucket_file, asset, version])
        db.commit()

        with pytest.raises(agent_turn_service.AgentTurnError) as exc_info:
            agent_turn_service.enqueue_turn(
                db,
                "upload-run-agent",
                ChatRequest(
                    message="处理过期附件",
                    idempotency_key="turn-expired-temporary",
                    attachments=[
                        AgentChatAttachmentIn(asset_version_id=version.id)
                    ],
                ),
            )
        assert exc_info.value.code == "attachment_expired"
        assert exc_info.value.status_code == 410


def test_upload_intent_http_endpoint_returns_202_without_storage_io(
    upload_database,
) -> None:
    app = FastAPI()
    app.include_router(managed_uploads.router, prefix="/api")

    def tenant_db_override():
        with _database(upload_database) as db:
            yield db

    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id="upload-run-user",
        tenant_id="upload-run-tenant",
    )
    app.dependency_overrides[get_tenant_db] = tenant_db_override
    with (
        patch.object(
            datasource_service,
            "ensure_file_bucket_storage",
            side_effect=AssertionError("intent endpoint must not perform storage I/O"),
        ),
        TestClient(app) as client,
    ):
        response = client.post(
            "/api/catalog/upload-runs",
            json={
                "filename": "renamed-input.bin",
                "byte_size": 1024,
                "media_type": "application/octet-stream",
                "purpose": "invocation_attachment",
                "idempotency_key": "http-upload-intent",
            },
        )

    assert response.status_code == 202
    assert response.json()["status"] == "awaiting_upload"
    assert "object_key" not in response.text
