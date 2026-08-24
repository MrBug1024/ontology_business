"""FastAPI 应用入口。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .database import init_db
from .routers import (
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
    datasource_service,
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    # 启动时同步技能（含复制进来的 ocr-parser）
    from .database import SessionLocal

    db = SessionLocal()
    try:
        permission_service.bootstrap_authorization(db)
        operations_service.purge_expired_assistant_attachments(db)
        datasource_service.reconcile_generated_file_orphans(db)
        db.commit()
        skill_service.sync_skills_to_db(db)
    finally:
        db.close()
    worker = asyncio.create_task(_operations_worker(), name="ontology-operations-worker")
    try:
        yield
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker


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


@app.get("/")
def root():
    return {"name": settings.app_name, "version": settings.app_version, "docs": "/docs"}


@app.get("/api/health")
def health():
    return {"status": "ok"}
