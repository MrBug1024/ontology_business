"""Template catalog visibility follows scenario and backing-source scope."""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models import (
    ArtifactTemplate,
    ArtifactTemplateVersion,
    AuthorizationGrant,
    BucketFile,
    BusinessScenario,
    DataSource,
    OrganizationMember,
    OrganizationRole,
    User,
)
from app.routers import templates
from app.services import permission_service, tenant_service
from isolated_postgresql import isolated_postgresql, seed_workspace, tenant_session


def _compiled(statement) -> str:
    return str(statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    ))


def _allow(monkeypatch, *, scenario_tenant: str) -> None:
    monkeypatch.setattr(tenant_service, "current_tenant_id", lambda _db: "tenant-a")
    monkeypatch.setattr(
        tenant_service,
        "require_scenario",
        lambda _db, scenario_id: SimpleNamespace(
            id=scenario_id,
            tenant_id=scenario_tenant,
        ),
    )
    monkeypatch.setattr(permission_service, "require_scenario_permission", lambda *_args: None)


def test_same_tenant_scenario_keeps_scenario_and_workspace_templates(monkeypatch):
    _allow(monkeypatch, scenario_tenant="tenant-a")
    monkeypatch.setattr(permission_service, "require_tenant_permission", lambda *_args: None)
    sql = _compiled(templates._visible_template_catalog_statement(
        SimpleNamespace(info={}),
        "scenario-a",
    ))
    assert "artifact_templates.tenant_id = 'tenant-a'" in sql
    assert "artifact_templates.scenario_id IS NULL" in sql
    assert "artifact_templates.scenario_id = 'scenario-a'" in sql
    assert "JOIN data_sources" not in sql


def test_scenario_grant_does_not_expand_into_workspace_templates_when_workspace_read_is_denied(monkeypatch):
    _allow(monkeypatch, scenario_tenant="tenant-a")

    def deny_workspace_read(_db, verb):
        assert verb == "read"
        raise HTTPException(403, "workspace read denied")

    monkeypatch.setattr(permission_service, "require_tenant_permission", deny_workspace_read)
    sql = _compiled(templates._visible_template_catalog_statement(
        SimpleNamespace(info={}),
        "scenario-a",
    ))
    assert "artifact_templates.scenario_id = 'scenario-a'" in sql
    assert "artifact_templates.scenario_id IS NULL" not in sql


def test_workspace_catalog_without_scenario_keeps_existing_tenant_scope(monkeypatch):
    checks: list[str] = []
    monkeypatch.setattr(tenant_service, "current_tenant_id", lambda _db: "tenant-a")
    monkeypatch.setattr(
        permission_service,
        "require_tenant_permission",
        lambda _db, verb: checks.append(verb),
    )
    sql = _compiled(templates._visible_template_catalog_statement(
        SimpleNamespace(info={}),
        None,
    ))
    assert checks == ["read"]
    assert "artifact_templates.tenant_id = 'tenant-a'" in sql
    assert "artifact_templates.scenario_id" not in sql.split("WHERE", 1)[1]


def test_public_foreign_scenario_does_not_list_unopenable_templates(monkeypatch):
    _allow(monkeypatch, scenario_tenant="tenant-b")
    sql = _compiled(templates._visible_template_catalog_statement(
        SimpleNamespace(info={}),
        "public-scenario",
    ))
    assert "WHERE false" in sql
    assert "JOIN artifact_template_versions" not in sql
    assert "JOIN data_sources" not in sql
    assert "artifact_templates.tenant_id = 'tenant-a'" not in sql


def test_external_scenario_context_does_not_include_workspace_templates(monkeypatch):
    _allow(monkeypatch, scenario_tenant="tenant-a")
    sql = _compiled(templates._visible_template_catalog_statement(
        SimpleNamespace(info={"external_scenario_id": "scenario-a"}),
        "scenario-a",
    ))
    assert "artifact_templates.scenario_id = 'scenario-a'" in sql
    assert "artifact_templates.scenario_id IS NULL" not in sql


def _template(
    db: Session,
    *,
    tenant_id: str,
    scenario_id: str | None,
    key: str,
    public_source: bool,
) -> ArtifactTemplate:
    source = DataSource(
        tenant_id=tenant_id,
        scenario_id=scenario_id,
        name=f"{key} source",
        type="file_bucket",
        config={},
        is_public=public_source,
    )
    db.add(source)
    db.flush()
    digest = uuid4().hex + uuid4().hex
    bucket_file = BucketFile(
        data_source_id=source.id,
        filename=f"{key}.md",
        stored_path=f"minio://synthetic/{key}.md",
        size=8,
        mime="text/markdown",
        content_sha256=digest,
        status="parsed",
    )
    db.add(bucket_file)
    db.flush()
    template = ArtifactTemplate(
        tenant_id=tenant_id,
        scenario_id=scenario_id,
        key=key,
        name=key,
        status="active",
    )
    db.add(template)
    db.flush()
    version = ArtifactTemplateVersion(
        template_id=template.id,
        version=1,
        bucket_file_id=bucket_file.id,
        filename=bucket_file.filename,
        artifact_format="markdown",
        mime=bucket_file.mime,
        size=bucket_file.size,
        content_sha256=digest,
        placeholder_paths=[],
        template_metadata={},
    )
    db.add(version)
    db.flush()
    template.current_version_id = version.id
    return template


def test_public_postgresql_catalog_excludes_all_unopenable_foreign_templates(
    isolated_postgresql,
):
    caller = seed_workspace(isolated_postgresql.admin_engine)
    owner = seed_workspace(isolated_postgresql.admin_engine)
    with Session(isolated_postgresql.admin_engine) as db:
        public_scenario = db.get(BusinessScenario, owner["scenario_id"])
        public_scenario.is_public = True
        _template(
            db,
            tenant_id=owner["tenant_id"],
            scenario_id=public_scenario.id,
            key="visible_public_template",
            public_source=True,
        )
        _template(
            db,
            tenant_id=owner["tenant_id"],
            scenario_id=public_scenario.id,
            key="private_foreign_template",
            public_source=False,
        )
        _template(
            db,
            tenant_id=caller["tenant_id"],
            scenario_id=None,
            key="caller_shared_template",
            public_source=False,
        )
        db.commit()

    with tenant_session(isolated_postgresql.runtime_engine, caller) as db:
        result = templates.list_templates(
            scenario_id=owner["scenario_id"],
            db=db,
        )
        assert result == []


def test_scenario_read_grant_reads_scenario_template_detail_without_workspace_read(
    isolated_postgresql,
):
    workspace = seed_workspace(isolated_postgresql.admin_engine)
    viewer_id, role_id = uuid4().hex, uuid4().hex
    with Session(isolated_postgresql.admin_engine) as db:
        template = _template(
            db,
            tenant_id=workspace["tenant_id"],
            scenario_id=workspace["scenario_id"],
            key="scenario_collaboration_template",
            public_source=False,
        )
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
            key="scenario_template_reader",
            name="Scenario template reader",
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
        template_id = template.id

    viewer = {**workspace, "user_id": viewer_id}
    with tenant_session(isolated_postgresql.runtime_engine, viewer) as db:
        rows = templates.list_templates(
            scenario_id=workspace["scenario_id"],
            db=db,
        )
        assert [item.id for item in rows] == [template_id]
        detail = templates.get_template(template_id, db)
        assert detail.id == template_id
        assert detail.scenario_id == workspace["scenario_id"]
        assert detail.versions and detail.versions[0].filename
