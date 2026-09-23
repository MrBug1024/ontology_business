from types import SimpleNamespace

from app.services import catalog_service


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _PostgresDb:
    def __init__(self):
        self.statement = None
        self.parameters = None

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def scalar(self, statement, parameters):
        self.statement = statement
        self.parameters = parameters
        return 2


class _SqliteDb:
    def __init__(self, rows):
        self.rows = iter(rows)

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    def scalars(self, _statement):
        return _ScalarResult(next(self.rows))


def test_validation_version_retirement_uses_scoped_postgresql_function():
    db = _PostgresDb()

    retired = catalog_service.retire_validation_dataset_versions(
        db,
        tenant_id="tenant-id",
        agent_id="agent-id",
        dataset_ids={"dataset-b", "dataset-a", "dataset-a"},
    )

    assert retired == 2
    assert "public.retire_validation_dataset_versions" in str(db.statement)
    assert db.parameters == {
        "tenant_id": "tenant-id",
        "agent_id": "agent-id",
        "dataset_ids": ["dataset-a", "dataset-b"],
    }


def test_local_validation_version_retirement_keeps_owner_scope_and_status_transition():
    dataset = SimpleNamespace(
        id="dataset-id",
        tenant_id="tenant-id",
        usage_plane="invocation_input",
        labels={
            "catalog_purpose": "validation_dataset",
            "owner_agent_id": "agent-id",
        },
    )
    active_version = SimpleNamespace(status="ready")
    retired_version = SimpleNamespace(status="retired")
    db = _SqliteDb([[dataset], [active_version, retired_version]])

    changed = catalog_service.retire_validation_dataset_versions(
        db,
        tenant_id="tenant-id",
        agent_id="agent-id",
        dataset_ids=["dataset-id"],
    )

    assert changed == 1
    assert active_version.status == "retired"
    assert retired_version.status == "retired"
