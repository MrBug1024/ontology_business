from types import SimpleNamespace

from app.services import managed_attachment_access as access


def test_agent_attachment_locks_asset_but_only_reads_immutable_version(monkeypatch):
    asset = SimpleNamespace(id="asset", tenant_id="tenant", lifecycle_status="active",
        usage_plane="invocation_input", labels={"catalog_purpose": "validation_asset"})
    version = SimpleNamespace(id="version", asset_id="asset", tenant_id="tenant",
        version_document={}, content_sha256="a" * 64)

    class Database:
        def __init__(self):
            self.statements = []
            self.rows = iter([version, asset, version])

        def scalar(self, statement):
            self.statements.append(statement)
            return next(self.rows)

    db = Database()
    monkeypatch.setattr(access.tenant_service, "current_tenant_id", lambda db: "tenant")
    monkeypatch.setattr(access.managed_asset_lifecycle, "require_current_asset_version", lambda doc: None)
    checked = []
    monkeypatch.setattr(access, "require_asset_version_scope", lambda *args, **kwargs: checked.append(kwargs))
    attachment = SimpleNamespace(upload_run_id=None, asset_version_id="version",
        dataset_version_id=None, expected_signature="a" * 64)

    access.validate_attachments(db, [attachment], user_id="user", agent_id="agent")

    assert len(checked) == 1
    assert db.statements[1]._for_update_arg is not None
    assert db.statements[2]._for_update_arg is None
    assert db.statements[2].get_execution_options()["populate_existing"] is True
