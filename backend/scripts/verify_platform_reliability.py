"""Exercise write races only in the disposable PostgreSQL verification database."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BucketFile, DataSource, DocumentIndexJob, OntologyEntity, OntologyProperty, User
from app.services import ontology_service, rag_service
from scripts.verify_distillation_publication import main


def verify_pending_names(engine):
    with Session(engine, autoflush=False) as db:
        entity = OntologyEntity(id="pending_entity", scenario_id="scene_a", name="Subject", api_name="subject")
        db.add(entity)
        db.flush()
        for index, name in enumerate(("字段一", "字段二", "字段三")):
            api_name = ontology_service.allocate_resource_api_name(db, OntologyProperty,
                scope_field="entity_id", scope_id=entity.id, display_name=name,
                prefix="property", stable_key=f"subject.{name}")
            db.add(OntologyProperty(entity_id=entity.id, name=name, api_name=api_name, data_type="string"))
        db.flush()
        names = db.scalars(select(OntologyProperty.api_name).where(OntologyProperty.entity_id == entity.id)).all()
        assert len(names) == len(set(names)) == 3
        db.rollback()


def verify_document_job_fencing(engine):
    started = datetime.now(timezone.utc)
    with Session(engine) as db:
        db.info.update(tenant_id="tenant_a", user_id="user_a")
        source = DataSource(id="source_job", tenant_id="tenant_a", scenario_id="scene_a",
                            type="file_bucket", name="Documents", resource_scope="modeling")
        db.add(source); db.flush()
        file = BucketFile(id="file_job", data_source_id=source.id, filename="synthetic.txt",
                          stored_path="minio://synthetic/doc.txt",
                          status="parsed", parsed_text="Synthetic evidence", size=18)
        db.add(file); db.flush()
        job, created = rag_service.enqueue_document_index(db, file, parse_document=False)
        assert created and job.requested_by_user_id == "user_a"
        job.status = "running"; job.attempt = 1; job.started_at = started
        db.commit()
        job_id = job.id

    with Session(engine) as old, Session(engine) as replacement:
        old_job = old.get(DocumentIndexJob, job_id)
        old_file = old.get(BucketFile, "file_job")
        new_job = replacement.get(DocumentIndexJob, job_id)
        new_job.attempt = 2; new_job.started_at = started + timedelta(seconds=1)
        replacement.commit()
        old_file.parsed_text = "Stale worker text"
        rag_service._retry_document_job(old, old_job, old_file, status="failed",
            error="Stale failure", now=started, expected_started_at=started)
        replacement.expire_all()
        assert replacement.get(DocumentIndexJob, job_id).status == "running"
        assert replacement.get(BucketFile, "file_job").parsed_text == "Synthetic evidence"

    with Session(engine) as db:
        job = db.get(DocumentIndexJob, job_id)
        job.status = "queued"; job.available_at = started
        user = db.get(User, "user_a"); user.status = "disabled"
        db.commit()
        with patch.object(rag_service, "prepare_file_index", side_effect=AssertionError("Unauthorized I/O")):
            rag_service.process_document_index_jobs(db, now=started + timedelta(seconds=2))
        db.expire_all()
        assert db.get(DocumentIndexJob, job_id).status == "failed"
        assert db.get(DocumentIndexJob, job_id).active_key is None
        assert db.info.get("user_id") is None


def verify(engine):
    verify_pending_names(engine)
    verify_document_job_fencing(engine)
    print("PASS: pending property names, durable task actor, stale worker fencing, disabled actor denial")


if __name__ == "__main__":
    try:
        main(additional_checks=verify)
    except Exception as error:
        print("FAILED:", type(error).__name__)
        raise SystemExit(1)
