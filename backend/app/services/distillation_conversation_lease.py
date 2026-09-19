"""PostgreSQL claim, heartbeat and fence for read-only investigation workers."""
from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ..distillation_conversation_models import DistillationConversationTurn as Turn


LEASE_SECONDS = 45
HEARTBEAT_SECONDS = 10
MAX_ATTEMPTS = 3
MAX_MODEL_CALLS = 10


class LeaseLost(RuntimeError):
    pass


def now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Lease:
    turn_id: str
    tenant_id: str
    user_id: str
    token: str
    generation: int


def claim(db: Session) -> Lease | None:
    moment = now()
    for _ in range(10):
        row = db.scalar(select(Turn).where(or_(Turn.status == "queued",
            (Turn.status == "running") & (Turn.lease_expires_at <= moment))).order_by(
            Turn.created_at, Turn.id).with_for_update(skip_locked=True).limit(1))
        if row is None:
            return None
        if any(step.get("tool_name") == "login_business_system" and step.get("status") == "running" for step in row.steps):
            row.status, row.error = "failed", "网页登录在上轮中断，登录结果未知；请核对系统状态后重新发起调查。"
            row.completed_at = row.updated_at = moment
            row.steps = [{**step, "status": "failed", "summary": "登录中断，未自动重试", "completed_at": moment.isoformat()}
                if step.get("status") == "running" else step for step in row.steps]
            row.lease_token = row.lease_expires_at = None
            db.flush()
            continue
        if row.attempt >= MAX_ATTEMPTS or row.model_calls >= MAX_MODEL_CALLS:
            row.status, row.error = "failed", "调查多次中断或达到本轮上限，请缩小问题后重新发送。"
            row.completed_at = row.updated_at = moment
            row.lease_token = row.lease_expires_at = None
            db.flush()
            continue
        row.steps = [{**step, "status": "failed", "summary": "上次只读调查中断，本轮将从已保存记录恢复。",
            "completed_at": moment.isoformat()} if step["status"] == "running" else step for step in row.steps]
        row.status, row.lease_token = "running", uuid.uuid4().hex
        row.lease_generation += 1
        row.attempt += 1
        row.lease_expires_at, row.updated_at = moment + timedelta(seconds=LEASE_SECONDS), moment
        db.flush()
        return Lease(row.id, row.tenant_id, row.created_by, row.lease_token, row.lease_generation)
    return None


def scope(lease: Lease):
    return (Turn.id == lease.turn_id, Turn.tenant_id == lease.tenant_id,
        Turn.status == "running", Turn.lease_token == lease.token,
        Turn.lease_generation == lease.generation, Turn.lease_expires_at > now())


def owned(db: Session, lease: Lease, *, lock: bool = False) -> Turn:
    query = select(Turn).where(*scope(lease)).execution_options(populate_existing=True)
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise LeaseLost("Investigation lease is no longer current")
    db.info.update(tenant_id=lease.tenant_id, user_id=lease.user_id)
    return row


def renew(db: Session, lease: Lease) -> bool:
    result = db.execute(update(Turn).where(*scope(lease)).values(
        lease_expires_at=now() + timedelta(seconds=LEASE_SECONDS)))
    return result.rowcount == 1


@contextmanager
def heartbeat(lease: Lease, session_factory):
    stopped = threading.Event()
    lost = threading.Event()

    def run() -> None:
        while not stopped.wait(HEARTBEAT_SECONDS):
            try:
                with session_factory() as db:
                    live = renew(db, lease)
                    db.commit()
                if not live:
                    lost.set()
                    return
            except Exception:  # A disconnected heartbeat must never prolong ownership locally.
                lost.set()
                return

    thread = threading.Thread(target=run, name="distillation-lease-renewal", daemon=True)
    thread.start()
    try:
        yield lost
    finally:
        stopped.set()
        thread.join(timeout=2)
