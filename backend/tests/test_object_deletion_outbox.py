from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import call, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import AssistantAttachment, BucketFile, DataSource, ObjectDeletionJob
from app.services import (
    datasource_service,
    object_deletion_service,
    object_storage_service,
)


class ObjectDeletionOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        ObjectDeletionJob.__table__.create(self.engine)
        BucketFile.__table__.create(self.engine)
        AssistantAttachment.__table__.create(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.configuration = object_storage_service.MinioConfiguration(
            endpoint="minio.example.test",
            access_key="access",
            secret_key="secret",
            bucket_name="ontology",
            prefix="ontology-business",
        )
        self.configuration_patch = patch.object(
            object_storage_service,
            "configuration",
            return_value=self.configuration,
        )
        self.configuration_patch.start()
        self.addCleanup(self.configuration_patch.stop)
        self.source = DataSource(
            id="source-a",
            tenant_id="tenant-a",
            scenario_id="scenario-a",
            name="资料库",
            type="file_bucket",
            config={
                "storage_backend": "minio",
                "bucket_name": "ontology",
                "prefix": "ontology-business",
            },
        )

    def _bucket_file(self, file_id: str = "a" * 32) -> BucketFile:
        filename = "审计依据.md"
        object_key = datasource_service.build_bucket_object_key(
            self.source, file_id, filename
        )
        object_url = object_storage_service.stable_object_url(
            "ontology", object_key
        )
        return BucketFile(
            id=file_id,
            data_source_id=self.source.id,
            filename=filename,
            stored_path=object_url,
            storage_provider="minio",
            bucket_name="ontology",
            object_key=object_key,
            object_version_id="version-1",
            object_url=object_url,
        )

    def _session_factory(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    def _bucket_file_for_claim(
        self,
        claim: object_deletion_service.UploadIntentClaim,
        *,
        version_id: str = "version-1",
    ) -> BucketFile:
        bucket_file = self._bucket_file(claim.origin_id)
        bucket_file.object_key = claim.object_key
        bucket_file.object_url = claim.object_url
        bucket_file.stored_path = claim.object_url
        bucket_file.object_version_id = version_id
        bucket_file._managed_object_created = True
        return bucket_file

    def _expire_job(self, job_id: str) -> None:
        job = self.db.get(ObjectDeletionJob, job_id)
        job.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.db.commit()

    def test_rollback_never_deletes_the_object_or_keeps_the_job(self) -> None:
        bucket_file = self._bucket_file()
        object_deletion_service.enqueue_bucket_file_deletion(
            self.db, bucket_file, self.source
        )
        with patch.object(object_storage_service, "delete_object") as delete_object:
            self.db.rollback()
        self.assertEqual(self.db.scalar(select(ObjectDeletionJob)), None)
        delete_object.assert_not_called()

    def test_failed_delete_is_retried_and_completed_idempotently(self) -> None:
        bucket_file = self._bucket_file()
        job_id = object_deletion_service.enqueue_bucket_file_deletion(
            self.db, bucket_file, self.source
        )
        self.db.commit()

        with patch.object(
            object_storage_service,
            "delete_object",
            side_effect=object_storage_service.ObjectStorageError("unavailable"),
        ):
            self.assertEqual(
                object_deletion_service.process_object_deletion_jobs(self.db), 0
            )
        job = self.db.get(ObjectDeletionJob, job_id)
        self.assertIsNotNone(job)
        self.assertEqual(job.status, "retry")
        self.assertEqual(job.attempts, 1)

        job.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.db.commit()
        with patch.object(object_storage_service, "delete_object") as delete_object:
            self.assertEqual(
                object_deletion_service.process_object_deletion_jobs(self.db), 1
            )
        delete_object.assert_called_once_with(
            "ontology",
            bucket_file.object_key,
            version_id="version-1",
        )
        self.assertEqual(self.db.get(ObjectDeletionJob, job_id).status, "completed")

    def test_unversioned_normal_delete_targets_only_the_recorded_unique_key(self) -> None:
        bucket_file = self._bucket_file()
        bucket_file.object_key = datasource_service.build_bucket_object_key(
            self.source,
            bucket_file.id,
            bucket_file.filename,
            upload_id="e" * 32,
        )
        bucket_file.object_url = object_storage_service.stable_object_url(
            bucket_file.bucket_name,
            bucket_file.object_key,
        )
        bucket_file.stored_path = bucket_file.object_url
        bucket_file.object_version_id = ""
        job_id = object_deletion_service.enqueue_bucket_file_deletion(
            self.db,
            bucket_file,
            self.source,
        )
        self.db.commit()

        with patch.object(object_storage_service, "delete_object") as delete_object:
            self.assertEqual(
                object_deletion_service.process_object_deletion_jobs(
                    self.db,
                    job_ids=[job_id],
                ),
                1,
            )
        delete_object.assert_called_once_with(
            bucket_file.bucket_name,
            bucket_file.object_key,
            version_id="",
        )

    def test_shared_legacy_object_version_is_retained_for_other_bucket_file(self) -> None:
        """A source cascade must not remove bytes still referenced elsewhere."""
        legacy_key = "ontology-business/migrations/file-buckets/shared-legacy.md"
        legacy_url = object_storage_service.stable_object_url("ontology", legacy_key)
        deleted_record = self._bucket_file("a" * 32)
        deleted_record.filename = "shared-legacy.md"
        deleted_record.object_key = legacy_key
        deleted_record.object_url = legacy_url
        deleted_record.stored_path = legacy_url
        deleted_record.object_version_id = "legacy-version"
        remaining_reference = BucketFile(
            id="b" * 32,
            data_source_id="source-b",
            filename="shared-legacy.md",
            stored_path=legacy_url,
            storage_provider="minio",
            bucket_name="ontology",
            object_key=legacy_key,
            object_version_id="legacy-version",
            object_url=legacy_url,
        )
        self.db.add(remaining_reference)
        job_id = object_deletion_service.enqueue_bucket_file_deletion(
            self.db, deleted_record, self.source
        )
        self.db.commit()

        with patch.object(object_storage_service, "delete_object") as delete_object:
            self.assertEqual(
                object_deletion_service.process_object_deletion_jobs(
                    self.db, job_ids=[job_id]
                ),
                1,
            )

        delete_object.assert_not_called()
        self.assertEqual(self.db.get(ObjectDeletionJob, job_id).status, "completed")
        guard_id = object_deletion_service._upload_intent_id(
            "minio", "ontology", legacy_key, legacy_url
        )
        self.assertEqual(self.db.get(ObjectDeletionJob, guard_id).status, "retained")
        self.assertIsNotNone(self.db.get(BucketFile, remaining_reference.id))

    def test_crash_after_external_delete_replays_the_same_version(self) -> None:
        bucket_file = self._bucket_file()
        job_id = object_deletion_service.enqueue_bucket_file_deletion(
            self.db, bucket_file, self.source
        )
        self.db.commit()
        calls: list[tuple[str, str, str]] = []

        def crash_after_delete(bucket: str, key: str, *, version_id: str = "") -> None:
            calls.append((bucket, key, version_id))
            raise KeyboardInterrupt("simulated process stop")

        with patch.object(
            object_storage_service, "delete_object", side_effect=crash_after_delete
        ):
            with self.assertRaises(KeyboardInterrupt):
                object_deletion_service.process_object_deletion_jobs(self.db)
        self.db.rollback()
        self.assertEqual(self.db.get(ObjectDeletionJob, job_id).status, "pending")

        with patch.object(
            object_storage_service,
            "delete_object",
            side_effect=lambda bucket, key, *, version_id="": calls.append(
                (bucket, key, version_id)
            ),
        ):
            self.assertEqual(
                object_deletion_service.process_object_deletion_jobs(self.db), 1
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])

    def test_parent_cascade_enqueues_every_object(self) -> None:
        first = self._bucket_file("a" * 32)
        second = self._bucket_file("b" * 32)
        ids = {
            object_deletion_service.enqueue_bucket_file_deletion(
                self.db, bucket_file, self.source
            )
            for bucket_file in (first, second)
        }
        self.db.commit()
        self.assertEqual(len(ids), 2)
        persisted = list(self.db.scalars(select(ObjectDeletionJob)).all())
        self.assertEqual(len(persisted), 4)
        self.assertTrue(ids.issubset({job.id for job in persisted}))
        self.assertEqual(
            {job.status for job in persisted if job.id not in ids},
            {"deleting"},
        )

    def test_corrupt_scope_never_enqueues_or_deletes(self) -> None:
        bucket_file = self._bucket_file()
        bucket_file.object_key = bucket_file.object_key.replace(
            "tenant-a", "tenant-b"
        )
        with patch.object(object_storage_service, "delete_object") as delete_object:
            with self.assertRaisesRegex(ValueError, "字段与地址|作用域"):
                object_deletion_service.enqueue_bucket_file_deletion(
                    self.db, bucket_file, self.source
                )
        self.db.rollback()
        self.assertEqual(self.db.scalar(select(ObjectDeletionJob)), None)
        delete_object.assert_not_called()

    def test_expired_upload_is_fenced_before_ambiguous_cleanup_failure(self) -> None:
        bucket_file = self._bucket_file()
        claim = object_deletion_service.prepare_bucket_file_upload(
            self.source,
            bucket_file.id,
            bucket_file.filename,
            session_factory=self._session_factory,
        )
        object_deletion_service.begin_upload_put(claim)
        self._expire_job(claim.job_id)
        with patch.object(
            object_storage_service,
            "delete_all_object_versions",
            side_effect=object_storage_service.ObjectStorageError("lost response"),
        ):
            self.assertEqual(
                object_deletion_service.process_object_deletion_jobs(self.db), 0
            )
        fenced = self.db.get(ObjectDeletionJob, claim.job_id)
        self.assertEqual(fenced.status, "cleanup_retry")
        self.assertEqual(fenced.lease_token, "")
        old_result = self._bucket_file_for_claim(claim)
        self.db.add(old_result)
        with self.assertRaisesRegex(RuntimeError, "回收"):
            object_deletion_service.retain_bucket_file_upload(
                self.db, claim, old_result, self.source
            )
        self.db.rollback()

        replacement_claim = object_deletion_service.prepare_bucket_file_upload(
            self.source,
            bucket_file.id,
            bucket_file.filename,
            session_factory=self._session_factory,
        )
        self.assertNotEqual(replacement_claim.object_key, claim.object_key)
        replacement = self._bucket_file_for_claim(
            replacement_claim,
            version_id="version-2",
        )
        object_deletion_service.begin_upload_put(replacement_claim)
        self.db.add(replacement)
        object_deletion_service.retain_bucket_file_upload(
            self.db,
            replacement_claim,
            replacement,
            self.source,
        )
        self.db.commit()

        self._expire_job(claim.job_id)
        with patch.object(object_storage_service, "delete_all_object_versions") as delete_versions:
            self.assertEqual(
                object_deletion_service.process_object_deletion_jobs(self.db), 0
            )
        self.assertEqual(
            self.db.get(ObjectDeletionJob, claim.job_id).status, "reclaim_wait"
        )
        self._expire_job(claim.job_id)
        with patch.object(object_storage_service, "delete_all_object_versions") as final_sweep:
            self.assertEqual(
                object_deletion_service.process_object_deletion_jobs(self.db), 1
            )
        delete_versions.assert_called_once_with(claim.bucket_name, claim.object_key)
        final_sweep.assert_called_once_with(claim.bucket_name, claim.object_key)
        self.assertEqual(
            self.db.get(BucketFile, replacement.id).object_key,
            replacement_claim.object_key,
        )

    def test_concurrent_attempts_use_distinct_physical_keys(self) -> None:
        bucket_file = self._bucket_file()
        first = object_deletion_service.prepare_bucket_file_upload(
            self.source,
            bucket_file.id,
            bucket_file.filename,
            session_factory=self._session_factory,
        )
        second = object_deletion_service.prepare_bucket_file_upload(
            self.source,
            bucket_file.id,
            bucket_file.filename,
            session_factory=self._session_factory,
        )
        self.assertNotEqual(first.job_id, second.job_id)
        self.assertNotEqual(first.object_key, second.object_key)

        object_deletion_service.begin_upload_put(second)
        self.assertEqual(
            self.db.get(ObjectDeletionJob, second.job_id).status,
            "putting",
        )
        with self.assertRaisesRegex(
            object_deletion_service.UploadIntentLeaseLostError,
            "重复写入|已消费",
        ):
            object_deletion_service.begin_upload_put(second)
        replacement = self._bucket_file_for_claim(second, version_id="version-2")
        self.db.add(replacement)
        object_deletion_service.retain_bucket_file_upload(
            self.db, second, replacement, self.source
        )
        self.db.commit()
        self._expire_job(first.job_id)
        with patch.object(object_storage_service, "delete_all_object_versions") as delete_versions:
            self.assertEqual(
                object_deletion_service.process_object_deletion_jobs(self.db), 0
            )
        self.assertEqual(
            self.db.get(ObjectDeletionJob, first.job_id).status, "reclaim_wait"
        )
        self._expire_job(first.job_id)
        with patch.object(object_storage_service, "delete_all_object_versions") as final_sweep:
            self.assertEqual(
                object_deletion_service.process_object_deletion_jobs(self.db), 1
            )
        old_result = self._bucket_file_for_claim(first)
        with self.assertRaisesRegex(RuntimeError, "回收"):
            object_deletion_service.retain_bucket_file_upload(
                self.db, first, old_result, self.source
            )
        self.db.rollback()
        delete_versions.assert_called_once_with(first.bucket_name, first.object_key)
        final_sweep.assert_called_once_with(first.bucket_name, first.object_key)
        self.assertEqual(
            self.db.get(ObjectDeletionJob, second.job_id).status,
            "retained",
        )

    def test_crash_before_put_reclaims_without_allowing_old_finalize(self) -> None:
        bucket_file = self._bucket_file()
        claim = object_deletion_service.prepare_bucket_file_upload(
            self.source,
            bucket_file.id,
            bucket_file.filename,
            session_factory=self._session_factory,
        )
        self._expire_job(claim.job_id)
        with patch.object(
            object_storage_service, "delete_all_object_versions"
        ) as delete_versions:
            self.assertEqual(
                object_deletion_service.process_object_deletion_jobs(self.db), 0
            )
            self._expire_job(claim.job_id)
            self.assertEqual(
                object_deletion_service.process_object_deletion_jobs(self.db), 1
            )
        self.assertEqual(
            delete_versions.call_args_list,
            [
                call(claim.bucket_name, claim.object_key),
                call(claim.bucket_name, claim.object_key),
            ],
        )
        old_result = self._bucket_file_for_claim(claim)
        with self.assertRaisesRegex(RuntimeError, "回收"):
            object_deletion_service.retain_bucket_file_upload(
                self.db, claim, old_result, self.source
            )
        self.db.rollback()

    def test_expired_upload_with_committed_reference_is_retained(self) -> None:
        bucket_file = self._bucket_file()
        claim = object_deletion_service.prepare_bucket_file_upload(
            self.source,
            bucket_file.id,
            bucket_file.filename,
            session_factory=self._session_factory,
        )
        object_deletion_service.begin_upload_put(claim)
        bucket_file = self._bucket_file_for_claim(claim)
        self.db.add(bucket_file)
        self.db.commit()
        self._expire_job(claim.job_id)

        with patch.object(
            object_storage_service, "delete_all_object_versions"
        ) as delete_versions:
            self.assertEqual(
                object_deletion_service.process_object_deletion_jobs(self.db), 0
            )

        delete_versions.assert_not_called()
        retained = self.db.get(ObjectDeletionJob, claim.job_id)
        self.assertEqual(retained.status, "retained")
        self.assertEqual(retained.lease_token, "")

    def test_late_old_version_cleanup_only_deletes_old_unique_key(self) -> None:
        old_file = self._bucket_file()
        old_claim = object_deletion_service.prepare_bucket_file_upload(
            self.source,
            old_file.id,
            old_file.filename,
            session_factory=self._session_factory,
        )
        object_deletion_service.begin_upload_put(old_claim)
        self._expire_job(old_claim.job_id)
        with patch.object(object_storage_service, "delete_all_object_versions"):
            self.assertEqual(
                object_deletion_service.process_object_deletion_jobs(self.db), 0
            )
            self._expire_job(old_claim.job_id)
            self.assertEqual(
                object_deletion_service.process_object_deletion_jobs(self.db), 1
            )

        replacement_claim = object_deletion_service.prepare_bucket_file_upload(
            self.source,
            old_file.id,
            old_file.filename,
            session_factory=self._session_factory,
        )
        replacement = self._bucket_file_for_claim(
            replacement_claim,
            version_id="version-2",
        )
        object_deletion_service.begin_upload_put(replacement_claim)
        self.db.add(replacement)
        object_deletion_service.retain_bucket_file_upload(
            self.db,
            replacement_claim,
            replacement,
            self.source,
        )
        self.db.commit()

        cleanup_job_id = object_deletion_service.enqueue_abandoned_upload_version(
            old_claim,
            object_version_id="version-1",
        )
        with patch.object(object_storage_service, "delete_object") as delete_object:
            self.assertEqual(
                object_deletion_service.process_object_deletion_jobs(
                    self.db,
                    job_ids=[cleanup_job_id],
                ),
                1,
            )

        delete_object.assert_called_once_with(
            old_claim.bucket_name,
            old_claim.object_key,
            version_id="version-1",
        )
        guard = self.db.get(ObjectDeletionJob, replacement_claim.job_id)
        self.assertEqual(guard.status, "retained")
        self.assertEqual(
            self.db.get(BucketFile, replacement.id).object_version_id,
            "version-2",
        )

    def test_unversioned_late_upload_cleanup_preserves_replacement_key(self) -> None:
        old_file = self._bucket_file()
        old_claim = object_deletion_service.prepare_bucket_file_upload(
            self.source,
            old_file.id,
            old_file.filename,
            session_factory=self._session_factory,
        )
        object_deletion_service.begin_upload_put(old_claim)
        self._expire_job(old_claim.job_id)
        with patch.object(object_storage_service, "delete_all_object_versions"):
            self.assertEqual(
                object_deletion_service.process_object_deletion_jobs(self.db), 0
            )
            self._expire_job(old_claim.job_id)
            self.assertEqual(
                object_deletion_service.process_object_deletion_jobs(self.db), 1
            )

        replacement_claim = object_deletion_service.prepare_bucket_file_upload(
            self.source,
            old_file.id,
            old_file.filename,
            session_factory=self._session_factory,
        )
        replacement = self._bucket_file_for_claim(
            replacement_claim,
            version_id="",
        )
        object_deletion_service.begin_upload_put(replacement_claim)
        self.db.add(replacement)
        object_deletion_service.retain_bucket_file_upload(
            self.db,
            replacement_claim,
            replacement,
            self.source,
        )
        self.db.commit()

        cleanup_job_id = object_deletion_service.enqueue_abandoned_upload_version(
            old_claim,
            object_version_id="",
        )
        with patch.object(object_storage_service, "delete_object") as delete_object:
            self.assertEqual(
                object_deletion_service.process_object_deletion_jobs(
                    self.db,
                    job_ids=[cleanup_job_id],
                ),
                1,
            )

        delete_object.assert_called_once_with(
            old_claim.bucket_name,
            old_claim.object_key,
            version_id="",
        )
        guard = self.db.get(ObjectDeletionJob, replacement_claim.job_id)
        self.assertEqual(guard.status, "retained")
        self.assertEqual(
            self.db.get(BucketFile, replacement.id).object_version_id,
            "",
        )

    def test_retain_lease_loss_schedules_unversioned_exact_key_cleanup(self) -> None:
        bucket_file = self._bucket_file()
        claim = object_deletion_service.prepare_bucket_file_upload(
            self.source,
            bucket_file.id,
            bucket_file.filename,
            session_factory=self._session_factory,
        )
        object_deletion_service.begin_upload_put(claim)
        guard = self.db.get(ObjectDeletionJob, claim.job_id)
        guard.status = "completed"
        guard.lease_token = ""
        guard.lease_generation += 1
        self.db.commit()
        late_result = self._bucket_file_for_claim(claim, version_id="")

        with self.assertRaises(
            object_deletion_service.UploadIntentLeaseLostError
        ):
            object_deletion_service.retain_bucket_file_upload(
                self.db,
                claim,
                late_result,
                self.source,
            )
        self.db.rollback()
        object_deletion_service.schedule_abandoned_upload_best_effort(
            claim,
            late_result,
        )
        cleanup_job = self.db.scalar(
            select(ObjectDeletionJob).where(
                ObjectDeletionJob.origin_type == "abandoned_upload_version",
                ObjectDeletionJob.object_key == claim.object_key,
            )
        )
        self.assertIsNotNone(cleanup_job)

        with patch.object(object_storage_service, "delete_object") as delete_object:
            self.assertEqual(
                object_deletion_service.process_object_deletion_jobs(
                    self.db,
                    job_ids=[cleanup_job.id],
                ),
                1,
            )
        delete_object.assert_called_once_with(
            claim.bucket_name,
            claim.object_key,
            version_id="",
        )


if __name__ == "__main__":
    unittest.main()
