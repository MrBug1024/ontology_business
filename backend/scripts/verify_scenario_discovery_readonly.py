"""Verify existing plugin authors' adopted scenario context without changing business data."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)


def verify(limit: int = 3) -> dict:
    from fastapi import HTTPException
    from sqlalchemy import event, func, select, text

    from app.database import SessionLocal
    from app.distillation_models import DistillationScenarioState
    from app.distillation_schemas import DistillationDocument
    from app.models import AssistantMessage, AssistantThread, DataSource, OntologyRelease
    from app.scenario_discovery_context_schemas import ScenarioDiscoveryContextOut
    from app.services import distillation_library_service, permission_service, scenario_discovery_context

    samples = []
    skipped = 0
    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        read_only = db.scalar(text("SHOW transaction_read_only")) == "on"
        candidates = db.execute(select(
            AssistantThread.created_by_user_id, AssistantThread.tenant_id, OntologyRelease.scenario_id,
        ).join(AssistantMessage, AssistantMessage.thread_id == AssistantThread.id).join(
            OntologyRelease, OntologyRelease.id ==
            AssistantMessage.proposal["manifest"]["deployment"]["release_id"].as_string(),
        ).outerjoin(DistillationScenarioState,
            (DistillationScenarioState.scenario_id == OntologyRelease.scenario_id)
            & (DistillationScenarioState.tenant_id == OntologyRelease.tenant_id),
        ).where(
            AssistantMessage.proposal["kind"].as_string() == "scenario-plugin-artifact.v1",
            AssistantThread.tenant_id == OntologyRelease.tenant_id,
            AssistantThread.created_by_user_id.is_not(None),
        ).order_by(DistillationScenarioState.revision.desc().nullslast(),
                   AssistantMessage.created_at.desc(), AssistantMessage.id.desc()).limit(40)).all()
        seen = set()
        for user_id, tenant_id, scenario_id in candidates:
            if scenario_id in seen:
                continue
            db.expunge_all()
            db.info.clear()
            db.info.update(user_id=user_id, tenant_id=tenant_id)
            try:
                permission_service.require_principal(db)
            except HTTPException:
                skipped += 1
                continue
            before = db.scalar(select(DistillationScenarioState).where(
                DistillationScenarioState.scenario_id == scenario_id,
                DistillationScenarioState.tenant_id == tenant_id,
            ))
            document = DistillationDocument.model_validate(before.document) if before else DistillationDocument()
            before_revision = before.revision if before else None
            state_count_before = db.scalar(select(func.count()).select_from(DistillationScenarioState).where(
                DistillationScenarioState.scenario_id == scenario_id,
                DistillationScenarioState.tenant_id == tenant_id,
            ))
            # Measure a cold service call, including its fresh authorization reads.
            db.expunge_all()
            db.info.clear()
            db.info.update(user_id=user_id, tenant_id=tenant_id)
            counters = {"queries": 0, "writes": 0}

            def record_query(connection, cursor, statement, parameters, context, executemany):
                counters["queries"] += 1
                token = statement.lstrip().split(None, 1)[0].upper()
                if token in {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "TRUNCATE"}:
                    counters["writes"] += 1

            connection = db.connection()
            event.listen(connection, "before_cursor_execute", record_query)
            started = perf_counter()
            try:
                result = scenario_discovery_context.context_for_scenario(db, scenario_id)
            except HTTPException as error:
                if error.status_code in {403, 404}:
                    skipped += 1
                    continue
                raise
            finally:
                elapsed_ms = round((perf_counter() - started) * 1000, 3)
                event.remove(connection, "before_cursor_execute", record_query)
            checked = ScenarioDiscoveryContextOut.model_validate(result.model_dump())
            assert checked.revision == before_revision
            for field in ("desired_outcome", "non_goals", "success_metric"):
                assert getattr(checked.business, field) == scenario_discovery_context._text(getattr(document, field), 4000)
            source_ids = {item.data_source_id for item in checked.materials.sources}
            allowed_ids = set(db.scalars(select(DataSource.id).where(
                *distillation_library_service.authorized_source_filters(db, scenario_id, include_shared=True),
                DataSource.id.in_(source_ids),
            ))) if source_ids else set()
            available_count = db.scalar(select(func.count()).select_from(DataSource).where(
                *distillation_library_service.authorized_source_filters(db, scenario_id, include_shared=True)))
            assert source_ids == allowed_ids
            assert len(checked.materials.sources) == min(available_count, scenario_discovery_context.MAX_MATERIALS)
            assert checked.materials.has_more == (available_count > scenario_discovery_context.MAX_MATERIALS)
            assert all(item.resource_scope == "modeling" and not item.content_read for item in checked.materials.sources)
            assert not db.new and not db.dirty and not db.deleted and counters["writes"] == 0
            state_count_after = db.scalar(select(func.count()).select_from(DistillationScenarioState).where(
                DistillationScenarioState.scenario_id == scenario_id,
                DistillationScenarioState.tenant_id == tenant_id,
            ))
            assert state_count_before == state_count_after
            prompt = scenario_discovery_context.prompt_context(checked)
            assert len(prompt) <= scenario_discovery_context.MAX_PROMPT_CHARS
            samples.append({
                "sample": len(samples) + 1,
                "principal_verified": True, "scenario_acl_verified": True,
                "dto_verified": True, "adopted_business_fields_match": True,
                "state_present": before is not None, "state_revision": checked.revision,
                "state_count_unchanged": True, "orm_writes": counters["writes"],
                "material_count": len(checked.materials.sources),
                "material_scope_verified": True, "material_count_matches_catalog": True,
                "material_content_read": False,
                "materials_has_more": checked.materials.has_more,
                "handoff_status": checked.handoff.status,
                "can_continue_construction": checked.construction.can_continue,
                "process_node_count": checked.processes.as_is.total_nodes + checked.processes.to_be.total_nodes,
                "historical_case_count": checked.historical_cases.total_count,
                "context_bytes": len(checked.model_dump_json().encode("utf-8")),
                "prompt_characters": len(prompt),
                "cold_query_count": counters["queries"], "elapsed_ms": elapsed_ms,
            })
            seen.add(scenario_id)
            if len(samples) >= limit:
                break
        db.rollback()
    return {"read_only": read_only, "rolled_back": True, "requested_scenarios": limit,
            "verified_scenarios": len(samples), "skipped_inaccessible_candidates": skipped,
            "selection": "existing_reviewed_plugin_catalog_author_and_release_scenario",
            "business_data_changed": False, "samples": samples}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, choices=range(1, 11), default=3)
    arguments = parser.parse_args()
    try:
        result = verify(arguments.limit)
    except Exception as error:
        print(f"Scenario discovery verification failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
    arguments.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
