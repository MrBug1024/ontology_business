from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import (
    BusinessScenario,
    DatasetHead,
    DatasetSchema,
    DatasetVersion,
    FunctionDefinition,
    LogicalDataset,
    OntologySnapshot,
    ScenarioCapabilityPort,
    ScenarioDatasetBinding,
    Tenant,
    User,
)
from app.services import permission_service, release_service, runtime_definition_service


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed(db):
    tenant = Tenant(id="tenant-release-port", name="Release port tenant")
    user = User(
        id="user-release-port",
        tenant_id=tenant.id,
        email="release-port@example.test",
        password_hash="test-only",
        status="active",
    )
    scenario = BusinessScenario(
        id="scenario-release-port",
        tenant_id=tenant.id,
        name="Generic capability",
    )
    dataset = LogicalDataset(
        id="dataset-release-port",
        tenant_id=tenant.id,
        key="generic.records",
        name="Generic records",
    )
    schema = DatasetSchema(
        id="schema-release-port",
        tenant_id=tenant.id,
        dataset_id=dataset.id,
        schema_version=1,
        schema_hash="a" * 64,
        compatibility="backward",
        schema_document={"type": "array"},
    )
    function = FunctionDefinition(
        id="function-release-port",
        scenario_id=scenario.id,
        name="Process business records",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )
    port = ScenarioCapabilityPort(
        id="port-release-input",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        capability_kind="function",
        capability_key=function.id,
        port_key="records.input",
        name="Business records",
        direction="input",
        role="invocation_input",
        media_kind="dataset",
        dataset_id=dataset.id,
        dataset_schema_id=schema.id,
        schema_document={"type": "array", "items": {"type": "object"}},
        is_required=True,
        binding_policy="per_invocation",
        status="active",
        config={"semantic_requirement": "record collection"},
    )
    db.add_all([tenant, user, scenario, dataset, schema, function, port])
    db.commit()
    permission_service.ensure_organization(db, tenant.id, owner_user_id=user.id)
    db.commit()
    db.info["tenant_id"] = tenant.id
    db.info["user_id"] = user.id
    return tenant, scenario, dataset, schema, port


def test_release_captures_port_contract_without_runtime_data(db) -> None:
    tenant, scenario, dataset, schema, port = _seed(db)
    before = release_service.capture_snapshot_content(db, scenario)
    assert before["capability_contract_version"] == 2
    assert before["capability_ports"] == [
        {
            "id": port.id,
            "capability_kind": "function",
            "capability_key": "function-release-port",
            "port_key": "records.input",
            "name": "Business records",
            "description": "",
            "direction": "input",
            "role": "invocation_input",
            "media_kind": "dataset",
            "schema_document": {"type": "array", "items": {"type": "object"}},
            "dataset_schema_hash": "a" * 64,
            "is_required": True,
            "cardinality": "one",
            "binding_policy": "per_invocation",
            "config": {"semantic_requirement": "record collection"},
        }
    ]
    encoded = json.dumps(before, ensure_ascii=False, sort_keys=True)
    assert dataset.id not in encoded
    assert schema.id not in encoded

    version = DatasetVersion(
        id="version-release-runtime-a",
        tenant_id=tenant.id,
        dataset_id=dataset.id,
        schema_id=schema.id,
        version_number=1,
        status="ready",
        record_count=2,
        fragment_count=0,
        byte_size=0,
        content_hash="b" * 64,
        manifest={},
    )
    binding = ScenarioDatasetBinding(
        id="binding-release-runtime",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        dataset_id=dataset.id,
        binding_key="records.input",
        environment="dev",
        role="invocation_input",
        binding_mode="pinned",
        dataset_version_id=version.id,
        is_required=True,
        status="active",
        config={},
    )
    db.add_all([version, binding])
    db.commit()
    after_binding = release_service.capture_snapshot_content(db, scenario)
    assert release_service.snapshot_hash(after_binding) == release_service.snapshot_hash(before)
    assert version.id not in json.dumps(after_binding, sort_keys=True)

    port.schema_document = {"type": "object"}
    db.commit()
    after_contract_change = release_service.capture_snapshot_content(db, scenario)
    assert release_service.snapshot_hash(after_contract_change) != release_service.snapshot_hash(before)


