from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from app import database
from app.database import Base
from app.models import (
    AssistantCompilationJob,
    AssistantMessage,
    AssistantThread,
    BusinessScenario,
    Tenant,
    User,
)
from app.services import assistant_compilation_job_service as job_service


class AssistantCompilationJobLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        database_path = Path(self.temp_dir.name) / "compilation-leases.sqlite3"
        self.engine = create_engine(
            f"sqlite:///{database_path.as_posix()}",
            connect_args={"check_same_thread": False, "timeout": 15},
        )

        @event.listens_for(self.engine, "connect")
        def _enable_foreign_keys(connection, _record) -> None:
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        self.db = self.factory()
        self.tenant = Tenant(id="tenant-lease", name="Lease tenant")
        self.owner = User(
            id="user-lease-owner",
            tenant_id=self.tenant.id,
            email="lease-owner@example.test",
            password_hash="test-only",
        )
        self.other_user = User(
            id="user-lease-other",
            tenant_id=self.tenant.id,
            email="lease-other@example.test",
            password_hash="test-only",
        )
        self.scenario = BusinessScenario(
            id="scenario-lease",
            tenant_id=self.tenant.id,
            name="Lease scenario",
        )
        self.db.add_all([
            self.tenant,
            self.owner,
            self.other_user,
            self.scenario,
        ])
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _claim(
        self,
        request_id: str,
        *,
        execution_input: dict | None = None,
    ) -> AssistantCompilationJob:
        thread = AssistantThread(
            tenant_id=self.tenant.id,
            created_by_user_id=self.owner.id,
            scenario_id=self.scenario.id,
            scope_key=f"scenario:{self.scenario.id}|request:{request_id}",
            title="Compilation lease",
        )
        self.db.add(thread)
        self.db.flush()
        message = AssistantMessage(
            thread_id=thread.id,
            role="user",
            content="Build the model",
        )
        self.db.add(message)
        self.db.commit()
        identity = job_service.build_compilation_identity(
            tenant_id=self.tenant.id,
            user_id=self.owner.id,
            scenario_id=self.scenario.id,
            message=message.content,
            attachments=[],
            llm=None,
            compiler_version="lease-test-v1",
            scenario_baseline="baseline-v1",
            request_id=request_id,
            execution_policy={"llm_call_budget": 2},
        )
        job, acquired = job_service.claim_compilation(
            self.db,
            identity=identity,
            tenant_id=self.tenant.id,
            user_id=self.owner.id,
            scenario_id=self.scenario.id,
            thread_id=thread.id,
            message_id=message.id,
            compiler_version="lease-test-v1",
            scenario_baseline="baseline-v1",
            llm_call_budget=2,
            execution_input=execution_input,
        )
        self.assertTrue(acquired)
        return job

    def test_execution_input_is_owner_private_and_lease_capability_bound(self) -> None:
        private_input = {
            "message": "private business instructions",
            "documents": [{"text": "sensitive attachment text"}],
        }
        job = self._claim("private-input", execution_input=private_input)

        loaded = job_service.load_owner_execution_input(
            self.db,
            job.id,
            tenant_id=self.tenant.id,
            created_by_user_id=self.owner.id,
        )
        self.assertEqual(loaded, private_input)
        loaded["documents"][0]["text"] = "mutated copy"
        self.assertEqual(
            job_service.load_owner_execution_input(
                self.db,
                job.id,
                tenant_id=self.tenant.id,
                created_by_user_id=self.owner.id,
            ),
            private_input,
        )
        with self.assertRaises(LookupError):
            job_service.load_owner_execution_input(
                self.db,
                job.id,
                tenant_id=self.tenant.id,
                created_by_user_id=self.other_user.id,
            )

        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        lease = job_service.acquire_compilation_lease(
            self.db,
            job.id,
            tenant_id=self.tenant.id,
            created_by_user_id=self.owner.id,
            token="a" * 64,
            lease_seconds=30,
            as_of=now,
        )
        self.assertIsNotNone(lease)
        self.assertEqual(
            job_service.load_leased_execution_input(
                self.db,
                job.id,
                token=lease.token,
                attempt=lease.attempt,
                as_of=now + timedelta(seconds=1),
            ),
            private_input,
        )
        self.assertTrue(job_service.release_compilation_lease(
            self.db,
            job.id,
            token=lease.token,
            attempt=lease.attempt,
            as_of=now + timedelta(seconds=2),
        ))
        with self.assertRaises(job_service.CompilationLeaseLost):
            job_service.load_leased_execution_input(
                self.db,
                job.id,
                token=lease.token,
                attempt=lease.attempt,
                as_of=now + timedelta(seconds=3),
            )

    def test_two_sessions_cannot_acquire_the_same_available_lease(self) -> None:
        job = self._claim("concurrent-lease")
        barrier = threading.Barrier(2)
        now = datetime(2026, 2, 1, tzinfo=timezone.utc)

        def acquire(token: str):
            with self.factory() as db:
                barrier.wait(timeout=10)
                return job_service.acquire_compilation_lease(
                    db,
                    job.id,
                    tenant_id=self.tenant.id,
                    created_by_user_id=self.owner.id,
                    token=token,
                    lease_seconds=60,
                    as_of=now,
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(acquire, ("b" * 64, "c" * 64)))

        winners = [lease for lease in results if lease is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0].attempt, 1)
        self.db.expire_all()
        persisted = self.db.get(AssistantCompilationJob, job.id)
        self.assertEqual(persisted.lease_token, winners[0].token)
        self.assertEqual(persisted.lease_attempt, 1)
        self.assertIsNone(job_service.acquire_compilation_lease(
            self.db,
            job.id,
            tenant_id=self.tenant.id,
            created_by_user_id=self.owner.id,
            token="d" * 64,
            lease_seconds=60,
            as_of=now + timedelta(seconds=1),
        ))

    def test_expiry_takeover_fences_stale_worker_and_terminal_clears_lease(self) -> None:
        job = self._claim(
            "takeover",
            execution_input={"message": "restart me"},
        )
        now = datetime(2026, 3, 1, tzinfo=timezone.utc)
        first = job_service.acquire_compilation_lease(
            self.db,
            job.id,
            tenant_id=self.tenant.id,
            created_by_user_id=self.owner.id,
            token="e" * 64,
            lease_seconds=10,
            as_of=now,
        )
        renewed = job_service.renew_compilation_lease(
            self.db,
            job.id,
            token=first.token,
            attempt=first.attempt,
            lease_seconds=10,
            as_of=now + timedelta(seconds=5),
        )
        self.assertEqual(renewed.expires_at, now + timedelta(seconds=15))
        self.assertIsNone(job_service.acquire_compilation_lease(
            self.db,
            job.id,
            tenant_id=self.tenant.id,
            created_by_user_id=self.owner.id,
            token="f" * 64,
            lease_seconds=10,
            as_of=now + timedelta(seconds=11),
        ))

        takeover = job_service.acquire_compilation_lease(
            self.db,
            job.id,
            tenant_id=self.tenant.id,
            created_by_user_id=self.owner.id,
            token="f" * 64,
            lease_seconds=20,
            as_of=now + timedelta(seconds=16),
        )
        self.assertIsNotNone(takeover)
        self.assertEqual(takeover.attempt, first.attempt + 1)
        self.assertIsNone(job_service.renew_compilation_lease(
            self.db,
            job.id,
            token=first.token,
            attempt=first.attempt,
            as_of=now + timedelta(seconds=17),
        ))
        self.assertFalse(job_service.release_compilation_lease(
            self.db,
            job.id,
            token=first.token,
            attempt=first.attempt,
            as_of=now + timedelta(seconds=17),
        ))
        with self.assertRaises(job_service.CompilationLeaseLost):
            job_service.mark_succeeded(
                self.db,
                job.id,
                result={"worker": "stale"},
                lease_token=first.token,
                lease_attempt=first.attempt,
                as_of=now + timedelta(seconds=17),
            )

        completed = job_service.mark_succeeded(
            self.db,
            job.id,
            result={"worker": "takeover"},
            lease_token=takeover.token,
            lease_attempt=takeover.attempt,
            as_of=now + timedelta(seconds=18),
        )
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(completed.result, {"worker": "takeover"})
        self.assertEqual(completed.lease_token, "")
        self.assertIsNone(completed.lease_expires_at)
        self.assertEqual(completed.execution_input, {})
        same_worker_replay = job_service.mark_succeeded(
            self.db,
            job.id,
            result={"worker": "ignored retry"},
            lease_token=takeover.token,
            lease_attempt=takeover.attempt,
            as_of=now + timedelta(seconds=19),
        )
        self.assertEqual(same_worker_replay.result, {"worker": "takeover"})
        stale_terminal_replay = job_service.mark_failed(
            self.db,
            job.id,
            error="late stale worker failure",
            lease_token=first.token,
            lease_attempt=first.attempt,
            as_of=now + timedelta(seconds=19),
        )
        self.assertEqual(stale_terminal_replay.status, "succeeded")
        self.assertEqual(stale_terminal_replay.result, {"worker": "takeover"})
        self.db.expire_all()
        self.assertEqual(
            self.db.get(AssistantCompilationJob, job.id).result,
            {"worker": "takeover"},
        )

        failed_job = self._claim("terminal-failure")
        failure_lease = job_service.acquire_compilation_lease(
            self.db,
            failed_job.id,
            tenant_id=self.tenant.id,
            created_by_user_id=self.owner.id,
            token="1" * 64,
            as_of=now,
        )
        failed = job_service.mark_failed(
            self.db,
            failed_job.id,
            error="provider unavailable",
            lease_token=failure_lease.token,
            lease_attempt=failure_lease.attempt,
            as_of=now + timedelta(seconds=1),
        )
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.lease_token, "")
        self.assertIsNone(failed.lease_expires_at)
        self.assertEqual(failed.execution_input, {})
        same_failure_replay = job_service.mark_failed(
            self.db,
            failed_job.id,
            error="ignored same-worker retry",
            lease_token=failure_lease.token,
            lease_attempt=failure_lease.attempt,
            as_of=now + timedelta(seconds=2),
        )
        self.assertEqual(same_failure_replay.error, "provider unavailable")
        failed_replay = job_service.mark_failed(
            self.db,
            failed_job.id,
            error="stale terminal overwrite",
            lease_token="0" * 64,
            lease_attempt=0,
            as_of=now + timedelta(seconds=2),
        )
        self.assertEqual(failed_replay.status, "failed")
        self.assertEqual(failed_replay.error, "provider unavailable")

    def test_terminal_jobs_scrub_restart_input_and_thread_deletion_retains_only_safe_ledger(self) -> None:
        private_input = {
            "message": "sensitive instructions" * 1_000,
            "documents": [{"text": "private attachment" * 1_000}],
        }
        succeeded_job = self._claim(
            "terminal-retention-success",
            execution_input=private_input,
        )
        succeeded = job_service.mark_succeeded(
            self.db,
            succeeded_job.id,
            result={"proposal_id": "safe-result"},
        )
        self.assertEqual(succeeded.execution_input, {})
        self.assertEqual(succeeded.result, {"proposal_id": "safe-result"})

        # Historic terminal rows are scrubbed on an idempotent terminal write as
        # well, so a retry/repair cannot preserve legacy private input forever.
        succeeded.execution_input = private_input
        self.db.commit()
        replayed = job_service.mark_succeeded(
            self.db,
            succeeded.id,
            result={"proposal_id": "ignored-replay"},
        )
        self.assertEqual(replayed.execution_input, {})
        self.assertEqual(replayed.result, {"proposal_id": "safe-result"})

        thread_id = str(replayed.thread_id)
        self.db.delete(self.db.get(AssistantThread, thread_id))
        self.db.commit()
        self.db.expire_all()
        retained = self.db.get(AssistantCompilationJob, replayed.id)
        self.assertIsNotNone(retained)
        self.assertIsNone(retained.thread_id)
        self.assertIsNone(retained.message_id)
        self.assertEqual(retained.execution_input, {})
        self.assertEqual(retained.result, {"proposal_id": "safe-result"})

        cancelled_job = self._claim(
            "terminal-retention-cancelled-stream",
            execution_input=private_input,
        )
        cancelled = job_service.mark_failed(
            self.db,
            cancelled_job.id,
            error="客户端连接已断开",
        )
        self.assertEqual(cancelled.status, "failed")
        self.assertEqual(cancelled.execution_input, {})
        self.assertEqual(
            cancelled.progress["error_code"],
            job_service.ERROR_CLIENT_DISCONNECTED,
        )

        failed_job = self._claim(
            "terminal-retention-failure",
            execution_input=private_input,
        )
        failed = job_service.mark_failed(
            self.db,
            failed_job.id,
            error="provider failed with private context",
        )
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.execution_input, {})
        failed.execution_input = private_input
        self.db.commit()
        failed_replay = job_service.mark_failed(
            self.db,
            failed.id,
            error="ignored terminal replay",
        )
        self.assertEqual(failed_replay.execution_input, {})
        self.assertIn("provider failed", failed_replay.error)

    def test_expired_query_and_batch_claim_do_not_return_private_input(self) -> None:
        first_job = self._claim(
            "batch-first",
            execution_input={"secret": "first"},
        )
        second_job = self._claim(
            "batch-second",
            execution_input={"secret": "second"},
        )
        now = datetime(2026, 4, 1, tzinfo=timezone.utc)
        first_lease = job_service.acquire_compilation_lease(
            self.db,
            first_job.id,
            tenant_id=self.tenant.id,
            created_by_user_id=self.owner.id,
            token="2" * 64,
            lease_seconds=10,
            as_of=now,
        )
        self.assertEqual(
            job_service.expired_running_job_ids(
                self.db,
                as_of=now + timedelta(seconds=5),
            ),
            [second_job.id],
        )
        self.assertEqual(
            set(job_service.expired_running_job_ids(
                self.db,
                as_of=now + timedelta(seconds=11),
            )),
            {first_job.id, second_job.id},
        )

        leases = job_service.claim_expired_running_jobs(
            self.db,
            lease_seconds=30,
            as_of=now + timedelta(seconds=11),
            limit=2,
        )
        self.assertEqual({lease.job_id for lease in leases}, {
            first_job.id,
            second_job.id,
        })
        self.assertTrue(all(
            not hasattr(lease, "execution_input") for lease in leases
        ))
        attempts = {lease.job_id: lease.attempt for lease in leases}
        self.assertEqual(attempts[first_job.id], first_lease.attempt + 1)
        self.assertEqual(attempts[second_job.id], 1)
        self.assertEqual(
            job_service.expired_running_job_ids(
                self.db,
                as_of=now + timedelta(seconds=12),
            ),
            [],
        )


