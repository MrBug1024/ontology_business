"""Serve the real application against a disposable, synthetic PostgreSQL workspace.

Requires RUN_POSTGRESQL_INTEGRATION_TESTS=1 and uses the same migration fixture as
the acceptance tests. The normal login/session/Origin middleware remain active.
Other background workers and startup external I/O are excluded. An explicit
scripted-model option exercises the real investigation worker without an LLM API.
Send `stop` on stdin to remove the isolated database. No real credentials are printed.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from threading import Event, Thread

import uvicorn
from sqlalchemy.orm import Session

from isolated_postgresql import isolated_database, seed_workspace, tenant_session


def seed_distillation(engine, workspace: dict[str, str], *, reviewable=False) -> str:
    from app.distillation_schemas import DistillationDocument, ProjectCreate
    from app.services.distillation_service import create_project

    document = DistillationDocument.model_validate({
        "beneficiary": "业务请求人及处理负责人",
        "pain": "现有流程记录了请求，但无法确认问题是否最终解决",
        "desired_outcome": "请求得到核验、处理并向请求人确认结果",
        "success_metric": "从受理到结果确认的时间，以及已确认解决的请求比例",
        "scope": "研究一个有界的请求处理流程",
        "non_goals": "不自动执行业务操作，不将推断标记为事实",
        "evidence": [{"key": "observed_result", "title": "合成结果观察", "kind": "observation", "role": "result",
                      "summary": "演示样本只有归档记录，缺少请求人确认的解决结果", "limitations": "合成验收资料，不能证明真实业务事实"}],
        "as_is": {"nodes": [{"key": "intake", "name": "受理请求"}, {"key": "archive", "name": "归档记录"}],
                  "edges": [{"source": "intake", "target": "archive", "label": "登记后归档"}]},
        "to_be": {"nodes": [{"key": "verify", "name": "核验请求", "owner": "处理负责人", "outcome": "明确事实与责任"},
                             {"key": "resolve", "name": "确认解决结果", "owner": "请求人", "outcome": "形成确认的结果证据"}],
                  "edges": [{"source": "verify", "target": "resolve", "label": "完成处理后确认"}]},
        "entities": [{"key": "request", "name": "业务请求", "attributes": ["请求标识", "目标结果"]},
                     {"key": "resolution", "name": "解决结果", "attributes": ["结果状态", "确认时间"]}],
        "relations": [{"source": "request", "target": "resolution", "label": "对应结果", "cardinality": "one_to_one"}],
        "lineage": [{"source": "request", "target": "resolution", "transformation": "经核验、处理与确认形成结果证据", "evidence_refs": ["observed_result"]}],
        "decision": "undecided",
        "open_questions": ["由谁确认结果，以及怎样验证改善是否有效？"],
    })
    if reviewable:
        from discovery_fixtures import reviewed_document

        document = reviewed_document()
        document.open_questions = ["由谁确认结果，以及怎样验证改善是否有效？"]
    with tenant_session(engine, workspace) as db:
        project = create_project(db, ProjectCreate(name="Browser acceptance", scenario_id=workspace["scenario_id"], document=document))
        db.commit()
        return project.id


def stop_from_stdin(server: uvicorn.Server) -> None:
    """Support deterministic cleanup when Windows PTYs do not deliver Ctrl-C."""
    for line in sys.stdin:
        if line.strip() == "stop":
            server.should_exit = True
            return


def run_investigation_worker(stop: Event) -> None:
    from app.services.distillation_conversation_worker import process_next_turn

    while not stop.is_set():
        if not process_next_turn():
            stop.wait(0.25)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8012)
    parser.add_argument("--frontend-port", type=int, default=5175)
    parser.add_argument("--seed-project", action="store_true", help="Create an undecided synthetic discovery project")
    parser.add_argument("--reviewable-project", action="store_true", help="Seed paired synthetic evidence for artifact review acceptance")
    parser.add_argument("--scripted-distillation-llm", action="store_true",
                        help="Use the test-only scripted model with the real persistent investigation worker")
    arguments = parser.parse_args()
    if not all(1024 <= value <= 65535 for value in (arguments.port, arguments.frontend_port)):
        parser.error("Ports must be between 1024 and 65535")

    with isolated_database() as isolated:
        from app import database
        from app.config import get_settings
        from app.models import User

        settings = get_settings()
        settings.database_url = isolated.runtime_engine.url.render_as_string(hide_password=False)
        settings.postgresql_database = isolated.runtime_engine.url.database or ""
        settings.public_app_url = f"http://127.0.0.1:{arguments.frontend_port}"
        settings.cors_origins = [settings.public_app_url]
        settings.auth_cookie_name = "ontology_acceptance_session"
        settings.auth_cookie_secure = False
        settings.bootstrap_superadmin_email = ""
        settings.redis_host = ""
        settings.minio_aliyun_endpoint = ""
        settings.minio_aliyun_access_key_id = ""
        settings.minio_aliyun_access_key_secret = ""
        settings.mail_server = ""
        settings.mail_username = ""
        settings.mail_password = ""
        database.engine.dispose()
        database.engine = isolated.runtime_engine
        database.SessionLocal.configure(bind=isolated.runtime_engine)

        from app.main import app
        from app.services.auth_service import hash_password
        from app.services import permission_service

        database.init_db()
        workspace = seed_workspace(isolated.admin_engine)
        password = "Aa1!" + secrets.token_urlsafe(24)
        with Session(isolated.admin_engine) as db:
            # The isolated server skips lifespan to keep unrelated external
            # workers off. Complete the same role bootstrap before concurrent
            # browser requests arrive; real startup performs this itself.
            permission_service.ensure_organization(db, workspace["tenant_id"])
            user = db.get(User, workspace["user_id"])
            user.password_hash = hash_password(password)
            email = user.email
            db.commit()
        login_details = {
            "purpose": "Disposable browser acceptance; synthetic login only",
            "api": f"http://127.0.0.1:{arguments.port}",
            "frontend": settings.public_app_url,
            "email": email,
            "password": password,
            "scenario_id": workspace["scenario_id"],
            "other_scenario_id": workspace["other_scenario_id"],
        }
        if arguments.seed_project:
            login_details["project_id"] = seed_distillation(isolated.runtime_engine, workspace, reviewable=arguments.reviewable_project)
        restore_model = None
        worker_stop = Event()
        worker = None
        if arguments.scripted_distillation_llm:
            from distillation_scripted_llm import install_scripted_llm

            restore_model = install_scripted_llm(isolated.admin_engine, workspace)
            worker = Thread(target=run_investigation_worker, args=(worker_stop,), daemon=True)
            worker.start()
            login_details["model"] = "Scripted synthetic acceptance model; no external LLM request"
        # One short field per line avoids terminal wrapping obscuring synthetic
        # login values during an interactive browser acceptance session.
        print(json.dumps(login_details, indent=2), flush=True)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=arguments.port, lifespan="off", log_level="warning"))
        Thread(target=stop_from_stdin, args=(server,), daemon=True).start()
        try:
            server.run()
        finally:
            worker_stop.set()
            if worker is not None:
                worker.join(timeout=10)
            if restore_model is not None:
                restore_model()


if __name__ == "__main__":
    main()
