from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, configure_mappers

from app.database import Base
from app.models import (
    BusinessScenario,
    ConnectorBinding,
    DataMapping,
    DataSource,
    DocumentChunk,
    OntologyEntity,
    OntologyInstance,
    OntologyProperty,
    OntologyRelation,
    RelationDataMapping,
    RelationInstance,
)
from app.routers import scenarios
from app.schemas import InstanceIn, RelationDataMappingIn, RelationInstanceIn
from app.services import (
    agent_engine,
    datasource_service,
    mapping_refresh_service,
    ontology_service,
    release_service,
    runtime_definition_service,
)


def _binding(key: str) -> tuple[str, dict]:
    return key, {"adapter": "postgres", "required_capabilities": ["sql_read"]}


def _write_source_database(path: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE source_rows (
                source_code TEXT PRIMARY KEY,
                source_label INTEGER,
                target_fk TEXT
            );
            CREATE TABLE target_rows (
                target_id TEXT PRIMARY KEY,
                target_name TEXT,
                source_fk TEXT
            );
            CREATE TABLE join_links (
                source_key TEXT,
                target_key TEXT
            );
            INSERT INTO source_rows VALUES (' S-1 ', 0, ' 7 ');
            INSERT INTO target_rows VALUES ('7', '目标七', ' S-1 ');
            INSERT INTO join_links VALUES (' S-1 ', '7');
            """
        )


def _external_connector_fakes(source_path: str):
    """Replace network I/O while retaining a PostgreSQL public contract.

    The temporary SQLite file is only a deterministic test double behind the
    connector service boundary; no production runtime path sees or accepts a
    SQLite DataSource.
    """

    def execute_query(
        sql: str,
        parameters: dict | None,
        limit: int | None,
    ) -> dict:
        statement = datasource_service.validate_read_only_sql(
            sql,
            dialect="postgres",
        )
        resolved_limit = max(1, int(limit or 500))
        with sqlite3.connect(source_path) as connection:
            cursor = connection.execute(statement, parameters or {})
            columns = [str(item[0]) for item in (cursor.description or [])]
            rows = cursor.fetchmany(resolved_limit + 1)
        truncated = len(rows) > resolved_limit
        values = [list(row) for row in rows[:resolved_limit]]
        return {
            "columns": columns,
            "rows": values,
            "row_count": len(values),
            "truncated": truncated,
        }

    def fake_run_query(
        _source,
        sql: str,
        limit: int | None = None,
        *,
        max_rows: int | None = None,
    ) -> dict:
        ceiling = limit if limit is not None else max_rows
        return execute_query(sql, None, ceiling)

    def fake_run_parameterized_query(
        _source,
        sql: str,
        parameters,
        *,
        limit: int | None = None,
    ) -> dict:
        return execute_query(sql, dict(parameters), limit)

    def fake_list_tables(_source) -> list[dict]:
        tables: list[dict] = []
        with sqlite3.connect(source_path) as connection:
            names = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' ORDER BY name"
            ).fetchall()
            for (name,) in names:
                safe_name = str(name).replace('"', '""')
                columns = [
                    {
                        "name": str(row[1]),
                        "type": str(row[2]),
                        "pk": bool(row[5]),
                    }
                    for row in connection.execute(
                        f'PRAGMA table_info("{safe_name}")'
                    ).fetchall()
                ]
                row_count = connection.execute(
                    f'SELECT COUNT(*) FROM "{safe_name}"'
                ).fetchone()[0]
                tables.append({
                    "name": str(name),
                    "columns": columns,
                    "row_count": int(row_count),
                })
        return tables

    return (
        patch.object(
            datasource_service,
            "test_connection",
            return_value=(True, "连接成功"),
        ),
        patch.object(datasource_service, "list_tables", side_effect=fake_list_tables),
        patch.object(datasource_service, "run_query", side_effect=fake_run_query),
        patch.object(
            datasource_service,
            "run_parameterized_query",
            side_effect=fake_run_parameterized_query,
        ),
    )


def _world(tmp_path, *, mode: str | None = "source_fk") -> SimpleNamespace:
    source_path = str(tmp_path / f"source-{mode or 'none'}.sqlite")
    _write_source_database(source_path)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine, expire_on_commit=False)

    scenario = BusinessScenario(
        id="scenario-links",
        tenant_id="tenant-links",
        name="显式关系映射",
        namespace="tests.links",
    )
    source_entity = OntologyEntity(
        id="entity-source",
        scenario=scenario,
        name="源对象",
        namespace="tests.links",
    )
    source_entity.properties = [
        OntologyProperty(
            id="property-source-key",
            name="编码",
            data_type="string",
            is_key=True,
            is_required=True,
        ),
        OntologyProperty(
            id="property-source-title",
            name="标题",
            data_type="integer",
            is_title=True,
        ),
    ]
    target_entity = OntologyEntity(
        id="entity-target",
        scenario=scenario,
        name="目标对象",
        namespace="tests.links",
    )
    target_entity.properties = [
        OntologyProperty(
            id="property-target-key",
            name="标识",
            data_type="integer",
            is_key=True,
            is_required=True,
        ),
        OntologyProperty(
            id="property-target-title",
            name="名称",
            data_type="string",
            is_title=True,
        ),
    ]
    relation = OntologyRelation(
        id="relation-source-target",
        scenario=scenario,
        name="关联",
        source_entity=source_entity,
        target_entity=target_entity,
        relation_type="N:M",
        constraints={},
    )
    data_source = DataSource(
        id="source-physical",
        tenant_id=scenario.tenant_id,
        scenario=scenario,
        name="业务 PostgreSQL",
        type="postgres",
        config={"host": "connector.test.invalid", "database": "fixture"},
        status="ok",
    )
    source_key, source_ref = _binding("source-binding")
    target_key, target_ref = _binding("target-binding")
    source_mapping = DataMapping(
        id="mapping-source",
        scenario=scenario,
        entity=source_entity,
        data_source=data_source,
        data_source_binding_key=source_key,
        data_source_binding_ref=source_ref,
        table_name="source_rows",
        column_map={"编码": "source_code", "标题": "source_label"},
        transform_rules={
            "编码": [{"op": "trim"}],
            "标题": [{"op": "to_integer"}],
        },
    )
    target_mapping = DataMapping(
        id="mapping-target",
        scenario=scenario,
        entity=target_entity,
        data_source=data_source,
        data_source_binding_key=target_key,
        data_source_binding_ref=target_ref,
        table_name="target_rows",
        column_map={"标识": "target_id", "名称": "target_name"},
        transform_rules={"标识": [{"op": "to_integer"}]},
    )
    db.add_all(
        [
            scenario,
            source_entity,
            target_entity,
            relation,
            data_source,
            source_mapping,
            target_mapping,
        ]
    )
    db.commit()

    connector_patchers = _external_connector_fakes(source_path)
    for patcher in connector_patchers:
        patcher.start()

    relation_mapping = None
    if mode:
        payload = {
            "relation_id": relation.id,
            "source_mapping_id": source_mapping.id,
            "target_mapping_id": target_mapping.id,
            "mode": mode,
            "foreign_key_column": (
                "target_fk" if mode == "source_fk" else "source_fk"
            ),
            "join_data_source_id": data_source.id,
            "join_table_name": "join_links",
            "source_key_column": "source_key",
            "target_key_column": "target_key",
        }
        derived, preview = ontology_service.validate_relation_data_mapping(
            db, scenario, payload
        )
        assert preview["ok"] is True
        relation_mapping = RelationDataMapping(
            id=f"relation-mapping-{mode}",
            scenario_id=scenario.id,
            status="ready",
            **derived,
        )
        db.add(relation_mapping)
        db.commit()

    return SimpleNamespace(
        engine=engine,
        db=db,
        source_path=source_path,
        scenario=scenario,
        source_entity=source_entity,
        target_entity=target_entity,
        relation=relation,
        data_source=data_source,
        source_mapping=source_mapping,
        target_mapping=target_mapping,
        relation_mapping=relation_mapping,
        connector_patchers=connector_patchers,
    )


def _close_world(world: SimpleNamespace) -> None:
    for patcher in reversed(world.connector_patchers):
        patcher.stop()
    source_id = str(world.data_source.id)
    world.db.close()
    world.engine.dispose()
    for key, cached in list(datasource_service._engine_cache.items()):
        if key.startswith(f"{source_id}:"):
            cached.dispose()
            datasource_service._engine_cache.pop(key, None)


def _connector_audits(world: SimpleNamespace) -> dict[str, dict]:
    return {
        world.source_mapping.id: {
            "kind": "data_source",
            "environment": "dev",
            "managed": True,
            "binding_key": "source-binding",
            "binding_id": "binding-source-dev",
            "connector_id": world.data_source.id,
            "connector_name": world.data_source.name,
            "adapter_type": "postgres",
        },
        world.target_mapping.id: {
            "kind": "data_source",
            "environment": "dev",
            "managed": True,
            "binding_key": "target-binding",
            "binding_id": "binding-target-dev",
            "connector_id": world.data_source.id,
            "connector_name": world.data_source.name,
            "adapter_type": "postgres",
        },
    }


def _refresh(
    world: SimpleNamespace,
    mapping: DataMapping,
    *,
    relation_mappings=None,
    environment: str = "dev",
) -> dict:
    return ontology_service.import_instances_from_mapping(
        world.db,
        world.scenario,
        mapping,
        data_source=world.data_source,
        environment=environment,
        relation_mappings=relation_mappings,
        mapping_connector_audits=_connector_audits(world),
        relation_connector_audits=(
            {
                world.relation_mapping.id: {
                    **_connector_audits(world)[world.source_mapping.id],
                    "binding_key": world.relation_mapping.data_source_binding_key,
                }
            }
            if world.relation_mapping and world.relation_mapping.mode == "join_table"
            else {}
        ),
        definition_provenance={
            "snapshot_id": "snapshot-1",
            "release_id": "release-1",
            "definition_hash": "definition-hash-1",
            "source": "release" if environment != "dev" else "live",
        },
    )


@pytest.mark.parametrize("mode", ["source_fk", "target_fk"])
@pytest.mark.parametrize("carrier_first", [True, False])
def test_fk_refresh_order_converges_and_applies_endpoint_key_transforms(
    tmp_path, mode: str, carrier_first: bool
) -> None:
    world = _world(tmp_path, mode=mode)
    try:
        carrier = world.source_mapping if mode == "source_fk" else world.target_mapping
        opposite = world.target_mapping if mode == "source_fk" else world.source_mapping
        first, second = (carrier, opposite) if carrier_first else (opposite, carrier)
        _refresh(world, first)
        assert world.relation_mapping.status == "error"
        assert world.db.scalar(select(RelationInstance)) is None

        _refresh(world, second)
        links = world.db.scalars(select(RelationInstance)).all()
        assert len(links) == 1
        assert world.relation_mapping.status == "ok"
        assert world.relation_mapping.last_link_count == 1
        source = world.db.get(OntologyInstance, links[0].source_instance_id)
        target = world.db.get(OntologyInstance, links[0].target_instance_id)
        assert source.source_metadata["record_key"] == "S-1"
        assert target.source_metadata["record_key"] == "7"
        assert source.name == "0"
        assert target.name == "目标七"
        assert links[0].source_metadata["definition_snapshot_id"] == "snapshot-1"
        assert links[0].source_metadata["definition_hash"] == "definition-hash-1"
        assert links[0].source_metadata["connector_ref"]["connector_id"] == world.data_source.id

        _refresh(world, first)
        _refresh(world, second)
        assert len(world.db.scalars(select(RelationInstance)).all()) == 1
    finally:
        _close_world(world)


def test_no_explicit_relation_mapping_means_zero_generated_links(tmp_path) -> None:
    world = _world(tmp_path, mode=None)
    try:
        _refresh(world, world.source_mapping)
        _refresh(world, world.target_mapping)
        assert world.db.scalars(select(RelationInstance)).all() == []
    finally:
        _close_world(world)


def test_fk_full_refresh_removes_changed_null_and_missing_target_links(tmp_path) -> None:
    world = _world(tmp_path, mode="source_fk")
    try:
        _refresh(world, world.target_mapping)
        _refresh(world, world.source_mapping)
        assert len(world.db.scalars(select(RelationInstance)).all()) == 1

        with sqlite3.connect(world.source_path) as connection:
            connection.execute(
                "INSERT INTO target_rows VALUES ('8', '目标八', NULL)"
            )
            connection.execute(
                "UPDATE source_rows SET target_fk = '8' WHERE source_code = ' S-1 '"
            )
        _refresh(world, world.target_mapping)
        _refresh(world, world.source_mapping)
        links = world.db.scalars(select(RelationInstance)).all()
        assert len(links) == 1
        assert links[0].source_metadata["target_record_key"] == "8"

        with sqlite3.connect(world.source_path) as connection:
            connection.execute(
                "UPDATE source_rows SET target_fk = NULL WHERE source_code = ' S-1 '"
            )
        _refresh(world, world.target_mapping)
        assert world.db.scalars(select(RelationInstance)).all() == []

        with sqlite3.connect(world.source_path) as connection:
            connection.execute(
                "UPDATE source_rows SET target_fk = '999' WHERE source_code = ' S-1 '"
            )
        _refresh(world, world.source_mapping)
        assert world.db.scalars(select(RelationInstance)).all() == []
        assert world.relation_mapping.status == "error"
        assert "找不到" in world.relation_mapping.last_error
    finally:
        _close_world(world)


def test_join_table_refresh_is_idempotent_deletes_stale_and_never_deletes_truncated(
    tmp_path,
) -> None:
    world = _world(tmp_path, mode="join_table")
    try:
        assert world.relation_mapping.data_source_binding_key == "source-binding"
        assert "sql_read" in world.relation_mapping.data_source_binding_ref[
            "required_capabilities"
        ]
        _refresh(world, world.source_mapping)
        _refresh(world, world.target_mapping)
        assert len(world.db.scalars(select(RelationInstance)).all()) == 1

        with patch(
            "app.services.ontology_service.datasource_service.run_parameterized_query",
            return_value={
                "columns": ["source_key", "target_key"],
                "rows": [],
                "row_count": 0,
                "truncated": True,
            },
        ):
            _refresh(world, world.source_mapping)
        assert len(world.db.scalars(select(RelationInstance)).all()) == 1

        with sqlite3.connect(world.source_path) as connection:
            connection.execute("DELETE FROM join_links")
        _refresh(world, world.target_mapping)
        assert world.db.scalars(select(RelationInstance)).all() == []
        assert world.relation_mapping.last_link_count == 0
    finally:
        _close_world(world)


def test_relation_mapping_crud_materializes_healthy_join_binding(tmp_path) -> None:
    world = _world(tmp_path, mode=None)
    try:
        payload = RelationDataMappingIn(
            relation_id=world.relation.id,
            source_mapping_id=world.source_mapping.id,
            target_mapping_id=world.target_mapping.id,
            mode="join_table",
            join_data_source_id=world.data_source.id,
            join_table_name="join_links",
            source_key_column="source_key",
            target_key_column="target_key",
        )
        with patch.object(
            scenarios, "_scenario_for_request", return_value=world.scenario
        ):
            created = scenarios.create_relation_mapping(
                world.scenario.id, payload, world.db
            )
        stored_mapping = world.db.get(RelationDataMapping, created.id)
        assert stored_mapping is not None
        binding = world.db.scalar(
            select(ConnectorBinding).where(
                ConnectorBinding.scenario_id == world.scenario.id,
                ConnectorBinding.environment == "dev",
                ConnectorBinding.binding_key
                == stored_mapping.data_source_binding_key,
            )
        )
        assert binding is not None
        assert binding.connector_id == world.data_source.id
        assert binding.health_status == "healthy"
    finally:
        _close_world(world)


def test_relation_mapping_validation_rejects_bad_endpoint_column_and_tenant(tmp_path) -> None:
    world = _world(tmp_path, mode=None)
    base = {
        "relation_id": world.relation.id,
        "source_mapping_id": world.source_mapping.id,
        "target_mapping_id": world.target_mapping.id,
        "mode": "source_fk",
        "foreign_key_column": "target_fk",
    }
    try:
        with pytest.raises(ValueError, match="源对象映射"):
            ontology_service.validate_relation_data_mapping(
                world.db,
                world.scenario,
                {**base, "source_mapping_id": world.target_mapping.id},
            )
        with pytest.raises(ValueError, match="外键列"):
            ontology_service.validate_relation_data_mapping(
                world.db,
                world.scenario,
                {**base, "foreign_key_column": "not_a_column"},
            )
        world.data_source.tenant_id = "another-tenant"
        world.db.commit()
        with pytest.raises(ValueError, match="其他租户"):
            ontology_service.validate_relation_data_mapping(
                world.db, world.scenario, base
            )
    finally:
        _close_world(world)


def _frozen_mapping(mapping: DataMapping, entity: OntologyEntity | None) -> SimpleNamespace:
    frozen = SimpleNamespace(
        id=mapping.id,
        scenario_id=mapping.scenario_id,
        entity_id=mapping.entity_id,
        data_source_id=mapping.data_source_id,
        data_source_binding_key=mapping.data_source_binding_key,
        data_source_binding_ref=dict(mapping.data_source_binding_ref or {}),
        table_name=mapping.table_name,
        column_map=dict(mapping.column_map or {}),
        transform_rules=dict(mapping.transform_rules or {}),
    )
    if entity is not None:
        frozen_entity = SimpleNamespace(
            id=entity.id,
            scenario_id=entity.scenario_id,
            name=entity.name,
            state_property=entity.state_property,
            properties=[
                SimpleNamespace(
                    name=prop.name,
                    data_type=prop.data_type,
                    is_key=prop.is_key,
                    is_title=prop.is_title,
                    is_required=prop.is_required,
                    is_enum=prop.is_enum,
                    enum_values=list(prop.enum_values or []),
                    default_value=prop.default_value,
                    constraints=dict(prop.constraints or {}),
                )
                for prop in entity.properties
            ],
        )
        frozen.entity = frozen_entity
    return frozen


def test_frozen_import_requires_snapshot_entity_and_keeps_environments_isolated(tmp_path) -> None:
    world = _world(tmp_path, mode="source_fk")
    try:
        missing_entity = _frozen_mapping(world.source_mapping, None)
        with pytest.raises(ValueError, match="发布快照中的对象定义"):
            ontology_service.import_instances_from_mapping(
                world.db,
                world.scenario,
                missing_entity,
                data_source=world.data_source,
                environment="staging",
                relation_mappings=[],
            )

        _refresh(world, world.source_mapping, relation_mappings=[])
        _refresh(world, world.target_mapping, relation_mappings=[])
        source_frozen = _frozen_mapping(world.source_mapping, world.source_entity)
        target_frozen = _frozen_mapping(world.target_mapping, world.target_entity)
        staging_provenance = {
            "snapshot_id": "snapshot-1",
            "release_id": "release-1",
            "definition_hash": "definition-hash-1",
            "source": "release",
        }
        ontology_service.import_instances_from_mapping(
            world.db,
            world.scenario,
            source_frozen,
            data_source=world.data_source,
            environment="staging",
            relation_mappings=[],
            definition_provenance=staging_provenance,
        )
        ontology_service.import_instances_from_mapping(
            world.db,
            world.scenario,
            target_frozen,
            data_source=world.data_source,
            environment="staging",
            relation_mappings=[],
            definition_provenance=staging_provenance,
        )
        relation_frozen = SimpleNamespace(
            **{
                field: getattr(world.relation_mapping, field)
                for field in (
                    "id",
                    "relation_id",
                    "source_mapping_id",
                    "target_mapping_id",
                    "mode",
                    "data_source_id",
                    "data_source_binding_key",
                    "data_source_binding_ref",
                    "table_name",
                    "foreign_key_column",
                    "source_key_column",
                    "target_key_column",
                )
            }
        )
        ontology_service.import_instances_from_mapping(
            world.db,
            world.scenario,
            source_frozen,
            data_source=world.data_source,
            environment="staging",
            relation_mappings=[relation_frozen],
            mapping_data_sources={
                source_frozen.id: world.data_source,
                target_frozen.id: world.data_source,
            },
            runtime_mappings={
                source_frozen.id: source_frozen,
                target_frozen.id: target_frozen,
            },
            runtime_relations={world.relation.id: world.relation},
            definition_provenance=staging_provenance,
        )

        imported = world.db.scalars(
            select(OntologyInstance).where(OntologyInstance.source == "imported")
        ).all()
        assert len(imported) == 4
        dev_objects = [
            item
            for item in imported
            if ontology_service.instance_in_runtime_environment(item, "dev")
        ]
        staging_objects = [
            item
            for item in imported
            if ontology_service.instance_in_runtime_environment(item, "staging")
        ]
        assert len(dev_objects) == len(staging_objects) == 2
        links = world.db.scalars(select(RelationInstance)).all()
        assert len(links) == 1
        assert links[0].source_metadata["runtime_environment"] == "staging"

        context = agent_engine.AgentContext.__new__(agent_engine.AgentContext)
        context.agent = SimpleNamespace(scenario_id=world.scenario.id)
        context.entities = [world.source_entity, world.target_entity]
        context.data_sources = [world.data_source]
        context.runtime_definition = SimpleNamespace(
            scenario=world.scenario,
            environment="staging",
            source="release",
            snapshot_id="snapshot-1",
            release_id="release-1",
            definition_hash="definition-hash-1",
            mappings={
                source_frozen.id: source_frozen,
                target_frozen.id: target_frozen,
            },
            relation_mappings={relation_frozen.id: relation_frozen},
        )
        assert context._object_in_data_context(staging_objects[0]) is True
        assert context._object_in_data_context(dev_objects[0]) is False
    finally:
        _close_world(world)


def test_title_name_is_server_synchronized_for_manual_create_and_search(tmp_path) -> None:
    world = _world(tmp_path, mode=None)
    try:
        payload = InstanceIn(
            entity_id=world.source_entity.id,
            name="陈旧独立名称",
            attributes={"编码": "M-1", "标题": 0},
        )
        allow = SimpleNamespace(allowed=True)
        with (
            patch.object(scenarios, "_scenario_for_request", return_value=world.scenario),
            patch.object(
                scenarios.permission_service,
                "require_instance_attribute_write_permissions",
            ),
            patch.object(scenarios, "_instance_out", side_effect=lambda _db, item: item),
        ):
            instance = scenarios.create_instance(world.scenario.id, payload, world.db)
        assert instance.name == "0"

        with (
            patch.object(scenarios, "_scenario_for_request", return_value=world.scenario),
            patch.object(scenarios.permission_service, "check_object", return_value=allow),
            patch.object(
                scenarios.permission_service,
                "filter_instance_attributes",
                side_effect=lambda _db, item: item.attributes,
            ),
        ):
            result = scenarios.search_objects(
                world.scenario.id,
                q="0",
                entity_id=None,
                limit=50,
                offset=0,
                db=world.db,
            )
        assert result.total == 1
        assert result.items[0].name == "0"

        assert ontology_service.resolve_instance_display_name(
            SimpleNamespace(
                properties=[SimpleNamespace(name="开关", is_title=True, is_key=False)]
            ),
            {"开关": False},
            explicit_name="旧名",
        ) == "False"
    finally:
        _close_world(world)


def test_object_search_keeps_large_runtime_data_bounded_and_pageable(tmp_path) -> None:
    world = _world(tmp_path, mode=None)
    try:
        world.db.add_all([
            OntologyInstance(
                id=f"large-runtime-{index:04d}",
                scenario_id=world.scenario.id,
                entity_id=world.source_entity.id,
                name=f"项目对象 {index:04d}",
                attributes={"编码": f"P-{index:04d}", "标题": index},
                source="manual",
            )
            for index in range(260)
        ])
        world.db.commit()
        definition = SimpleNamespace(
            scenario=world.scenario,
            entities={world.source_entity.id: object()},
            mappings={},
        )
        allow = SimpleNamespace(allowed=True)
        with (
            patch.object(scenarios, "_scenario_for_request", return_value=world.scenario),
            patch.object(scenarios, "_runtime_definition_for_scenario", return_value=definition),
            patch.object(scenarios.permission_service, "check_object", return_value=allow),
            patch.object(
                scenarios.permission_service,
                "filter_instance_attributes",
                side_effect=lambda _db, item: item.attributes,
            ),
        ):
            first = scenarios.search_objects(
                world.scenario.id,
                q="",
                entity_id=None,
                limit=50,
                offset=0,
                db=world.db,
            )
            second = scenarios.search_objects(
                world.scenario.id,
                q="",
                entity_id=None,
                limit=50,
                offset=int(first.next_offset or 0),
                db=world.db,
            )

        assert len(first.items) == 50
        assert first.has_more is True
        assert first.total_is_exact is False
        assert first.next_offset == 50
        assert len(second.items) == 50
        assert {item.id for item in first.items}.isdisjoint(
            {item.id for item in second.items}
        )
    finally:
        _close_world(world)


def test_manual_relation_unique_race_returns_stable_409(tmp_path) -> None:
    world = _world(tmp_path, mode=None)
    try:
        source = OntologyInstance(
            id="manual-source",
            scenario_id=world.scenario.id,
            entity_id=world.source_entity.id,
            name="源",
            attributes={"编码": "M-1", "标题": 1},
        )
        target = OntologyInstance(
            id="manual-target",
            scenario_id=world.scenario.id,
            entity_id=world.target_entity.id,
            name="目标",
            attributes={"标识": 2, "名称": "目标"},
        )
        world.db.add_all([source, target])
        world.db.commit()
        payload = RelationInstanceIn(
            relation_id=world.relation.id,
            source_instance_id=source.id,
            target_instance_id=target.id,
        )
        with (
            patch.object(scenarios, "_scenario_for_request", return_value=world.scenario),
            patch.object(scenarios.permission_service, "require_object_permission"),
            patch.object(ontology_service, "validate_relation_instance_create"),
        ):
            scenarios.create_relation_instance(world.scenario.id, payload, world.db)
            with pytest.raises(HTTPException) as caught:
                scenarios.create_relation_instance(world.scenario.id, payload, world.db)
        assert caught.value.status_code == 409
        assert "已存在" in caught.value.detail
    finally:
        _close_world(world)


def test_release_title_normalization_fk_binding_and_runtime_hash(tmp_path) -> None:
    world = _world(tmp_path, mode="source_fk")
    try:
        world.source_entity.properties[1].is_title = False
        world.db.commit()
        content = release_service.capture_snapshot_content(world.db, world.scenario)
        normalized = release_service.normalize_snapshot_content(content)
        source = next(
            item for item in normalized["entities"] if item["id"] == world.source_entity.id
        )
        assert [prop["name"] for prop in source["properties"] if prop["is_title"]] == [
            "编码"
        ]

        bad = release_service.capture_snapshot_content(world.db, world.scenario)
        bad["relation_mappings"][0]["data_source_binding_key"] = "wrong-binding"
        with pytest.raises(release_service.ReleaseValidationError, match="承载侧"):
            release_service.normalize_snapshot_content(bad)

        definition_before = runtime_definition_service.resolve_active(
            world.db, world.scenario, environment="dev"
        )
        relation_fingerprint_before = mapping_refresh_service.relation_mapping_fingerprint(
            definition_before, world.source_mapping.id
        )
        world.relation_mapping.data_source_binding_key = "updated-binding"
        world.db.commit()
        definition_after = runtime_definition_service.resolve_active(
            world.db, world.scenario, environment="dev"
        )
        assert definition_before.definition_hash != definition_after.definition_hash
        assert relation_fingerprint_before != mapping_refresh_service.relation_mapping_fingerprint(
            definition_after, world.source_mapping.id
        )
    finally:
        _close_world(world)


def test_postgresql_mapping_identifiers_are_dialect_safe() -> None:
    assert ontology_service._quoted_mapping_table("sales.orders", "postgres") == (
        '"sales"."orders"'
    )
    with pytest.raises(ValueError):
        ontology_service._quoted_mapping_table("orders; DROP TABLE users", "postgres")


def test_agent_and_scenario_reads_hide_retired_mapping_and_release_facts(tmp_path) -> None:
    world = _world(tmp_path, mode="source_fk")
    try:
        provenance = {
            "runtime_environment": "staging",
            "definition_snapshot_id": "snapshot-current",
            "release_id": "release-current",
            "definition_hash": "hash-current",
            "definition_source": "release",
            "data_source_id": world.data_source.id,
        }
        current_source = OntologyInstance(
            id="runtime-source-current",
            scenario_id=world.scenario.id,
            entity_id=world.source_entity.id,
            name="当前源对象",
            source="imported",
            source_metadata={**provenance, "mapping_id": world.source_mapping.id},
        )
        current_target = OntologyInstance(
            id="runtime-target-current",
            scenario_id=world.scenario.id,
            entity_id=world.target_entity.id,
            name="当前目标对象",
            source="imported",
            source_metadata={**provenance, "mapping_id": world.target_mapping.id},
        )
        retired_mapping_object = OntologyInstance(
            id="runtime-object-retired-mapping",
            scenario_id=world.scenario.id,
            entity_id=world.source_entity.id,
            name="旧映射对象",
            source="imported",
            source_metadata={**provenance, "mapping_id": "mapping-retired"},
        )
        retired_release_object = OntologyInstance(
            id="runtime-object-retired-release",
            scenario_id=world.scenario.id,
            entity_id=world.source_entity.id,
            name="旧发布对象",
            source="imported",
            source_metadata={
                **provenance,
                "mapping_id": world.source_mapping.id,
                "release_id": "release-retired",
            },
        )
        manual_targets = [
            OntologyInstance(
                id=f"runtime-manual-{index}",
                scenario_id=world.scenario.id,
                entity_id=world.target_entity.id,
                name=f"手工目标{index}",
                source="manual",
            )
            for index in range(1, 4)
        ]
        relation_provenance = {
            **provenance,
            "relation_mapping_id": world.relation_mapping.id,
        }
        current_link = RelationInstance(
            id="runtime-link-current",
            scenario_id=world.scenario.id,
            relation_id=world.relation.id,
            source_instance_id=current_source.id,
            target_instance_id=current_target.id,
            source="mapping",
            source_metadata=relation_provenance,
        )
        retired_mapping_link = RelationInstance(
            id="runtime-link-retired-mapping",
            scenario_id=world.scenario.id,
            relation_id=world.relation.id,
            source_instance_id=current_source.id,
            target_instance_id=manual_targets[0].id,
            source="mapping",
            source_metadata={**provenance, "relation_mapping_id": "relation-map-retired"},
        )
        retired_release_link = RelationInstance(
            id="runtime-link-retired-release",
            scenario_id=world.scenario.id,
            relation_id=world.relation.id,
            source_instance_id=current_source.id,
            target_instance_id=manual_targets[1].id,
            source="mapping",
            source_metadata={
                **relation_provenance,
                "definition_hash": "hash-retired",
            },
        )
        manual_link = RelationInstance(
            id="runtime-link-manual",
            scenario_id=world.scenario.id,
            relation_id=world.relation.id,
            source_instance_id=current_source.id,
            target_instance_id=manual_targets[2].id,
            source="manual",
        )
        world.db.add_all(
            [
                current_source,
                current_target,
                retired_mapping_object,
                retired_release_object,
                *manual_targets,
                current_link,
                retired_mapping_link,
                retired_release_link,
                manual_link,
            ]
        )
        world.db.commit()
        definition = SimpleNamespace(
            scenario=world.scenario,
            environment="staging",
            source="release",
            snapshot_id="snapshot-current",
            release_id="release-current",
            definition_hash="hash-current",
            mappings={
                world.source_mapping.id: world.source_mapping,
                world.target_mapping.id: world.target_mapping,
            },
            relation_mappings={world.relation_mapping.id: world.relation_mapping},
        )
        allowed = SimpleNamespace(allowed=True)

        context = agent_engine.AgentContext.__new__(agent_engine.AgentContext)
        context.db = world.db
        context.agent = SimpleNamespace(scenario_id=world.scenario.id)
        context.scenario = world.scenario
        context.entities = [world.source_entity, world.target_entity]
        context.relations = [world.relation]
        context.data_sources = [world.data_source]
        context.runtime_definition = definition
        with (
            patch.object(
                agent_engine.permission_service, "check_object", return_value=allowed
            ),
            patch.object(
                agent_engine.permission_service,
                "filter_instance_attributes",
                side_effect=lambda _db, item: item.attributes or {},
            ),
        ):
            assert context._ontology_object(retired_mapping_object.id)["error"]
            assert context._ontology_object(retired_release_object.id)["error"]
            agent_detail = context._ontology_object(current_source.id)
        assert {item["id"] for item in agent_detail["relations"]} == {
            current_link.id,
            manual_link.id,
        }

        with (
            patch.object(scenarios, "_scenario_for_request", return_value=world.scenario),
            patch.object(
                scenarios, "_runtime_definition_for_scenario", return_value=definition
            ),
            patch.object(scenarios.permission_service, "check_object", return_value=allowed),
            patch.object(scenarios.permission_service, "require_object_permission"),
            patch.object(
                scenarios.permission_service,
                "filter_instance_attributes",
                side_effect=lambda _db, item: item.attributes or {},
            ),
        ):
            with pytest.raises(HTTPException) as retired_mapping_error:
                scenarios.get_object(
                    world.scenario.id, retired_mapping_object.id, world.db
                )
            with pytest.raises(HTTPException) as retired_release_error:
                scenarios.get_object(
                    world.scenario.id, retired_release_object.id, world.db
                )
            scenario_detail = scenarios.get_object(
                world.scenario.id, current_source.id, world.db
            )
        assert retired_mapping_error.value.status_code == 404
        assert retired_release_error.value.status_code == 404
        assert {item.id for item in scenario_detail.relations} == {
            current_link.id,
            manual_link.id,
        }
    finally:
        _close_world(world)


def test_mixed_mapping_catalog_only_exposes_object_tables_to_sql_tools(tmp_path) -> None:
    world = _world(tmp_path, mode="join_table")
    try:
        context = agent_engine.AgentContext.__new__(agent_engine.AgentContext)
        context.db = world.db
        context.agent = SimpleNamespace(scenario_id=world.scenario.id)
        context.entities = [world.source_entity, world.target_entity]
        context.relations = [world.relation]
        context.mappings = [world.source_mapping, world.target_mapping]
        context.relation_mappings = [world.relation_mapping]
        context.data_sources = [world.data_source]
        table_rows = [
            {
                "name": "source_rows",
                "columns": [{"name": "source_code"}, {"name": "target_fk"}],
            },
            {
                "name": "target_rows",
                "columns": [{"name": "target_id"}, {"name": "target_name"}],
            },
            {
                "name": "join_links",
                "columns": [{"name": "source_key"}, {"name": "target_key"}],
            },
        ]
        with (
            patch.object(
                agent_engine.permission_service, "can_read_property", return_value=True
            ),
            patch.object(
                agent_engine.datasource_service, "list_tables", return_value=table_rows
            ),
        ):
            catalog = context._mapping_catalog()
            assert {item["kind"] for item in catalog} == {"object", "relation"}
            visible_tables = json.loads(
                context.execute_tool(
                    "list_tables", {"data_source_id": world.data_source.id}
                )
            )
            assert {item["name"] for item in visible_tables} == {
                "source_rows",
                "target_rows",
            }
            assert context.validate_sql_query(
                world.data_source.id,
                "SELECT source_code FROM source_rows",
            ) == "SELECT source_code FROM source_rows"
            assert context.authorize_historic_tool_result(
                "list_tables",
                {"data_source_id": world.data_source.id},
                json.dumps(visible_tables, ensure_ascii=False),
            ) is True
    finally:
        _close_world(world)
