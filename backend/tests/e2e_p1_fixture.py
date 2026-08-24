"""Create an isolated, browser-ready P1 fixture database.

This is intentionally a small executable fixture rather than a development
seed.  It refuses a non-fresh database and only accepts an SQLite URL whose
file name contains ``e2e`` so a local command cannot accidentally populate a
normal development database.

Example (PowerShell)::

    $env:DATABASE_URL = 'sqlite:///E:/work/test/backend/data/e2e-p1.sqlite3'
    python backend/tests/e2e_p1_fixture.py

On success the script prints exactly one JSON document prefixed with
``E2E_P1_FIXTURE=``.  The generated source SQLite database and a small bucket
file are deliberately retained as part of the disposable fixture; the script
never deletes or overwrites an existing file.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote


def _database_path() -> Path:
    """Return a safe file path for the explicitly requested fixture database."""
    raw_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not raw_url.lower().startswith("sqlite:///") or raw_url.lower().endswith(":memory:"):
        raise SystemExit(
            "E2E fixture requires DATABASE_URL to point to a fresh SQLite file, "
            "for example sqlite:///E:/work/test/backend/data/e2e-p1.sqlite3"
        )
    raw_path = unquote(raw_url[len("sqlite:///") :])
    # SQLAlchemy represents POSIX absolute paths as sqlite:////tmp/x.  Preserve
    # the leading slash while also accepting a normal Windows drive path.
    if raw_path.startswith("/") and len(raw_path) > 3 and raw_path[2] == ":":
        raw_path = raw_path[1:]
    path = Path(raw_path).expanduser().resolve()
    if "e2e" not in path.name.lower():
        raise SystemExit("Refusing to seed a database whose file name does not contain 'e2e'.")
    if path.exists():
        raise SystemExit(f"Refusing to overwrite existing fixture database: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _seed_source_database(path: Path) -> None:
    """Create a harmless external SQLite source used by the read-only operation."""
    if path.exists():
        raise SystemExit(f"Refusing to overwrite existing fixture source database: {path}")
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE purchase_records (
                code TEXT PRIMARY KEY,
                supplier TEXT NOT NULL,
                amount REAL NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO purchase_records(code, supplier, amount, status) VALUES (?, ?, ?, ?)",
            [
                ("P1-001", "星河供应商", 12800.0, "待审批"),
                ("P1-002", "远航供应商", 8600.0, "已提交"),
                ("P1-003", "云帆供应商", 3200.0, "草稿"),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _add_initial_pending_run(
    db,
    *,
    scenario_id: str,
    workflow_id: str,
    owner_id: str,
):
    """Provide a stable cancellation path without racing the live worker."""
    from app.models import WorkflowApprovalRequest, WorkflowRun

    now = datetime.now(timezone.utc)
    run = WorkflowRun(
        scenario_id=scenario_id,
        workflow_id=workflow_id,
        trigger_source="manual",
        input_params={"min_amount": 1000},
        environment="dev",
        status="awaiting_approval",
        attempt=1,
        max_attempts=3,
        timeout_seconds=3600,
        available_at=now,
        started_at=now,
        result={
            "steps": [
                {
                    "step": 1,
                    "type": "approval",
                    "status": "awaiting_approval",
                    "result": {"node_id": "fixture-cancel-approval"},
                }
            ]
        },
        created_by_user_id=owner_id,
    )
    db.add(run)
    db.flush()
    run.execution_key = run.id
    approval = WorkflowApprovalRequest(
        workflow_run_id=run.id,
        scenario_id=scenario_id,
        node_id="fixture-cancel-approval",
        node_name="取消路径预置审批",
        instructions="这是一条为浏览器取消路径准备的测试审批任务。",
        status="pending",
        requested_at=now,
        expires_at=now + timedelta(hours=2),
    )
    db.add(approval)
    db.flush()
    return run, approval


def main() -> None:
    target_db = _database_path()
    source_db = target_db.with_name(f"{target_db.stem}-source.sqlite3")
    _seed_source_database(source_db)

    # Keep imports below the URL guard: app.database constructs its engine at
    # import time and must only ever see the explicitly supplied test URL.
    backend_root = Path(__file__).resolve().parents[1]
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))

    from app.database import SessionLocal, init_db
    from app.models import (
        Agent,
        AssistantMessage,
        AssistantThread,
        BusinessScenario,
        DataMapping,
        DataSource,
        DocumentChunk,
        LLMConfig,
        LLMEvaluationRecord,
        Message,
        OntologyAction,
        OntologyEntity,
        OntologyEvent,
        OntologyInstance,
        OntologyProperty,
        OntologyRelation,
        OntologyWorkflow,
        RelationInstance,
        Tenant,
        User,
        Conversation,
    )
    from app.services import (
        auth_service,
        datasource_service,
        permission_service,
        rag_service,
        workflow_service,
    )
    from sqlalchemy import select

    init_db()
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        owner_email = "p1-e2e-owner@example.test"
        viewer_email = "p1-e2e-viewer@example.test"
        owner_password = "P1E2e!Owner2026"
        viewer_password = "P1E2e!Viewer2026"

        tenant = Tenant(name="P1 浏览器联调租户")
        db.add(tenant)
        db.flush()
        owner = User(
            tenant_id=tenant.id,
            email=owner_email,
            display_name="P1 联调所有者",
            password_hash=auth_service.hash_password(owner_password),
            status="active",
            email_verified_at=now,
        )
        db.add(owner)
        db.flush()
        organization = permission_service.ensure_organization(
            db,
            tenant.id,
            owner_user_id=owner.id,
        )
        viewer = User(
            tenant_id=tenant.id,
            email=viewer_email,
            display_name="P1 联调查看者",
            password_hash=auth_service.hash_password(viewer_password),
            status="active",
            email_verified_at=now,
        )
        db.add(viewer)
        db.flush()
        permission_service.assign_member_role(
            db,
            organization,
            user_id=viewer.id,
            role_key="viewer",
        )

        # Every service called below is intentionally executed as the fixture
        # owner, exercising the same tenant/permission boundary as an API call.
        db.info["tenant_id"] = tenant.id
        db.info["user_id"] = owner.id

        scenario = BusinessScenario(
            tenant_id=tenant.id,
            name="P1 浏览器联调场景",
            description="覆盖对象、资料检索、操作、规则、事件、工作流与 Agent 的隔离联调数据。",
            industry="采购运营",
            status="active",
        )
        db.add(scenario)
        db.flush()

        entity = OntologyEntity(
            scenario_id=scenario.id,
            name="采购申请",
            description="需要审核的采购请求。",
            icon="document",
            color="#2563eb",
        )
        db.add(entity)
        db.flush()
        properties = [
            OntologyProperty(entity_id=entity.id, name="申请编号", data_type="string", is_key=True, is_required=True),
            OntologyProperty(entity_id=entity.id, name="供应商", data_type="string", is_required=True),
            OntologyProperty(entity_id=entity.id, name="金额", data_type="number", is_required=True),
            OntologyProperty(
                entity_id=entity.id,
                name="状态",
                data_type="string",
                is_enum=True,
                enum_values=["草稿", "已提交", "待审批", "已完成"],
            ),
            OntologyProperty(
                entity_id=entity.id,
                name="内部备注",
                data_type="string",
                description="仅所有者/管理员可见，用于验证属性级脱敏。",
                is_sensitive=True,
            ),
        ]
        db.add_all(properties)

        sql_source = DataSource(
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            name="P1 采购 SQLite",
            type="sqlite",
            config={"path": str(source_db)},
            status="ok",
        )
        bucket_source = DataSource(
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            name="P1 采购资料库",
            type="file_bucket",
            config={},
            status="ok",
        )
        db.add_all([sql_source, bucket_source])
        db.flush()

        mapping = DataMapping(
            scenario_id=scenario.id,
            entity_id=entity.id,
            data_source_id=sql_source.id,
            table_name="purchase_records",
            column_map={"申请编号": "code", "供应商": "supplier", "金额": "amount", "状态": "status"},
            status="ok",
            last_row_count=3,
            last_imported_count=3,
            last_checked_at=now,
            last_refreshed_at=now,
        )
        db.add(mapping)
        db.flush()

        instances: list[OntologyInstance] = []
        for index in range(1, 57):
            code = f"P1-{index:03d}"
            instance = OntologyInstance(
                scenario_id=scenario.id,
                entity_id=entity.id,
                name=f"采购申请 {code}",
                attributes={
                    "申请编号": code,
                    "供应商": "星河供应商" if index % 2 else "远航供应商",
                    "金额": 900 + index * 250,
                    "状态": "待审批" if index % 3 else "已提交",
                    "内部备注": f"仅限管理人员：第 {index} 条内部核验记录",
                },
                source="imported" if index <= 3 else "manual",
                source_ref=f"purchase_records.{code}" if index <= 3 else "fixture",
                source_metadata={
                    "mapping_id": mapping.id,
                    "data_source_id": sql_source.id,
                    "table_name": "purchase_records",
                    "row_key": code,
                },
                # The last row intentionally needs an explicit grant so the
                # viewer exercises object-level filtering in the UI/API.
                access_scope="restricted" if index == 56 else "tenant",
            )
            instances.append(instance)
        db.add_all(instances)
        db.flush()

        relation = OntologyRelation(
            scenario_id=scenario.id,
            name="关联复核",
            source_entity_id=entity.id,
            target_entity_id=entity.id,
            relation_type="1:N",
            description="采购申请间的复核依赖。",
        )
        db.add(relation)
        db.flush()
        db.add_all(
            [
                RelationInstance(
                    scenario_id=scenario.id,
                    relation_id=relation.id,
                    source_instance_id=instances[0].id,
                    target_instance_id=instances[1].id,
                    attributes={"原因": "同一供应商复核"},
                ),
                RelationInstance(
                    scenario_id=scenario.id,
                    relation_id=relation.id,
                    source_instance_id=instances[1].id,
                    target_instance_id=instances[2].id,
                    attributes={"原因": "额度联动"},
                ),
            ]
        )

        document_text = (
            "# P1 采购运营手册\n\n"
            "金额超过 10000 元的采购申请必须经过人工审批，审批人需核对供应商、预算和证据资料。\n\n"
            "采购申请提交后会发布“采购申请已提交”事件；事件订阅工作流仅可在有明确执行主体时运行。\n\n"
            "所有操作必须预演、确认并防止重复提交，Agent 只生成预演，不直接执行外部副作用。"
        )
        bucket_file = datasource_service.save_bucket_file(
            bucket_source,
            "P1-采购运营手册.md",
            document_text.encode("utf-8"),
        )
        bucket_file.status = "parsed"
        bucket_file.mime = "text/markdown"
        bucket_file.parsed_text = document_text
        db.add(bucket_file)
        db.flush()
        index_result = rag_service.index_file(db, bucket_file, force=True)
        if index_result.get("status") not in {"indexed", "partial"}:
            raise RuntimeError(f"Could not build fixture RAG index: {index_result}")
        document_chunk = db.execute(
            select(DocumentChunk)
            .where(DocumentChunk.bucket_file_id == bucket_file.id)
            .order_by(DocumentChunk.ordinal)
            .limit(1)
        ).scalars().one()

        action = OntologyAction(
            scenario_id=scenario.id,
            entity_id=entity.id,
            name="查询高金额采购",
            description="在隔离的演示 SQLite 数据源中查询高金额采购，供预演和审批工作流使用。",
            input_schema={
                "type": "object",
                "properties": {"min_amount": {"type": "number", "minimum": 0}},
                "required": ["min_amount"],
                "additionalProperties": False,
            },
            executor_type="sql",
            executor_config={
                "data_source_id": sql_source.id,
                "sql": "SELECT code, supplier, amount, status FROM purchase_records WHERE amount >= {min_amount} ORDER BY amount DESC",
            },
            precondition="仅查询已连接的采购演示数据源。",
            postcondition="返回符合阈值的采购申请，不写入外部数据。",
            requires_confirmation=True,
            idempotency_required=True,
            permission_scope="scenario",
            access_scope="tenant",
        )
        db.add(action)
        db.flush()

        event = OntologyEvent(
            scenario_id=scenario.id,
            name="采购申请已提交",
            description="采购申请提交后发布，供运营处理流程订阅。",
            payload_schema={
                "type": "object",
                "properties": {"reference": {"type": "string", "minLength": 1}},
                "required": ["reference"],
                "additionalProperties": False,
            },
            trigger_source="采购申请提交",
            enabled=True,
        )
        db.add(event)
        db.flush()

        approval_workflow = OntologyWorkflow(
            scenario_id=scenario.id,
            name="采购审批流",
            description="查询高金额采购后暂停，等待人工审批再完成。",
            trigger_type="manual",
            trigger_config={"max_attempts": 3, "timeout_seconds": 3600, "retry_backoff_seconds": 5},
            steps=[
                {"step": 1, "type": "action", "action_id": action.id, "params": {"min_amount": 1000}},
                {
                    "step": 2,
                    "id": "manager-approval",
                    "type": "approval",
                    "instructions": "请确认采购清单与预算证据后批准。",
                    "timeout_seconds": 3600,
                    "on_timeout": "reject",
                },
            ],
            nodes=[],
            edges=[],
            status="active",
            enabled=True,
            access_scope="tenant",
        )
        event_workflow = OntologyWorkflow(
            scenario_id=scenario.id,
            name="采购事件处理",
            description="订阅采购提交事件并在任务中心展示可处理的运营任务。",
            trigger_type="event",
            trigger_config={
                "event_id": event.id,
                "max_attempts": 3,
                "timeout_seconds": 3600,
                "retry_backoff_seconds": 5,
            },
            steps=[
                {
                    "step": 1,
                    "id": "event-review",
                    "type": "approval",
                    "instructions": "请核对事件载荷并确认是否进入后续处理。",
                    "timeout_seconds": 3600,
                    "on_timeout": "reject",
                }
            ],
            nodes=[],
            edges=[],
            status="active",
            enabled=True,
            access_scope="tenant",
        )
        db.add_all([approval_workflow, event_workflow])
        db.flush()

        llm = LLMConfig(
            tenant_id=tenant.id,
            name="P1 本地 Mock LLM",
            provider="openai",
            base_url="http://127.0.0.1:8033/v1",
            api_key="e2e-key",
            model="e2e-model",
            temperature=0.1,
            max_tokens=1024,
            is_default=True,
            capabilities=["chat", "tool"],
            enabled=True,
            routing_priority=1,
        )
        db.add(llm)
        db.flush()
        db.add(
            LLMEvaluationRecord(
                tenant_id=tenant.id,
                llm_config_id=llm.id,
                name="P1 fixture smoke evaluation",
                capability="chat",
                passed=True,
                score=1.0,
                latency_ms=1,
                notes="Fixture-only record; the browser test invokes the local mock service for live calls.",
                metrics={"fixture": True},
            )
        )
        agent = Agent(
            tenant_id=tenant.id,
            name="P1 采购助手",
            description="绑定采购场景、资料库和本地 Mock LLM 的只读/预演 Agent。",
            scenario_id=scenario.id,
            llm_config_id=llm.id,
            system_prompt="优先引用资料库；只预演操作，绝不直接执行外部副作用。",
            data_source_ids=[bucket_source.id, sql_source.id],
            capability_scope={
                "functions": {"mode": "explicit", "selected_ids": []},
                "actions": {"mode": "explicit", "selected_ids": [action.id]},
                "rules": {"mode": "explicit", "selected_ids": []},
                "events": {"mode": "explicit", "selected_ids": [event.id]},
                "workflows": {
                    "mode": "explicit",
                    "selected_ids": [approval_workflow.id, event_workflow.id],
                },
            },
            temperature=0.1,
            max_tokens=1024,
        )
        db.add(agent)
        db.flush()

        citation = {
            "citation_id": "C1",
            "chunk_id": document_chunk.id,
            "file_id": bucket_file.id,
            "filename": bucket_file.filename,
            "data_source_id": bucket_source.id,
            "data_source_name": bucket_source.name,
            "char_start": document_chunk.char_start,
            "char_end": document_chunk.char_end,
            "content_hash": document_chunk.content_hash,
            "file_content_hash": bucket_file.indexed_content_hash,
            "excerpt": document_chunk.text[:220],
        }
        conversation = Conversation(
            agent_id=agent.id,
            created_by_user_id=owner.id,
            title="P1 血缘示例对话",
        )
        db.add(conversation)
        db.flush()
        db.add_all(
            [
                Message(conversation_id=conversation.id, role="user", content="哪些采购需要人工审批？"),
                Message(
                    conversation_id=conversation.id,
                    role="assistant",
                    content="金额超过 10000 元的采购申请需要人工审批【C1】。",
                    citations=[citation],
                    tool_results=[
                        {
                            "name": "search_ontology",
                            "result": {"items": [{"id": instances[0].id, "name": instances[0].name}]},
                        }
                    ],
                ),
            ]
        )

        assistant_thread = AssistantThread(
            tenant_id=tenant.id,
            created_by_user_id=owner.id,
            scenario_id=scenario.id,
            scope_key=f"scenario:{scenario.id}|path:/scenarios/{scenario.id}",
            title="P1 场景资料问答",
        )
        db.add(assistant_thread)
        db.flush()
        db.add_all(
            [
                AssistantMessage(
                    thread_id=assistant_thread.id,
                    role="user",
                    content="采购审批规则是什么？",
                    context={"path": f"/scenarios/{scenario.id}"},
                ),
                AssistantMessage(
                    thread_id=assistant_thread.id,
                    role="assistant",
                    content="金额超过 10000 元的采购申请必须经过人工审批。",
                    attachments=[
                        {
                            "id": f"rag:C1:{document_chunk.id}",
                            "kind": "rag",
                            "citation_id": "C1",
                            "filename": f"C1 · {bucket_file.filename}",
                            "status": "cited",
                            "data_source_id": bucket_source.id,
                            "file_id": bucket_file.id,
                            "chunk_id": document_chunk.id,
                            "content_hash": document_chunk.content_hash,
                            "file_content_hash": bucket_file.indexed_content_hash,
                            "char_start": document_chunk.char_start,
                            "char_end": document_chunk.char_end,
                        }
                    ],
                ),
            ]
        )

        # A real, side-effect-free read-only operation lets the execution
        # page demonstrate operation -> external result without requiring the
        # browser run to manufacture its own prior history.
        db.commit()
        action_result = workflow_service.execute_action(
            db,
            action,
            {"min_amount": 1000},
            confirm=True,
            idempotency_key="e2e-fixture-readonly-action",
        )
        if action_result.get("status") != "success":
            raise RuntimeError(f"Could not execute fixture read-only operation: {action_result}")

        pending_run, pending_approval = _add_initial_pending_run(
            db,
            scenario_id=scenario.id,
            workflow_id=approval_workflow.id,
            owner_id=owner.id,
        )
        db.commit()

        payload = {
            "database_url": os.environ["DATABASE_URL"],
            "source_database_path": str(source_db),
            "owner": {"email": owner_email, "password": owner_password, "user_id": owner.id},
            "viewer": {"email": viewer_email, "password": viewer_password, "user_id": viewer.id},
            "tenant_id": tenant.id,
            "scenario_id": scenario.id,
            "entity_id": entity.id,
            "object_count": len(instances),
            "restricted_object_id": instances[-1].id,
            "sql_data_source_id": sql_source.id,
            "bucket_data_source_id": bucket_source.id,
            "bucket_file_id": bucket_file.id,
            "document_chunk_id": document_chunk.id,
            "action_id": action.id,
            "event_id": event.id,
            "approval_workflow_id": approval_workflow.id,
            "event_workflow_id": event_workflow.id,
            "initial_pending_run_id": pending_run.id,
            "initial_pending_approval_id": pending_approval.id,
            "agent_id": agent.id,
            "assistant_thread_id": assistant_thread.id,
            "llm_config_id": llm.id,
            "urls": {
                "scenario": f"/scenarios/{scenario.id}",
                "agent": f"/agents/{agent.id}/chat",
                "tasks": f"/tasks?scenario_id={scenario.id}",
            },
        }
        print("E2E_P1_FIXTURE=" + json.dumps(payload, ensure_ascii=False, sort_keys=True))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
