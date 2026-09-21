"""Scenario-read collaborators can inspect modeling material without write access."""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import (
    AuthorizationGrant,
    BucketFile,
    DataSource,
    OrganizationMember,
    OrganizationRole,
    User,
)
from app.routers import data_sources
from isolated_postgresql import isolated_postgresql, seed_workspace, tenant_session


def test_scenario_reader_can_read_material_files_and_cannot_mutate(
    isolated_postgresql,
    monkeypatch,
):
    """File APIs inherit the scenario ACL, including text and download paths."""

    isolated = isolated_postgresql
    workspace = seed_workspace(isolated.admin_engine)
    viewer_id, role_id = uuid4().hex, uuid4().hex
    with Session(isolated.admin_engine) as db:
        source = DataSource(
            tenant_id=workspace["tenant_id"],
            scenario_id=workspace["scenario_id"],
            name="Collaborative material bucket",
            type="file_bucket",
            resource_scope="modeling",
            config={},
            status="ok",
        )
        db.add(source)
        db.flush()
        bucket_file = BucketFile(
            data_source_id=source.id,
            filename="process-notes.md",
            stored_path="minio://acceptance/collaboration/process-notes.md",
            size=21,
            mime="text/markdown",
            content_sha256="a" * 64,
            parsed_text="Observed shared process",
            status="parsed",
        )
        db.add(bucket_file)
        db.add(User(
            id=viewer_id,
            tenant_id=workspace["tenant_id"],
            email=f"{viewer_id}@acceptance.invalid",
            password_hash="unusable",
            status="active",
        ))
        db.add(OrganizationRole(
            id=role_id,
            organization_id=workspace["organization_id"],
            key="scenario_material_reader",
            name="Scenario material reader",
            is_system=False,
        ))
        db.flush()
        db.add(OrganizationMember(
            organization_id=workspace["organization_id"],
            user_id=viewer_id,
            role_id=role_id,
            status="active",
        ))
        db.add(AuthorizationGrant(
            organization_id=workspace["organization_id"],
            role_id=role_id,
            resource_type="scenario",
            resource_id=workspace["scenario_id"],
            verb="read",
            effect="allow",
        ))
        db.commit()
        source_id, file_id = source.id, bucket_file.id

    viewer = {**workspace, "user_id": viewer_id}

    def fake_read_bucket_file(_file, _source):
        return b"Observed shared process", 23, "text/markdown"

    monkeypatch.setattr(data_sources.datasource_service, "read_bucket_file", fake_read_bucket_file)
    with tenant_session(isolated.runtime_engine, viewer) as db:
        visible_sources = data_sources.list_data_sources(
            scenario_id=workspace["scenario_id"],
            db=db,
        )
        assert [item.id for item in visible_sources] == [source_id]
        assert visible_sources[0].can_write is False
        assert visible_sources[0].can_delete is False

        files = data_sources.list_files(source_id, agent_id=None, db=db)
        assert [item.id for item in files] == [file_id]
        assert files[0].filename == "process-notes.md"

        text_result = data_sources.file_text(file_id, db=db)
        assert text_result == {
            "filename": "process-notes.md",
            "text": "Observed shared process",
        }

        download = data_sources.file_download(file_id, db=db)
        assert download.body == b"Observed shared process"
        assert download.media_type == "text/markdown"

        for mutation in (
            lambda: data_sources.reparse_file(file_id, agent_id=None, db=db),
            lambda: data_sources.delete_file(file_id, agent_id=None, db=db),
        ):
            with pytest.raises(HTTPException) as denied:
                mutation()
            assert denied.value.status_code == 403
            db.rollback()
