"""FastAPI 应用入口。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .config import get_settings
from .database import (
    BackgroundWorkerLease,
    BackgroundWorkerLeaseLostError,
    acquire_background_worker_lease,
    engine,
    init_db,
)
from .worker import prepare_durable_worker_state
from . import agent_mcp_server
from .routers import (
    agent_mcp,
    agents,
    assistant,
    auth,
    data_sources,
    external_api,
    functions,
    llm_configs,
    mcp,
    operations,
    organization,
    scenarios,
    skills,
    templates,
)
from .services import (
    cache_service,
    object_storage_service,
    operations_service,
)


logger = logging.getLogger(__name__)


async def _operations_worker(worker_lease: BackgroundWorkerLease) -> None:
    """数据库驱动的 P1 worker；即使 API 重启，待处理任务仍会在下一次启动后继续。"""
    while True:
        try:
            await asyncio.to_thread(worker_lease.assert_held)
        except BackgroundWorkerLeaseLostError:
            logger.critical("后台 worker PostgreSQL 租约已丢失，停止旧进程任务调度")
            return
        try:
            await asyncio.to_thread(operations_service.worker_tick)
        except Exception:  # noqa: BLE001
            logger.exception("后台任务 worker 轮询失败")
        await asyncio.sleep(settings.background_worker_poll_seconds)


async def _assistant_compilation_worker(worker_lease: BackgroundWorkerLease) -> None:
    """Continuously reclaim durable modelling jobs after crashes or restarts."""
    # The lease holder performs one synchronous recovery before this task is
    # created.  Waiting here avoids a second claim pass during the same startup.
    await asyncio.sleep(settings.assistant_recovery_poll_seconds)
    while True:
        try:
            await asyncio.to_thread(worker_lease.assert_held)
        except BackgroundWorkerLeaseLostError:
            logger.critical("后台 worker PostgreSQL 租约已丢失，停止旧进程建模恢复")
            return
        try:
            await asyncio.to_thread(assistant.recover_expired_compilation_jobs)
        except Exception:  # noqa: BLE001
            logger.exception("AI 场景建模任务恢复轮询失败")
        await asyncio.sleep(settings.assistant_recovery_poll_seconds)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    logger.info("平台数据库、表结构和运行目录启动检查完成")
    worker_lease = None
    operations_worker: asyncio.Task[None] | None = None
    compilation_worker: asyncio.Task[None] | None = None
    if settings.run_background_workers:
        worker_lease = acquire_background_worker_lease()
        if worker_lease.acquired:
            # Only the lease holder may mutate durable job state or resubmit
            # expired work.  Under Uvicorn --workers this prevents every API
            # process from racing the same startup recovery path.
            try:
                await asyncio.to_thread(worker_lease.assert_held)
                configured_storage = object_storage_service.configuration()
                if configured_storage.configured:
                    await asyncio.to_thread(
                        object_storage_service.ensure_bucket,
                        configured_storage.bucket_name,
                    )
                    logger.info("MinIO 托管文件桶启动检查完成")
                await asyncio.to_thread(worker_lease.assert_held)
                await prepare_durable_worker_state()
                await asyncio.to_thread(worker_lease.assert_held)
            except Exception:
                worker_lease.release()
                raise
            operations_worker = asyncio.create_task(
                _operations_worker(worker_lease),
                name="ontology-operations-worker",
            )
            compilation_worker = asyncio.create_task(
                _assistant_compilation_worker(worker_lease),
                name="assistant-compilation-recovery-worker",
            )
            logger.info("当前进程已取得后台任务 worker 租约")
        else:
            logger.info("后台任务 worker 已由另一个进程持有；当前 API 仅处理请求")
    try:
        async with agent_mcp_server.mcp_server.session_manager.run():
            yield
    finally:
        if operations_worker is not None:
            operations_worker.cancel()
        if compilation_worker is not None:
            compilation_worker.cancel()
        if operations_worker is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await operations_worker
        if compilation_worker is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await compilation_worker
        if worker_lease is not None:
            worker_lease.release()


settings = get_settings()
app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)


@app.middleware("http")
async def reject_oversized_request_body(request, call_next):
    """Fail oversized uploads before multipart parsing consumes worker memory.

    Multipart framing adds a small amount of transport overhead beyond an
    individual file's 400 MiB allowance, so the fast-path leaves 1 MiB for
    boundaries and form fields.  Each upload route still applies the exact
    per-file limit while reading the stream.
    """
    raw_length = request.headers.get("content-length", "").strip()
    if raw_length:
        try:
            content_length = int(raw_length)
        except ValueError:
            return JSONResponse({"detail": "Content-Length 格式无效"}, status_code=400)
        if content_length < 0:
            return JSONResponse({"detail": "Content-Length 格式无效"}, status_code=400)
        if content_length > settings.max_upload_bytes + 1024 * 1024:
            return JSONResponse(
                {"detail": f"请求超过大小限制（{settings.max_upload_bytes // (1024 * 1024)} MB）"},
                status_code=413,
            )
    return await call_next(request)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id"],
)

app.include_router(scenarios.router, prefix=settings.api_prefix)
app.include_router(auth.router, prefix=settings.api_prefix)
app.include_router(organization.router, prefix=settings.api_prefix)
app.include_router(data_sources.router, prefix=settings.api_prefix)
app.include_router(llm_configs.router, prefix=settings.api_prefix)
app.include_router(skills.router, prefix=settings.api_prefix)
app.include_router(templates.router, prefix=settings.api_prefix)
app.include_router(mcp.router, prefix=settings.api_prefix)
app.include_router(agents.router, prefix=settings.api_prefix)
app.include_router(assistant.router, prefix=settings.api_prefix)
app.include_router(operations.router, prefix=settings.api_prefix)
app.include_router(operations.operations_router, prefix=settings.api_prefix)
app.include_router(functions.router, prefix=settings.api_prefix)
app.include_router(external_api.management_router, prefix=settings.api_prefix)
app.include_router(external_api.router, prefix=settings.api_prefix)
app.include_router(agent_mcp.router, prefix=settings.api_prefix)


@app.get("/")
def root():
    return {"name": settings.app_name, "version": settings.app_version, "docs": "/docs"}


@app.get("/api/health")
def health():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        database_ok = True
    except Exception:  # noqa: BLE001 - never expose a connection diagnostic.
        database_ok = False

    minio_requested = any(
        str(value or "").strip()
        for value in (
            settings.minio_aliyun_endpoint,
            settings.minio_aliyun_access_key_id,
            settings.minio_aliyun_access_key_secret,
            settings.minio_bucketname,
        )
    )
    minio_required = True
    try:
        minio_configured = object_storage_service.configuration().configured
        minio_declared = minio_configured or minio_requested
        minio_ok = (
            object_storage_service.healthcheck()
            if minio_configured
            else not minio_required and not minio_requested
        )
    except object_storage_service.ObjectStorageError:
        # An unsafe/incomplete storage endpoint is a failed dependency, but its
        # configuration details never belong in an unauthenticated health body.
        minio_configured = False
        minio_declared = True
        minio_ok = False

    redis_configured = settings.redis_configured
    redis_ok = cache_service.healthcheck() if redis_configured else True
    critical_ok = database_ok and minio_ok
    status = (
        "unavailable"
        if not critical_ok
        else "degraded"
        if redis_configured and not redis_ok
        else "ok"
    )
    payload = {
        "status": status,
        "dependencies": {
            "database": {
                "configured": True,
                "status": "ok" if database_ok else "error",
                "backend": engine.dialect.name,
            },
            "redis": {
                "configured": redis_configured,
                "status": (
                    "ok" if redis_configured and redis_ok
                    else "error" if redis_configured
                    else "disabled"
                ),
                "authoritative": False,
            },
            "minio": {
                "configured": minio_configured,
                "required": minio_required,
                "status": (
                    "ok" if minio_configured and minio_ok
                    else "error" if minio_declared or minio_required
                    else "disabled"
                ),
            }
        },
    }
    return JSONResponse(payload, status_code=200 if critical_ok else 503)


# Keep the gateway mount last: its root mount must not shadow browser/API routes.
app.mount("/", agent_mcp_server.mcp_app)
