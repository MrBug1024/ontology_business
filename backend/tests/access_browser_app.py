"""Run the actual application against a disposable PostgreSQL database for browser QA.

All identities are synthetic; outbound mail is mocked. Start with
`python -m tests.access_browser_app` and stop with the same command plus `stop`.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
STOP = ROOT / ".runtime" / "access-browser.stop"


def serve():
    from app.database import SessionLocal, init_db
    from app.models import User
    from app.services import auth_service, invitation_delivery_service
    from app.main import app
    from sqlalchemy import select
    from tests.access_fixtures import seed_access
    import uvicorn

    init_db()
    with SessionLocal() as db:
        f = seed_access(db)
        names = [(f.owner, "owner@access.example.test", "测试平台管理员"),
                 (f.recipient, "collaborator@access.example.test", "测试协作者"),
                 (f.stranger, "viewer@access.example.test", "测试查看者")]
        for user, email, name in names:
            user.email, user.display_name = email, name
            user.password_hash = auth_service.hash_password("Synthetic-browser-only-2026!")
        f.tenant.name = "协作验收工作区"
        f.organization.name = "协作验收工作区"
        db.commit()

    auth_service.send_verification_email = lambda *args, **kwargs: None
    invitation_delivery_service.send_mail_message = lambda *args, **kwargs: None

    @asynccontextmanager
    async def qa_lifespan(_):
        worker = asyncio.create_task(invitation_delivery_service.run_worker())
        try:
            yield
        finally:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
    # Use the real routes/middleware. The QA lifetime starts only the worker under
    # test, keeping unrelated external systems and business workers out of fixtures.
    app.router.lifespan_context = qa_lifespan
    uvicorn.run(app, host="127.0.0.1", port=18080, log_level="warning")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "stop":
        STOP.parent.mkdir(parents=True, exist_ok=True)
        STOP.touch()
        return
    from tests.access_postgresql import isolated_access_database
    STOP.parent.mkdir(parents=True, exist_ok=True)
    STOP.unlink(missing_ok=True)
    with isolated_access_database() as (url, _):
        environment = dict(os.environ, DATABASE_URL=url.render_as_string(hide_password=False),
            PUBLIC_APP_URL="http://127.0.0.1:5174", AUTH_COOKIE_SECURE="false", BOOTSTRAP_SUPERADMIN_EMAIL="")
        with (STOP.parent / "access-browser.log").open("w", encoding="utf-8") as log:
            child = subprocess.Popen([sys.executable, "-m", "tests.access_browser_app", "serve"],
                cwd=ROOT, env=environment, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                print("Isolated browser backend starting at http://127.0.0.1:18080; SMTP mocked", flush=True)
                while child.poll() is None and not STOP.exists():
                    time.sleep(1)
            finally:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=20)
                STOP.unlink(missing_ok=True)
    print("Isolated browser database removed", flush=True)


if __name__ == "__main__":
    main()
