"""Create an isolated, browser-ready P2 release-governance fixture.

This fixture deliberately has a narrower scope than ``e2e_p1_fixture.py``:
it creates a complete, inspectable A/B release history rather than attempting
to seed every P1 product surface.  It is safe to use for local browser E2E
work because it only accepts a *new* SQLite file whose name contains ``e2e``.
It never overwrites a database, source database, or bucket file.

Example (PowerShell)::

    $env:DATABASE_URL = 'sqlite:///E:/work/test/backend/data/e2e-p2.sqlite3'
    python backend/tests/e2e_p2_fixture.py

The script prints one ``E2E_P2_FIXTURE=`` JSON payload with IDs, URLs and
synthetic account emails.  Passwords are deliberately never printed.  The
two local-only passwords can be overridden before invoking the script through
``E2E_P2_OWNER_PASSWORD`` and ``E2E_P2_REVIEWER_PASSWORD``.
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote


OWNER_EMAIL = "p2-e2e-owner@example.test"
REVIEWER_EMAIL = "p2-e2e-reviewer@example.test"
DEFAULT_OWNER_PASSWORD = "P2E2e!Owner2026"
DEFAULT_REVIEWER_PASSWORD = "P2E2e!Reviewer2026"
BINDING_KEY = "data_source:p2-orders:sqlite"


def _database_path() -> Path:
    """Return a fresh, explicitly named E2E SQLite target or stop safely."""
    raw_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not raw_url.lower().startswith("sqlite:///") or raw_url.lower().endswith(":memory:"):
        raise SystemExit(
            "P2 E2E fixture requires DATABASE_URL to point to a fresh SQLite file, "
            "for example sqlite:///E:/work/test/backend/data/e2e-p2.sqlite3"
        )
    raw_path = unquote(raw_url[len("sqlite:///") :])
    # Accept SQLAlchemy's POSIX absolute-path spelling while preserving Windows
    # drive paths such as ``sqlite:///E:/work/...``.
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
    """Create the harmless, read-only SQLite target used by the governed SQL Action."""
    if path.exists():
        raise SystemExit(f"Refusing to overwrite existing fixture source database: {path}")
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE p2_orders (
                order_no TEXT PRIMARY KEY,
                amount REAL NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO p2_orders(order_no, amount, status) VALUES (?, ?, ?)",
            [
                ("P2-ORDER-A", 1200.0, "approved"),
                ("P2-ORDER-B", 3400.0, "approved"),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _as_principal(db, user) -> None:
    """Switch the same fixture session between the independent governance actors."""
    db.info["tenant_id"] = user.tenant_id
    db.info["user_id"] = user.id


def _proposal_content(db, scenario, *, label: str) -> dict:
    """Capture the current full definition and make a visible immutable revision."""
    from app.services import release_service

    content = copy.deepcopy(release_service.capture_snapshot_content(db, scenario))
    content["scenario"]["description"] = f"P2 fixture immutable release {label}"
    workflow = content["workflows"][0]
    workflow["name"] = f"P2 发布工作流 {label}"
    workflow["description"] = f"用于验证冻结定义 {label} 的只读订单检查流程。"
    action = content["actions"][0]
    action["description"] = f"P2 受治理 SQL Action，定义版本 {label}。"
    return content


def _create_frozen_evidence(
    db,
    *,
    definition,
    workflow_id: str,
    action_id: str,
    event_id: str,
    actor_id: str,
    release_label: str,
    connector_audit: list[dict],
) -> dict[str, str]:
    """Insert completed, credential-free runtime records for Tasks and Lineage.

    These are deliberately completed fixture records, never queued jobs: opening
    the browser cannot accidentally make the local worker execute a seeded run.
    """
    from app.models import ActionExecutionLog, EventEnvelope, WorkflowRun

    now = datetime.now(timezone.utc)
    execution_key = f"p2-fixture-{definition.environment}-{release_label.lower()}"
    event = EventEnvelope(
        scenario_id=definition.scenario.id,
        event_id=event_id,
        name="P2 发布验证事件",
        payload={"fixture": "p2", "release": release_label},
        source="fixture",
        environment=definition.environment,
        definition_snapshot_id=definition.snapshot_id,
        release_id=definition.release_id,
        definition_hash=definition.definition_hash,
        definition_source=definition.source,
        dedupe_key=f"{execution_key}:event",
        created_at=now,
    )
    run = WorkflowRun(
        scenario_id=definition.scenario.id,
        workflow_id=workflow_id,
        trigger_source="event",
        event_envelope_id=None,
        dedupe_key=None,
        input_params={"fixture": "p2", "release": release_label},
        environment=definition.environment,
        definition_snapshot_id=definition.snapshot_id,
        release_id=definition.release_id,
        definition_hash=definition.definition_hash,
        definition_source=definition.source,
        execution_key=execution_key,
        status="succeeded",
        attempt=1,
        max_attempts=1,
        timeout_seconds=60,
        available_at=now,
        started_at=now,
        completed_at=now,
        result={"fixture": True, "release": release_label, "steps": []},
        created_by_user_id=actor_id,
    )
    db.add_all([event, run])
    db.flush()
    run.event_envelope_id = event.id
    log = ActionExecutionLog(
        scenario_id=definition.scenario.id,
        target_type="action",
        target_id=action_id,
        target_name=f"P2 订单读取 {release_label}",
        input_params={"minimum_amount": 1000, "fixture": True},
        status="success",
        mode="execute",
        idempotency_key=f"{execution_key}:action",
        environment=definition.environment,
        definition_snapshot_id=definition.snapshot_id,
        release_id=definition.release_id,
        definition_hash=definition.definition_hash,
        definition_source=definition.source,
        result={"fixture": True, "row_count": 2, "release": release_label},
        connector_audit=copy.deepcopy(connector_audit),
        duration_ms=1,
        created_at=now,
    )
    db.add(log)
    db.flush()
    run.result = {
        "fixture": True,
        "release": release_label,
        "steps": [{"step": 1, "type": "action", "status": "success", "log_id": log.id}],
    }
    return {"event_id": event.id, "run_id": run.id, "log_id": log.id}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"P2 fixture validation failed: {message}")


def main() -> None:
    target_db = _database_path()
    source_db = target_db.with_name(f"{target_db.stem}-source.sqlite3")
    _seed_source_database(source_db)

    # Imports happen only after the fresh-database guard.  ``app.database``
    # builds its engine at import time and must not observe an accidental URL.
    backend_root = Path(__file__).resolve().parents[1]
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))

    from app.database import SessionLocal, init_db
    from app.models import (
        BusinessScenario,
        DataSource,
        OntologyAction,
        OntologyEntity,
        OntologyEvent,
        OntologyProperty,
        OntologyWorkflow,
        Tenant,
        User,
    )
    from app.services import (
        auth_service,
        connector_service,
        permission_service,
        release_service,
        runtime_definition_service,
    )

    init_db()
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        owner_password = os.environ.get("E2E_P2_OWNER_PASSWORD", DEFAULT_OWNER_PASSWORD)
        reviewer_password = os.environ.get("E2E_P2_REVIEWER_PASSWORD", DEFAULT_REVIEWER_PASSWORD)
        if len(owner_password) < 8 or len(reviewer_password) < 8:
            raise SystemExit("P2 E2E account passwords must each contain at least 8 characters.")

        tenant = Tenant(name="P2 发布治理浏览器联调租户")
        db.add(tenant)
        db.flush()
        owner = User(
            tenant_id=tenant.id,
            email=OWNER_EMAIL,
            display_name="P2 联调所有者",
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
        reviewer = User(
            tenant_id=tenant.id,
            email=REVIEWER_EMAIL,
            display_name="P2 独立评审管理员",
            password_hash=auth_service.hash_password(reviewer_password),
            status="active",
            email_verified_at=now,
        )
        db.add(reviewer)
        db.flush()
        permission_service.assign_member_role(
            db,
            organization,
            user_id=reviewer.id,
            role_key="admin",
        )

        scenario = BusinessScenario(
            tenant_id=tenant.id,
            name="P2 A/B 发布与回滚联调场景",
            description="P2 fixture authoring definition before release A.",
            industry="发布治理",
            status="active",
        )
        db.add(scenario)
        db.flush()
        entity = OntologyEntity(
            scenario_id=scenario.id,
            name="发布订单",
            description="用于验证非开发环境 SQL 绑定的最小实体。",
            icon="box",
            color="#2563eb",
        )
        db.add(entity)
        db.flush()
        db.add_all(
            [
                OntologyProperty(
                    entity_id=entity.id,
                    name="订单号",
                    data_type="string",
                    is_key=True,
                    is_required=True,
                ),
                OntologyProperty(
                    entity_id=entity.id,
                    name="金额",
                    data_type="number",
                    is_required=True,
                ),
            ]
        )

        source = DataSource(
            tenant_id=tenant.id,
            scenario_id=scenario.id,
            name="P2 本地订单 SQLite",
            type="sqlite",
            config={"path": str(source_db)},
            status="ok",
        )
        db.add(source)
        db.flush()
        action = OntologyAction(
            scenario_id=scenario.id,
            entity_id=entity.id,
            name="P2 读取订单",
            description="受治理的只读 SQLite Action。",
            input_schema={
                "type": "object",
                "properties": {"minimum_amount": {"type": "number", "minimum": 0}},
                "required": ["minimum_amount"],
                "additionalProperties": False,
            },
            executor_type="sql",
            executor_config={
                "data_source_binding_key": BINDING_KEY,
                "data_source_binding_ref": {
                    "adapter": "sqlite",
                    "required_capabilities": ["sql_read"],
                },
                "sql": (
                    "SELECT order_no, amount, status FROM p2_orders "
                    "WHERE amount >= {minimum_amount} ORDER BY amount DESC"
                ),
            },
            precondition="仅可读取隔离的 P2 fixture SQLite 数据。",
            postcondition="返回符合阈值的订单，不写入外部系统。",
            requires_confirmation=True,
            idempotency_required=True,
            permission_scope="scenario",
            access_scope="tenant",
        )
        event = OntologyEvent(
            scenario_id=scenario.id,
            name="P2 发布验证事件",
            description="用于展示事件和任务都固定到同一发布定义。",
            payload_schema={
                "type": "object",
                "properties": {"release": {"type": "string"}},
                "required": ["release"],
                "additionalProperties": False,
            },
            trigger_source="P2 fixture",
            enabled=True,
        )
        db.add_all([action, event])
        db.flush()
        workflow = OntologyWorkflow(
            scenario_id=scenario.id,
            name="P2 发布工作流 Draft",
            description="在 release A/B 中分别变为可见的冻结工作流名称。",
            trigger_type="manual",
            trigger_config={"max_attempts": 1, "timeout_seconds": 60},
            steps=[
                {
                    "step": 1,
                    "type": "action",
                    "action_id": action.id,
                    "params": {"minimum_amount": 1000},
                }
            ],
            nodes=[],
            edges=[],
            status="active",
            enabled=True,
            access_scope="tenant",
        )
        db.add(workflow)
        db.flush()

        _as_principal(db, owner)
        for environment in ("dev", "staging", "prod"):
            connector_service.upsert_binding(
                db,
                scenario,
                environment=environment,
                binding_key_value=BINDING_KEY,
                kind="data_source",
                connector_id=source.id,
                reference_label="P2 fixture read-only SQLite orders",
                check=True,
                created_by_user_id=owner.id,
            )
        db.commit()

        # A is independently reviewed, merged and published to every runtime
        # environment.  B is then promoted, after which staging rolls back to
        # A without altering the shared live authoring definition (which stays
        # on B).  This is the core non-dev isolation chain browser tests need.
        branch = release_service.create_branch(
            db,
            scenario.id,
            name="p2-e2e-release",
            description="Seeded A/B release branch for local browser E2E.",
        )
        proposal_a = release_service.create_proposal(
            db,
            branch.id,
            title="P2 release A",
            description="First independently reviewed immutable release.",
            content=_proposal_content(db, scenario, label="A"),
            submit=True,
            expected_base_snapshot_id=branch.head_snapshot_id,
        )
        _as_principal(db, reviewer)
        release_service.create_review(
            db,
            proposal_a.id,
            decision="approve",
            comment="Independent fixture reviewer approved release A.",
        )
        _as_principal(db, owner)
        proposal_a = release_service.merge_proposal(
            db,
            proposal_a.id,
            confirmed=True,
            note="Fixture merge A",
        )
        _require(bool(proposal_a.merged_snapshot_id), "release A must be merged")
        release_a = {
            environment: release_service.publish_snapshot(
                db,
                scenario.id,
                environment=environment,
                confirmed=True,
                branch_id=branch.id,
                notes="P2 fixture release A",
            )
            for environment in ("dev", "staging", "prod")
        }

        proposal_b = release_service.create_proposal(
            db,
            branch.id,
            title="P2 release B",
            description="Second independently reviewed immutable release.",
            content=_proposal_content(db, scenario, label="B"),
            submit=True,
            expected_base_snapshot_id=branch.head_snapshot_id,
        )
        _as_principal(db, reviewer)
        release_service.create_review(
            db,
            proposal_b.id,
            decision="approve",
            comment="Independent fixture reviewer approved release B.",
        )
        _as_principal(db, owner)
        proposal_b = release_service.merge_proposal(
            db,
            proposal_b.id,
            confirmed=True,
            note="Fixture merge B",
        )
        _require(bool(proposal_b.merged_snapshot_id), "release B must be merged")
        release_b = {
            environment: release_service.publish_snapshot(
                db,
                scenario.id,
                environment=environment,
                confirmed=True,
                branch_id=branch.id,
                notes="P2 fixture release B",
            )
            for environment in ("dev", "staging", "prod")
        }
        rollback = release_service.rollback_snapshot(
            db,
            scenario.id,
            target_snapshot_id=str(proposal_a.merged_snapshot_id),
            confirmed=True,
            branch_id=branch.id,
            environment="staging",
            reason="P2 fixture validates non-dev rollback to release A.",
        )

        staging_definition = runtime_definition_service.resolve_active(
            db, scenario, environment="staging"
        )
        prod_definition = runtime_definition_service.resolve_active(
            db, scenario, environment="prod"
        )
        _require(staging_definition.snapshot_id == proposal_a.merged_snapshot_id, "staging must resolve A after rollback")
        _require(prod_definition.snapshot_id == proposal_b.merged_snapshot_id, "prod must resolve B")
        _require(staging_definition.source == prod_definition.source == "release", "non-dev definitions must be frozen")
        _require(release_a["staging"].connector_audit, "A staging release must record connector audit")
        _require(release_b["prod"].connector_audit, "B prod release must record connector audit")

        staging_release = next(
            item
            for item in release_service.list_releases(db, scenario.id, environment="staging")
            if item.id == staging_definition.release_id
        )
        prod_release = next(
            item
            for item in release_service.list_releases(db, scenario.id, environment="prod")
            if item.id == prod_definition.release_id
        )
        evidence_a = _create_frozen_evidence(
            db,
            definition=staging_definition,
            workflow_id=workflow.id,
            action_id=action.id,
            event_id=event.id,
            actor_id=owner.id,
            release_label="A",
            connector_audit=staging_release.connector_audit or [],
        )
        evidence_b = _create_frozen_evidence(
            db,
            definition=prod_definition,
            workflow_id=workflow.id,
            action_id=action.id,
            event_id=event.id,
            actor_id=owner.id,
            release_label="B",
            connector_audit=prod_release.connector_audit or [],
        )
        db.commit()

        # Re-resolve the durable staging evidence as a final, fail-closed check
        # that the row can never silently fall forward to release B.
        from app.models import WorkflowRun

        staging_run = db.get(WorkflowRun, evidence_a["run_id"])
        _require(staging_run is not None, "staging evidence run must exist")
        replay_definition = runtime_definition_service.resolve_for_run(db, staging_run)
        _require(replay_definition.snapshot_id == proposal_a.merged_snapshot_id, "staging evidence run must stay pinned to A")

        payload = {
            "database_url": os.environ["DATABASE_URL"],
            "source_database_path": str(source_db),
            "owner": {"email": OWNER_EMAIL, "user_id": owner.id},
            "reviewer": {"email": REVIEWER_EMAIL, "user_id": reviewer.id, "role": "admin"},
            "tenant_id": tenant.id,
            "scenario_id": scenario.id,
            "branch_id": branch.id,
            "binding_key": BINDING_KEY,
            "snapshots": {"a": proposal_a.merged_snapshot_id, "b": proposal_b.merged_snapshot_id},
            "active_releases": {
                "dev": release_b["dev"].id,
                "staging": staging_definition.release_id,
                "prod": prod_definition.release_id,
            },
            "rollback_id": rollback.id,
            "evidence": {"staging_a": evidence_a, "prod_b": evidence_b},
            "urls": {
                "releases": f"/releases?scenario_id={scenario.id}",
                "tasks": f"/tasks?scenario_id={scenario.id}",
                "lineage": f"/lineage?scenario_id={scenario.id}",
                "connectors": f"/connectors?scenario_id={scenario.id}",
            },
        }
        print("E2E_P2_FIXTURE=" + json.dumps(payload, ensure_ascii=False, sort_keys=True))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
