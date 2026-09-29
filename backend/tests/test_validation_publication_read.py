from types import SimpleNamespace

import pytest

from app.services import validation_dataset_service as service


class PublicationDatabase:
    def __init__(self, *, retired=False, changed=False):
        self.statements = []
        self.dataset = SimpleNamespace(id="dataset", tenant_id="tenant", lifecycle_status="active",
            labels={"catalog_purpose": "validation_dataset", "owner_agent_id": "agent"})
        self.asset = SimpleNamespace(id="asset")
        self.version = SimpleNamespace(id="version", asset_id="asset", bucket_file_id="file",
            content_sha256="changed" if changed else "hash")
        self.singles = iter([SimpleNamespace(id="agent"), self.dataset, "retired" if retired else None])
        self.lists = iter([["asset"], [self.asset], [self.version]])

    def scalar(self, statement):
        self.statements.append(statement)
        return next(self.singles)

    def scalars(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(all=lambda: next(self.lists))


@pytest.fixture
def publication_scope(monkeypatch):
    monkeypatch.setattr(service.permission_service, "refresh_request_authorization", lambda db: None)
    monkeypatch.setattr(service, "_validate_agent_scope", lambda *args, **kwargs: None)
    lineage = []
    monkeypatch.setattr(service, "_validate_input_source_lineage", lambda *args, **kwargs: lineage.append(kwargs))
    def publish(db):
        return service._lock_validation_publication_scope(db, tenant_id="tenant", agent_id="agent",
            dataset_key="package", expected_inputs={"version": ("asset", "file", "hash")})
    return publish, lineage


def test_publication_locks_mutable_parents_and_reads_immutable_versions(publication_scope):
    publish, lineage = publication_scope
    db = PublicationDatabase()
    assert publish(db) is db.dataset
    assert len(lineage) == 1
    assert [statement._for_update_arg is not None for statement in db.statements] == [
        True, True, False, False, True, False,
    ]
    assert db.statements[-1].get_execution_options()["populate_existing"] is True


@pytest.mark.parametrize("failure", ["retired", "changed"])
def test_publication_rejects_retirement_or_changed_content(publication_scope, failure):
    publish, lineage = publication_scope
    with pytest.raises(service.ValidationDatasetError):
        publish(PublicationDatabase(**{failure: True}))
    assert lineage == []
