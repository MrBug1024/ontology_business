"""Actual routes, worker and PostgreSQL; deterministic model for browser stream QA.

Run `python -m tests.agent_streaming_browser_app`, then use the frontend proxy
at http://127.0.0.1:5175 -> http://127.0.0.1:18081. Stop with the same command
plus `stop`. The database is created and removed by the isolated test helper.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
STOP = ROOT / ".runtime" / "agent-stream-browser.stop"


def serve():
    from app.database import SessionLocal, init_db
    from app.main import app, _agent_turn_worker, _operations_worker, _managed_upload_worker
    from app.models import OntologyWorkflow, OntologyEvent
    from app.services import auth_service, llm_service
    from tests.test_agent_runtime_adapter import _world
    import uvicorn

    init_db()
    with SessionLocal() as db:
        _, user, scenario, _, _, agent = _world(db, "stream-qa")
        user.email = "stream@qa.example.test"
        user.email_verified_at = datetime.now(timezone.utc)
        user.password_hash = auth_service.hash_password("Synthetic-stream-only-2026!")
        scenario.name, agent.name = "消息能力验收场景", "消息能力验收 Agent"
        event = OntologyEvent(id="channel-qa-event", scenario_id=scenario.id, name="验收事件", enabled=True)
        db.add(event)
        workflow_ids = []
        for key, title, middle in (
            ("analysis", "分析核对", []),
            ("effect", "执行登记", [{"id": "event", "type": "event", "data": {"event_id": event.id, "payload": {}}}]),
            ("approval", "文件审批", [{"id": "review", "type": "approval", "data": {
                "name": "资料复核", "instructions": "请核对并提交佐证文件", "timeout_seconds": 1800,
                "approver_user_ids": [user.id], "requires_evidence": True}}]),
        ):
            nodes = [{"id": "start", "type": "start", "data": {}}] + middle + [{"id": "end", "type": "end", "data": {"summary": title + "已完成，结果已核实。"}}]
            workflow = OntologyWorkflow(id="channel-qa-" + key, scenario_id=scenario.id, name=title,
                nodes=nodes, edges=[{"id": f"e-{i}", "source": left["id"], "target": right["id"], "label": ""}
                for i, (left, right) in enumerate(zip(nodes, nodes[1:]))], enabled=True, status="active")
            db.add(workflow)
            workflow_ids.append(workflow.id)
        agent.capability_scope = {**agent.capability_scope, "workflows": {"mode": "explicit", "selected_ids": workflow_ids}}
        db.commit()

    def model_stream(_cfg, messages, **_kwargs):
        last_user = next((str(item.get("content", "")) for item in reversed(messages) if item.get("role") == "user"), "")
        selected = next((key for word, key in (("分析核对", "analysis"), ("执行登记", "effect"), ("文件审批", "approval")) if word in last_user), None)
        if selected:
            if messages[-1].get("role") == "tool":
                yield {"type": "token", "content": "已收到，本次处理进展如下。"}
            else:
                yield {"type": "tool_calls", "tool_calls": [{"id": "qa-invocation", "function": {
                    "name": "invoke_capability", "arguments": {"kind": "workflow", "key": "channel-qa-" + selected, "inputs": {}}}}]}
            return
        yield {"type": "token", "content": "正文已经开始输出，任务仍在处理中。\n\n"}
        for index in range(1, 41):
            time.sleep(0.45)
            yield {"type": "token", "content": f"第 {index} 段：这是可恢复的流式正文。😀\n"}
        yield {"type": "token", "content": "\n全部输出完成。"}

    llm_service.chat_stream = model_stream

    @asynccontextmanager
    async def lifespan(_):
        workers = [asyncio.create_task(work()) for work in (_agent_turn_worker, _operations_worker, _managed_upload_worker)]
        try:
            yield
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    app.router.lifespan_context = lifespan
    uvicorn.run(app, host="127.0.0.1", port=18081, log_level="warning")


def main():
    if len(sys.argv) > 1:
        if sys.argv[1] == "serve":
            serve()
        elif sys.argv[1] == "stop":
            STOP.parent.mkdir(parents=True, exist_ok=True)
            STOP.touch()
        return
    from tests.access_postgresql import isolated_access_database
    from sqlalchemy import text
    from app.services import object_storage_service

    STOP.parent.mkdir(parents=True, exist_ok=True)
    STOP.unlink(missing_ok=True)
    uploaded_objects = []
    with isolated_access_database() as (url, admin):
        environment = dict(
            os.environ, DATABASE_URL=url.render_as_string(hide_password=False),
            PUBLIC_APP_URL="http://127.0.0.1:5175", AUTH_COOKIE_SECURE="false",
            BOOTSTRAP_SUPERADMIN_EMAIL="", WORKFLOW_PAYLOAD_ACTIVE_KEY_ID="stream-qa",
            WORKFLOW_PAYLOAD_ENCRYPTION_KEYS=json.dumps({"stream-qa": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")}),
        )
        with (STOP.parent / "agent-stream-browser.log").open("w", encoding="utf-8") as log:
            child = subprocess.Popen(
                [sys.executable, "-m", "tests.agent_streaming_browser_app", "serve"],
                cwd=ROOT, env=environment, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                print("Isolated streaming browser backend starting on 18081", flush=True)
                while child.poll() is None and not STOP.exists():
                    time.sleep(1)
            finally:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=20)
                STOP.unlink(missing_ok=True)
                with admin.connect() as connection:
                    uploaded_objects = connection.execute(text(
                        "SELECT f.bucket_name, f.object_key FROM bucket_files f "
                        "JOIN data_sources d ON d.id=f.data_source_id WHERE d.tenant_id=:tenant"
                    ), {"tenant": "tenant-stream-qa"}).all()
    for bucket_name, object_key in uploaded_objects:
        assert "/tenants/tenant-stream-qa/" in "/" + object_key
        object_storage_service.delete_all_object_versions(bucket_name, object_key)
    print("Isolated browser database removed", flush=True)


if __name__ == "__main__":
    main()
