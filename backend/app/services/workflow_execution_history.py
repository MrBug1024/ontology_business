"""Preserve a terminal execution before a human starts another attempt."""
from __future__ import annotations

from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CapabilityInvocation, WorkflowRun


STORAGE_KEY = "workflow_execution_snapshot"


def freeze_before_retry(db: Session, run: WorkflowRun) -> None:
    invocations = db.scalars(select(CapabilityInvocation).where(
        CapabilityInvocation.scenario_id == run.scenario_id,
        CapabilityInvocation.capability_kind == "workflow",
        CapabilityInvocation.capability_key == run.workflow_id,
        CapabilityInvocation.result_document["output"]["workflow_run_id"].as_string() == run.id,
    ).with_for_update()).all()
    for invocation in invocations:
        document = deepcopy(invocation.result_document or {})
        output = document.get("output") or {}
        if STORAGE_KEY in document or output.get("execution_key") != run.execution_key:
            continue
        document[STORAGE_KEY] = {
            "execution_key": run.execution_key, "status": run.status,
            "result": deepcopy(run.result or {}), "error": run.error or "",
        }
        invocation.result_document = document


def stored_execution(db: Session, invocation_id: str, execution_key: str) -> dict | None:
    invocation = db.get(CapabilityInvocation, invocation_id)
    snapshot = (invocation.result_document or {}).get(STORAGE_KEY) if invocation else None
    return snapshot if isinstance(snapshot, dict) and snapshot.get("execution_key") == execution_key else None