def test_runtime_definition_reads_v2_ports_and_legacy_v1_as_empty(db) -> None:
    tenant, scenario, _dataset, _schema, _port = _seed(db)
    content = release_service.capture_snapshot_content(db, scenario)
    snapshot = OntologySnapshot(
        id="snapshot-release-port",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        kind="baseline",
        content=content,
        content_hash=release_service.snapshot_hash(content),
    )
    resolved = runtime_definition_service._from_snapshot(
        scenario, "prod", snapshot, release=None
    )
    assert set(resolved.capability_ports) == {"port-release-input"}
    assert resolved.capability_ports["port-release-input"].dataset_schema_hash == "a" * 64

    legacy_raw = {
        key: value
        for key, value in content.items()
        if key not in {"capability_contract_version", "capability_ports"}
    }
    legacy = release_service.normalize_snapshot_content(legacy_raw)
    assert "capability_ports" not in legacy
    legacy_snapshot = OntologySnapshot(
        id="snapshot-release-legacy",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        kind="baseline",
        content=legacy,
        content_hash=release_service.snapshot_hash(legacy),
    )
    legacy_resolved = runtime_definition_service._from_snapshot(
        scenario, "prod", legacy_snapshot, release=None
    )
    assert legacy_resolved.capability_ports == {}

    legacy_unowned = {
        **content,
        "capability_contract_version": 1,
        "capability_ports": [
            {
                key: value
                for key, value in item.items()
                if key not in {"capability_kind", "capability_key"}
            }
            for item in content["capability_ports"]
        ],
    }
    legacy_unowned = release_service.normalize_snapshot_content(legacy_unowned)
    unsafe_snapshot = OntologySnapshot(
        id="snapshot-release-legacy-unowned",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        kind="baseline",
        content=legacy_unowned,
        content_hash=release_service.snapshot_hash(legacy_unowned),
    )
    with pytest.raises(
        runtime_definition_service.RuntimeDefinitionError,
        match="缺少明确归属",
    ):
        runtime_definition_service._from_snapshot(
            scenario, "prod", unsafe_snapshot, release=None
        )


def test_port_contract_rejects_physical_references() -> None:
    content = {
        "scenario": {"name": "Generic", "namespace": "default"},
        "entities": [],
        "relations": [],
        "mappings": [],
        "functions": [],
        "actions": [],
        "rules": [],
        "events": [],
        "workflows": [],
        "capability_contract_version": 1,
        "capability_ports": [
            {
                "id": "port-unsafe-contract",
                "port_key": "records.input",
                "name": "Records",
                "direction": "input",
                "role": "invocation_input",
                "media_kind": "dataset",
                "config": {"dataset_version_id": "must-not-be-published"},
            }
        ],
    }
    with pytest.raises(release_service.ReleaseValidationError, match="不能保存物理资源"):
        release_service.normalize_snapshot_content(content)


@pytest.mark.parametrize("port_key", ["", "   ", ".records", "records input"])
def test_port_contract_rejects_invalid_port_keys(port_key: str) -> None:
    content = {
        "scenario": {"name": "Generic", "namespace": "default"},
        "entities": [],
        "relations": [],
        "mappings": [],
        "functions": [],
        "actions": [],
        "rules": [],
        "events": [],
        "workflows": [],
        "capability_contract_version": 1,
        "capability_ports": [
            {
                "id": "port-invalid-contract",
                "port_key": port_key,
                "name": "Records",
                "direction": "input",
                "role": "invocation_input",
                "media_kind": "dataset",
            }
        ],
    }

    with pytest.raises(release_service.ReleaseValidationError, match="能力端口 key"):
        release_service.normalize_snapshot_content(content)


def test_port_contract_rejects_case_insensitive_duplicate_keys() -> None:
    content = {
        "scenario": {"name": "Generic", "namespace": "default"},
        "entities": [],
        "relations": [],
        "mappings": [],
        "functions": [],
        "actions": [],
        "rules": [],
        "events": [],
        "workflows": [],
        "capability_contract_version": 1,
        "capability_ports": [
            {
                "id": "port-duplicate-a",
                "port_key": "records.input",
                "name": "Records A",
                "direction": "input",
                "role": "invocation_input",
                "media_kind": "dataset",
            },
            {
                "id": "port-duplicate-b",
                "port_key": "Records.Input",
                "name": "Records B",
                "direction": "input",
                "role": "invocation_input",
                "media_kind": "dataset",
            },
        ],
    }

    with pytest.raises(release_service.ReleaseValidationError, match="能力端口 key 不能重复"):
        release_service.normalize_snapshot_content(content)


