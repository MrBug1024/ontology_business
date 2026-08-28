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
from .database import engine, init_db
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
    scenarios,
    skills,
    templates,
)
from .services import (
    cache_service,
    object_storage_service,
    operations_service,
    permission_service,
    skill_service,
)


logger = logging.getLogger(__name__)


async def _operations_worker() -> None:
    """数据库驱动的 P1 worker；即使 API 重启，待处理任务仍会在下一次启动后继续。"""
    while True:
        try:
            await asyncio.to_thread(operations_service.worker_tick)
        except Exception:  # noqa: BLE001
            logger.exception("后台任务 worker 轮询失败")
        await asyncio.sleep(1)


async def _assistant_compilation_worker() -> None:
    """Continuously reclaim durable modelling jobs after crashes or restarts."""
    while True:
        try:
            await asyncio.to_thread(assistant.recover_expired_compilation_jobs)
        except Exception:  # noqa: BLE001
            logger.exception("AI 场景建模任务恢复轮询失败")
        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    logger.info("平台数据库、表结构和运行目录启动检查完成")
    configured_storage = object_storage_service.configuration()
    if configured_storage.configured:
        await asyncio.to_thread(
            object_storage_service.ensure_bucket,
            configured_storage.bucket_name,
        )
        logger.info("MinIO 托管文件桶启动检查完成")
    # 启动时同步技能（含复制进来的 ocr-parser）
    from .database import SessionLocal

    db = SessionLocal()
    try:
        permission_service.bootstrap_authorization(db)
        operations_service.purge_expired_assistant_attachments(db)
        db.commit()
        skill_service.sync_skills_to_db(db)
    finally:
        db.close()
    try:
        await asyncio.to_thread(assistant.recover_expired_compilation_jobs)
    except Exception:  # noqa: BLE001
        logger.exception("AI 场景建模任务启动恢复失败")
    async with agent_mcp_server.mcp_server.session_manager.run():
        operations_worker = asyncio.create_task(
            _operations_worker(),
            name="ontology-operations-worker",
        )
        compilation_worker = asyncio.create_task(
            _assistant_compilation_worker(),
            name="assistant-compilation-recovery-worker",
        )
        try:
            yield
        finally:
            operations_worker.cancel()
            compilation_worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await operations_worker
            with contextlib.suppress(asyncio.CancelledError):
                await compilation_worker


settings = get_settings()
app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(scenarios.router, prefix=settings.api_prefix)
app.include_router(auth.router, prefix=settings.api_prefix)
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
