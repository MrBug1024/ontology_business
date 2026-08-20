"""FastAPI 应用入口。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .database import init_db
from .routers import agents, assistant, auth, data_sources, llm_configs, mcp, scenarios, skills
from .services import skill_service


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    # 启动时同步技能（含复制进来的 ocr-parser）
    from .database import SessionLocal

    db = SessionLocal()
    try:
        skill_service.sync_skills_to_db(db)
    finally:
        db.close()
    yield


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
app.include_router(mcp.router, prefix=settings.api_prefix)
app.include_router(agents.router, prefix=settings.api_prefix)
app.include_router(assistant.router, prefix=settings.api_prefix)


@app.get("/")
def root():
    return {"name": settings.app_name, "version": settings.app_version, "docs": "/docs"}


@app.get("/api/health")
def health():
    return {"status": "ok"}