def test_port_contract_rejects_required_unbound_port() -> None:
    content = {
        "scenario": {"name": "Generic", "namespace": "default"},
        "entities": [],
        "relations": [],
        "mappings": [],
        "functions": [],
        "actions": [],
        "rules": [],
        "events": [],
        "workflows": [],
        "capability_contract_version": 1,
        "capability_ports": [
            {
                "id": "port-required-none",
                "port_key": "records.input",
                "name": "Records",
                "direction": "input",
                "role": "invocation_input",
                "media_kind": "dataset",
                "binding_policy": "none",
                "is_required": True,
            }
        ],
    }

    with pytest.raises(release_service.ReleaseValidationError, match="不能声明为必填"):
        release_service.normalize_snapshot_content(content)


def test_publish_requires_ready_pinned_binding_for_release_pinned_rules(db) -> None:
    tenant, scenario, dataset, schema, port = _seed(db)
    port.role = "rules"
    port.binding_policy = "release_pinned"
    port.config = {"default_binding_key": "Rules.Release"}
    db.commit()
    branch = release_service.create_branch(
        db,
        scenario.id,
        name="rules/release-pinned",
    )

    with pytest.raises(release_service.ReleaseValidationError, match="缺少必填 rules"):
        release_service.publish_snapshot(
            db,
            scenario.id,
            environment="staging",
            confirmed=True,
            branch_id=branch.id,
        )

    version = DatasetVersion(
        id="version-release-rules",
        tenant_id=tenant.id,
        dataset_id=dataset.id,
        schema_id=schema.id,
        version_number=1,
        status="ready",
        record_count=3,
        fragment_count=0,
        byte_size=0,
        content_hash="c" * 64,
        manifest={},
    )
    binding = ScenarioDatasetBinding(
        id="binding-release-rules",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        dataset_id=dataset.id,
        binding_key="Rules.Release",
        environment="staging",
        role="rules",
        binding_mode="pinned",
        dataset_version_id=version.id,
        is_required=True,
        status="active",
        config={},
    )
    db.add_all([version, binding])
    db.commit()

    release = release_service.publish_snapshot(
        db,
        scenario.id,
        environment="staging",
        confirmed=True,
        branch_id=branch.id,
    )
    assert release.snapshot_id == branch.head_snapshot_id


def test_release_pinned_rules_reject_mutable_head_binding(db) -> None:
    tenant, scenario, dataset, schema, port = _seed(db)
    port.role = "rules"
    port.binding_policy = "release_pinned"
    version = DatasetVersion(
        id="version-release-rules-head",
        tenant_id=tenant.id,
        dataset_id=dataset.id,
        schema_id=schema.id,
        version_number=1,
        status="ready",
        content_hash="d" * 64,
    )
    head = DatasetHead(
        id="head-release-rules",
        tenant_id=tenant.id,
        dataset_id=dataset.id,
        environment="prod",
        dataset_version_id=version.id,
    )
    binding = ScenarioDatasetBinding(
        id="binding-release-rules-head",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        dataset_id=dataset.id,
        binding_key=port.port_key,
        environment="prod",
        role="rules",
        binding_mode="head",
        dataset_head_id=head.id,
        is_required=True,
        status="active",
        config={},
    )
    db.add_all([version, head, binding])
    db.commit()
    branch = release_service.create_branch(
        db,
        scenario.id,
        name="rules/head-rejected",
    )

    with pytest.raises(
        release_service.ReleaseValidationError,
        match="release_pinned 端口必须绑定固定数据版本",
    ):
        release_service.publish_snapshot(
            db,
            scenario.id,
            environment="prod",
            confirmed=True,
            branch_id=branch.id,
        )


def test_release_pinned_rules_reject_unready_dataset_version(db) -> None:
    tenant, scenario, dataset, schema, port = _seed(db)
    port.role = "rules"
    port.binding_policy = "release_pinned"
    version = DatasetVersion(
        id="version-release-rules-validating",
        tenant_id=tenant.id,
        dataset_id=dataset.id,
        schema_id=schema.id,
        version_number=1,
        status="validating",
        content_hash="e" * 64,
    )
    binding = ScenarioDatasetBinding(
        id="binding-release-rules-validating",
        tenant_id=tenant.id,
        scenario_id=scenario.id,
        dataset_id=dataset.id,
        binding_key=port.port_key,
        environment="staging",
        role="rules",
        binding_mode="pinned",
        dataset_version_id=version.id,
        status="active",
    )
    db.add_all([version, binding])
    db.commit()
    branch = release_service.create_branch(
        db,
        scenario.id,
        name="rules/unready-version",
    )

    with pytest.raises(release_service.ReleaseValidationError, match="受管绑定尚未就绪"):
        release_service.publish_snapshot(
            db,
            scenario.id,
            environment="staging",
            confirmed=True,
            branch_id=branch.id,
        )
