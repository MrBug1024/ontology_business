"""Dedicated durable-job worker for production deployments.

The API can still run background loops during local development.  Production
uses this module as one independently scalable process so Uvicorn workers only
serve HTTP/SSE requests.  PostgreSQL elects one active worker with an advisory
lock; a process crash releases that lock automatically.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from .config import get_settings
from .database import SessionLocal, acquire_background_worker_lease, init_db
from .routers import assistant
from .services import (
    object_storage_service,
    operations_service,
    permission_service,
    skill_service,
)


logger = logging.getLogger(__name__)


async def prepare_durable_worker_state() -> None:
    """Run one-time durable maintenance after this process owns the lease.

    This must not run before advisory-lock election: the same worker image may
    be replicated accidentally, and recovery/cleanup are database mutations.
    The API imports this helper only for local single-leader development mode.
    """
    db = SessionLocal()
    try:
        permission_service.bootstrap_authorization(db)
        operations_service.purge_expired_assistant_attachments(db)
        skill_service.sync_skills_to_db(db)
        db.commit()
    finally:
        db.close()

    try:
        await asyncio.to_thread(assistant.recover_expired_compilation_jobs)
    except Exception:  # noqa: BLE001 - the scheduled retry keeps jobs durable.
        logger.exception("AI 场景建模任务启动恢复失败")


async def run() -> None:
    settings = get_settings()
    init_db()
    lease = acquire_background_worker_lease()
    if not lease.acquired:
        raise RuntimeError("另一个后台 worker 已持有 PostgreSQL 租约")

    try:
        await asyncio.to_thread(lease.assert_held)
        configured_storage = object_storage_service.configuration()
        if configured_storage.configured:
            await asyncio.to_thread(
                object_storage_service.ensure_bucket,
                configured_storage.bucket_name,
            )
        await asyncio.to_thread(lease.assert_held)
        await prepare_durable_worker_state()
        await asyncio.to_thread(lease.assert_held)

        logger.info("后台 worker 已启动并取得 PostgreSQL 租约")
        next_compilation_recovery = (
            asyncio.get_running_loop().time()
            + settings.assistant_recovery_poll_seconds
        )
        while True:
            # Stop immediately after a failover/reconnect.  A replacement
            # PostgreSQL session cannot retain this process's advisory lock.
            await asyncio.to_thread(lease.assert_held)
            now = asyncio.get_running_loop().time()
            try:
                await asyncio.to_thread(operations_service.worker_tick)
            except Exception:  # noqa: BLE001 - the next durable poll can recover.
                logger.exception("后台任务 worker 轮询失败")
            if now >= next_compilation_recovery:
                try:
                    await asyncio.to_thread(assistant.recover_expired_compilation_jobs)
                except Exception:  # noqa: BLE001 - jobs retain their leases/provenance.
                    logger.exception("AI 场景建模任务恢复轮询失败")
                next_compilation_recovery = now + settings.assistant_recovery_poll_seconds
            await asyncio.sleep(settings.background_worker_poll_seconds)
    finally:
        with contextlib.suppress(Exception):
            lease.release()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())


if __name__ == "__main__":
    main()