class AssistantCompilationJobLeaseMigrationTests(unittest.TestCase):
    def test_legacy_rows_receive_safe_restart_defaults_idempotently(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            path = Path(temp_dir) / "legacy-compilation-jobs.sqlite3"
            engine = create_engine(f"sqlite:///{path.as_posix()}")
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "CREATE TABLE assistant_compilation_jobs ("
                    "id VARCHAR(32) PRIMARY KEY, "
                    "status VARCHAR(20), "
                    "error TEXT, completed_at TIMESTAMP, "
                    "created_at TIMESTAMP)"
                )
                connection.exec_driver_sql(
                    "INSERT INTO assistant_compilation_jobs "
                    "(id, status, error, created_at) VALUES "
                    "('legacy-job', 'running', '', CURRENT_TIMESTAMP), "
                    "('legacy-job-2', 'succeeded', '', CURRENT_TIMESTAMP)"
                )
                connection.exec_driver_sql(
                    "CREATE TABLE assistant_attachments ("
                    "id VARCHAR(32) PRIMARY KEY, parsed_text TEXT)"
                )
                connection.exec_driver_sql(
                    "INSERT INTO assistant_attachments (id, parsed_text) VALUES "
                    "('legacy-attachment', '这是解析文本，不是原始上传字节')"
                )
            with patch.object(database, "engine", engine):
                database._migrate_assistant_compilation_jobs()
                database._migrate_assistant_compilation_jobs()

            inspector = inspect(engine)
            columns = {
                item["name"]
                for item in inspector.get_columns("assistant_compilation_jobs")
            }
            self.assertTrue({
                "request_fingerprint",
                "execution_input",
                "lease_token",
                "lease_expires_at",
                "lease_attempt",
            } <= columns)
            indexes = {
                item.get("name")
                for item in inspector.get_indexes("assistant_compilation_jobs")
            }
            self.assertIn(
                "ix_assistant_compilation_jobs_status_lease_expiry",
                indexes,
            )
            guards = indexes | {
                item.get("name")
                for item in inspector.get_unique_constraints(
                    "assistant_compilation_jobs"
                )
            }
            self.assertIn(
                "uq_assistant_compilation_jobs_fingerprint",
                guards,
            )
            attachment_columns = {
                item["name"]
                for item in inspector.get_columns("assistant_attachments")
            }
            self.assertIn("content_hash", attachment_columns)
            self.assertIn(
                "ix_assistant_attachments_content_hash",
                {
                    item.get("name")
                    for item in inspector.get_indexes("assistant_attachments")
                },
            )
            with engine.connect() as connection:
                row = connection.exec_driver_sql(
                    "SELECT request_fingerprint, execution_input, lease_token, "
                    "lease_expires_at, lease_attempt, status, error, completed_at "
                    "FROM assistant_compilation_jobs "
                    "WHERE id = 'legacy-job'"
                ).one()
                content_hash = connection.exec_driver_sql(
                    "SELECT content_hash FROM assistant_attachments "
                    "WHERE id = 'legacy-attachment'"
                ).scalar_one()
                fingerprint_count = connection.exec_driver_sql(
                    "SELECT COUNT(DISTINCT request_fingerprint) "
                    "FROM assistant_compilation_jobs"
                ).scalar_one()
                historic_status = connection.exec_driver_sql(
                    "SELECT status FROM assistant_compilation_jobs "
                    "WHERE id = 'legacy-job-2'"
                ).scalar_one()
            expected_fingerprint = hashlib.sha256(
                "ontology-platform:assistant-compilation-job:"
                "legacy-request-fingerprint:v1:legacy-job".encode("utf-8")
            ).hexdigest()
            self.assertEqual(row.request_fingerprint, expected_fingerprint)
            self.assertEqual(json.loads(row.execution_input), {})
            self.assertEqual(row.lease_token, "")
            self.assertIsNone(row.lease_expires_at)
            self.assertEqual(row.lease_attempt, 0)
            self.assertEqual(row.status, "failed")
            self.assertIn("缺少可验证执行输入", row.error)
            self.assertIsNotNone(row.completed_at)
            self.assertEqual(content_hash, "")
            self.assertEqual(fingerprint_count, 2)
            self.assertEqual(historic_status, "succeeded")
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
