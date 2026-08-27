"""Restore the wage-warning contract without inventing an operational source.

The current demo scenario is named ``农名工欠薪预警``.  This upgrade only
materializes the document-backed ontology contract: object/link definitions,
two explicitly marked example objects, and inert operation definitions.  It
does not bind a DataMapping, treat an attachment bucket as a wage ledger, or
create an Agent.  Existing drafts, sources, and unrelated formal resources are
left in place.

Run from ``backend``::

    python -m examples.upgrade_wage_warning
"""
from __future__ import annotations

import copy
from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import (
    BusinessScenario,
    FunctionDefinition,
    OntologyAction,
    OntologyEntity,
    OntologyEvent,
    OntologyInstance,
    OntologyProperty,
    OntologyRelation,
    OntologyRule,
    OntologyWorkflow,
)
from app.services import (
    function_definition_service,
    operations_service,
    workflow_service,
)


SCENARIO_NAME = "农名工欠薪预警"
NAMESPACE = "wage_warning"
EXAMPLE_SOURCE_PREFIX = "example:wage-warning:"


def _property(
    name: str,
    api_name: str,
    data_type: str = "string",
    *,
    key: bool = False,
    title: bool = False,
    required: bool = False,
    enum_values: Iterable[str] = (),
) -> dict[str, Any]:
    values = list(enum_values)
    return {
        "name": name,
        "api_name": api_name,
        "data_type": data_type,
        "description": "建筑领域欠薪预警业务字段",
        "is_key": key,
        "is_title": title,
        "is_required": key or required,
        "is_enum": bool(values),
        "enum_values": values,
        "default_value": None,
        "constraints": {},
        "is_sensitive": False,
    }


ENTITY_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "建设项目",
        "api_name": "project",
        "description": "建筑施工项目及其欠薪风险治理范围。",
        "state_property": "项目状态",
        "properties": (
            _property("项目编号", "project_id", key=True, title=True),
            _property("项目名称", "project_name"),
            _property("建设单位", "construction_unit"),
            _property(
                "项目状态",
                "project_status",
                required=True,
                enum_values=("筹备", "在建", "停工", "竣工"),
            ),
        ),
    },
    {
        "name": "农民工",
        "api_name": "worker",
        "description": "参与建设项目施工并产生工资权益的劳动者。",
        "state_property": "劳动状态",
        "properties": (
            _property("人员编号", "worker_id", key=True, title=True),
            _property("姓名", "worker_name"),
            _property("所属班组", "team_name"),
            _property("进场日期", "entry_date", "date"),
            _property(
                "劳动状态",
                "employment_status",
                required=True,
                enum_values=("待进场", "在场", "退场"),
            ),
        ),
    },
    {
        "name": "施工企业",
        "api_name": "contractor",
        "description": "承担施工、用工和工资支付责任的参与主体。",
        "state_property": "企业状态",
        "properties": (
            _property("统一社会信用代码", "credit_code", key=True, title=True),
            _property("企业名称", "contractor_name"),
            _property("信用等级", "credit_rating"),
            _property(
                "企业状态",
                "contractor_status",
                required=True,
                enum_values=("正常", "整改中", "限制承接"),
            ),
        ),
    },
    {
        "name": "工资台账",
        "api_name": "wage_ledger",
        "description": "记录应发工资、实发工资、支付日期和支付凭证。",
        "state_property": "支付状态",
        "properties": (
            _property("台账编号", "ledger_id", key=True, title=True),
            _property("人员编号", "worker_id"),
            _property("应发工资", "wage_due", "number"),
            _property("实发工资", "wage_paid", "number"),
            _property("应付日期", "due_date", "date"),
            _property("支付日期", "payment_date", "date"),
            _property("逾期天数", "overdue_days", "integer"),
            _property(
                "支付状态",
                "payment_status",
                required=True,
                enum_values=("待支付", "部分支付", "已支付", "逾期"),
            ),
        ),
    },
    {
        "name": "欠薪风险",
        "api_name": "wage_risk",
        "description": "由工资、考勤、专户和投诉等证据识别出的欠薪风险。",
        "state_property": "风险状态",
        "properties": (
            _property("风险编号", "risk_id", key=True, title=True),
            _property("风险等级", "risk_level"),
            _property("逾期天数", "overdue_days", "integer"),
            _property("风险原因", "risk_reason"),
            _property(
                "风险状态",
                "risk_status",
                required=True,
                enum_values=("待识别", "待复核", "已确认", "已消除"),
            ),
        ),
    },
    {
        "name": "欠薪预警",
        "api_name": "warning",
        "description": "面向监管人员的欠薪风险预警及处置记录。",
        "state_property": "处置状态",
        "properties": (
            _property("预警编号", "warning_id", key=True, title=True),
            _property("预警级别", "warning_level"),
            _property("生成时间", "created_at", "datetime"),
            _property(
                "处置状态",
                "disposal_status",
                required=True,
                enum_values=("待处置", "处置中", "已关闭"),
            ),
        ),
    },
)


RELATION_SPECS: tuple[dict[str, str], ...] = (
    {
        "name": "项目雇佣农民工",
        "api_name": "project_workers",
        "source": "建设项目",
        "target": "农民工",
        "relation_type": "N:M",
        "source_display_name": "参建农民工",
        "source_api_name": "workers",
        "target_display_name": "参与项目",
        "target_api_name": "projects",
    },
    {
        "name": "项目由施工企业承建",
        "api_name": "project_contractor",
        "source": "建设项目",
        "target": "施工企业",
        "relation_type": "N:1",
        "source_display_name": "施工企业",
        "source_api_name": "contractor",
        "target_display_name": "承建项目",
        "target_api_name": "projects",
    },
    {
        "name": "农民工拥有工资台账",
        "api_name": "worker_wage_ledgers",
        "source": "农民工",
        "target": "工资台账",
        "relation_type": "1:N",
        "source_display_name": "工资台账",
        "source_api_name": "wage_ledgers",
        "target_display_name": "所属农民工",
        "target_api_name": "worker",
    },
    {
        "name": "工资台账触发欠薪风险",
        "api_name": "wage_ledger_risks",
        "source": "工资台账",
        "target": "欠薪风险",
        "relation_type": "1:N",
        "source_display_name": "欠薪风险",
        "source_api_name": "risks",
        "target_display_name": "来源工资台账",
        "target_api_name": "wage_ledger",
    },
    {
        "name": "欠薪风险生成预警",
        "api_name": "risk_warnings",
        "source": "欠薪风险",
        "target": "欠薪预警",
        "relation_type": "1:N",
        "source_display_name": "欠薪预警",
        "source_api_name": "warnings",
        "target_display_name": "来源欠薪风险",
        "target_api_name": "risk",
    },
)


FUNCTION_SPEC = {
    "name": "计算工资逾期天数",
    "description": "根据应付日期和支付日期计算逾期天数。",
    "input_schema": {
        "type": "object",
        "properties": {
            "应付日期": {"type": "string", "format": "date"},
            "支付日期": {"type": "string", "format": "date"},
        },
        "required": ["应付日期"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {"逾期天数": {"type": "integer"}},
        "additionalProperties": False,
    },
    "tags": ["欠薪", "风险"],
    "visibility": "scenario",
    "runtime_kind": "contract",
    "runtime_config": {},
}

ACTION_NAME = "生成欠薪预警"
RULE_NAME = "工资逾期欠薪预警"
EVENT_NAME = "欠薪预警已生成"
WORKFLOW_NAME = "欠薪预警处置流程"
RECOVERY_MARKER = "[recovery-pack:wage-warning]"
ACTION_DESCRIPTION = "根据确认的欠薪风险生成预警记录；绑定受治理执行器前不可执行。"
ACTION_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"风险编号": {"type": "string"}},
    "required": ["风险编号"],
    "additionalProperties": False,
}
ACTION_PRECONDITION = "欠薪风险已确认"
ACTION_POSTCONDITION = "生成欠薪预警并进入处置流程"
RULE_DESCRIPTION = "工资逾期超过 90 天或实发工资低于应发工资时触发欠薪风险。"
RULE_CONDITION = {
    "op": "or",
    "conditions": [
        {"field": "逾期天数", "op": ">=", "value": 90},
        {"field": "实发工资", "op": "<", "value_field": "应发工资"},
    ],
}
EVENT_DESCRIPTION = "欠薪风险通过规则后发布预警事件。"
EVENT_PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {"预警编号": {"type": "string"}},
    "required": ["预警编号"],
    "additionalProperties": False,
}
EVENT_TRIGGER_SOURCE = "欠薪风险识别"
WORKFLOW_DESCRIPTION = "识别风险、生成预警并等待监管人员处置；绑定执行器并评审前保持停用。"


T = TypeVar("T")


def _identity_match(
    values: Iterable[T],
    *,
    name: str,
    api_name: str | None,
    label: str,
) -> T | None:
    matches = {
        str(getattr(value, "id")): value
        for value in values
        if getattr(value, "name", None) == name
        or (api_name is not None and getattr(value, "api_name", None) == api_name)
    }
    if len(matches) > 1:
        raise RuntimeError(
            f"{label}身份冲突：name={name!r} 与 api_name={api_name!r} 指向不同记录"
        )
    return next(iter(matches.values()), None)


def _named_match(values: Iterable[T], *, name: str, label: str) -> T | None:
    matches = [value for value in values if getattr(value, "name", None) == name]
    if len(matches) > 1:
        raise RuntimeError(f"{label}名称不唯一：{name}")
    return matches[0] if matches else None


def _marked_description(description: str) -> str:
    return f"{description.rstrip()}\n{RECOVERY_MARKER}"


def _has_recovery_marker(value: Any) -> bool:
    return RECOVERY_MARKER in {
        line.strip() for line in str(getattr(value, "description", "") or "").splitlines()
    }


def _claim_named_resource(
    values: Iterable[T],
    *,
    name: str,
    label: str,
    legacy_matches: Callable[[T], bool],
) -> T | None:
    """Return only a pack-owned row or an exact inert row from pack v1.

    Name alone is display metadata and cannot prove ownership.  The narrowly
    defined legacy predicate exists solely for databases that already ran the
    first recovery script before it wrote a provenance marker.
    """
    value = _named_match(values, name=name, label=label)
    if value is None or _has_recovery_marker(value):
        return value
    if legacy_matches(value):
        return value
    raise RuntimeError(
        f"{label}“{name}”已被未标记资源占用；恢复包不会按名称覆盖现有定义"
    )


def _claim_identity_resource(
    values: Iterable[T],
    *,
    name: str,
    api_name: str,
    label: str,
    legacy_matches: Callable[[T], bool],
) -> T | None:
    """Claim an identity only when it is pack-owned or exact recovery-pack v1."""
    value = _identity_match(
        values,
        name=name,
        api_name=api_name,
        label=label,
    )
    if value is None or _has_recovery_marker(value):
        return value
    if legacy_matches(value):
        return value
    raise RuntimeError(
        f"{label}“{name}”已被未标记资源占用；恢复包不会覆盖有差异的现有定义"
    )


def _legacy_entity_matches(entity: OntologyEntity, spec: dict[str, Any]) -> bool:
    return (
        entity.name == spec["name"]
        and entity.api_name == spec["api_name"]
        and entity.namespace == NAMESPACE
        and (entity.lifecycle_status or "active") == "active"
        and entity.description == spec["description"]
        and entity.is_abstract is False
        and entity.state_property == spec["state_property"]
    )


def _legacy_property_matches(prop: OntologyProperty, spec: dict[str, Any]) -> bool:
    return all(
        getattr(prop, field) == spec[field]
        for field in (
            "name",
            "api_name",
            "data_type",
            "description",
            "is_key",
            "is_title",
            "is_required",
            "is_enum",
            "enum_values",
            "default_value",
            "constraints",
            "is_sensitive",
        )
    )


def _legacy_relation_matches(
    relation: OntologyRelation,
    spec: dict[str, Any],
    *,
    source_entity_id: str,
    target_entity_id: str,
) -> bool:
    return (
        relation.name == spec["name"]
        and relation.api_name == spec["api_name"]
        and relation.namespace == NAMESPACE
        and relation.source_entity_id == source_entity_id
        and relation.target_entity_id == target_entity_id
        and relation.source_display_name == spec["source_display_name"]
        and relation.source_api_name == spec["source_api_name"]
        and relation.target_display_name == spec["target_display_name"]
        and relation.target_api_name == spec["target_api_name"]
        and relation.storage_kind == "none"
        and relation.relation_type == spec["relation_type"]
        and relation.constraints == {}
        and relation.description
        == f"{spec['source']}与{spec['target']}的概念关系；尚未绑定物理数据。"
    )


def _find_scenario(db, scenario_id: str | None) -> BusinessScenario:
    if scenario_id:
        scenario = db.get(BusinessScenario, scenario_id)
        if scenario is None:
            raise RuntimeError(f"找不到业务场景：{scenario_id}")
        if scenario.name != SCENARIO_NAME:
            raise RuntimeError(
                f"场景 {scenario_id} 名称为“{scenario.name}”，不是“{SCENARIO_NAME}”"
            )
        return scenario
    matches = db.scalars(
        select(BusinessScenario).where(BusinessScenario.name == SCENARIO_NAME)
    ).all()
    if not matches:
        raise RuntimeError(f"找不到业务场景“{SCENARIO_NAME}”；升级不会自动创建场景")
    if len(matches) > 1:
        raise RuntimeError(
            f"业务场景“{SCENARIO_NAME}”存在 {len(matches)} 条，请显式传入 scenario_id"
        )
    return matches[0]


def _upsert_entities(db, scenario: BusinessScenario) -> dict[str, OntologyEntity]:
    existing = list(db.scalars(
        select(OntologyEntity).where(OntologyEntity.scenario_id == scenario.id)
    ))
    entities: dict[str, OntologyEntity] = {}
    for spec in ENTITY_SPECS:
        entity = _claim_identity_resource(
            existing,
            name=spec["name"],
            api_name=spec["api_name"],
            label="对象类型",
            legacy_matches=lambda value, entity_spec=spec: _legacy_entity_matches(
                value, entity_spec
            ),
        )
        legacy_entity = entity is not None and not _has_recovery_marker(entity)
        if entity is None:
            entity = OntologyEntity(
                scenario_id=scenario.id,
                name=spec["name"],
                api_name=spec["api_name"],
            )
            db.add(entity)
            db.flush()
            existing.append(entity)
        entity.name = spec["name"]
        entity.api_name = spec["api_name"]
        entity.namespace = NAMESPACE
        entity.lifecycle_status = "active"
        entity.description = _marked_description(spec["description"])
        entity.is_abstract = False
        entity.state_property = spec["state_property"]

        properties = list(db.scalars(
            select(OntologyProperty).where(OntologyProperty.entity_id == entity.id)
        ))
        if legacy_entity:
            expected_identities = {
                (item["name"], item["api_name"])
                for item in spec["properties"]
            }
            actual_identities = {
                (item.name, item.api_name)
                for item in properties
            }
            if actual_identities != expected_identities:
                raise RuntimeError(
                    f"对象类型“{spec['name']}”的属性集合与恢复包 v1 不一致；"
                    "恢复包不会覆盖或删除现有字段"
                )
        for property_spec in spec["properties"]:
            prop = _claim_identity_resource(
                properties,
                name=property_spec["name"],
                api_name=property_spec["api_name"],
                label=f"对象类型“{entity.name}”的属性",
                legacy_matches=lambda value, expected=property_spec: (
                    _legacy_property_matches(value, expected)
                ),
            )
            if prop is None:
                prop = OntologyProperty(
                    entity_id=entity.id,
                    name=property_spec["name"],
                    api_name=property_spec["api_name"],
                )
                db.add(prop)
                properties.append(prop)
            for field in (
                "name",
                "api_name",
                "data_type",
                "description",
                "is_key",
                "is_title",
                "is_required",
                "is_enum",
                "enum_values",
                "default_value",
                "constraints",
                "is_sensitive",
            ):
                setattr(prop, field, property_spec[field])
            prop.description = _marked_description(property_spec["description"])
        entities[entity.name] = entity
    db.flush()
    return entities


def _upsert_relations(
    db,
    scenario: BusinessScenario,
    entities: dict[str, OntologyEntity],
) -> list[OntologyRelation]:
    existing = list(db.scalars(
        select(OntologyRelation).where(OntologyRelation.scenario_id == scenario.id)
    ))
    relations: list[OntologyRelation] = []
    for spec in RELATION_SPECS:
        source_entity_id = entities[spec["source"]].id
        target_entity_id = entities[spec["target"]].id
        relation = _claim_identity_resource(
            existing,
            name=spec["name"],
            api_name=spec["api_name"],
            label="关系类型",
            legacy_matches=lambda value, relation_spec=spec, source_id=source_entity_id, target_id=target_entity_id: (
                _legacy_relation_matches(
                    value,
                    relation_spec,
                    source_entity_id=source_id,
                    target_entity_id=target_id,
                )
            ),
        )
        if relation is None:
            relation = OntologyRelation(
                scenario_id=scenario.id,
                name=spec["name"],
                api_name=spec["api_name"],
                source_entity_id=source_entity_id,
                target_entity_id=target_entity_id,
            )
            db.add(relation)
            existing.append(relation)
        relation.name = spec["name"]
        relation.api_name = spec["api_name"]
        relation.namespace = NAMESPACE
        relation.source_entity_id = source_entity_id
        relation.target_entity_id = target_entity_id
        relation.source_display_name = spec["source_display_name"]
        relation.source_api_name = spec["source_api_name"]
        relation.target_display_name = spec["target_display_name"]
        relation.target_api_name = spec["target_api_name"]
        relation.storage_kind = "none"
        relation.relation_type = spec["relation_type"]
        relation.constraints = {}
        relation.description = _marked_description(
            f"{spec['source']}与{spec['target']}的概念关系；尚未绑定物理数据。"
        )
        relations.append(relation)
    db.flush()
    return relations


def _upsert_example_instances(
    db,
    scenario: BusinessScenario,
    entities: dict[str, OntologyEntity],
) -> list[OntologyInstance]:
    specs = (
        {
            "contract_key": "instance.worker.demo",
            "entity": "农民工",
            "name": "示例农民工",
            "attributes": {
                "人员编号": "WORKER-001",
                "姓名": "示例农民工",
                "劳动状态": "待进场",
            },
            "state": "待进场",
        },
        {
            "contract_key": "instance.project.demo",
            "entity": "建设项目",
            "name": "示例建设项目",
            "attributes": {
                "项目编号": "PROJECT-001",
                "项目名称": "示例建设项目",
                "项目状态": "筹备",
            },
            "state": "筹备",
        },
    )
    existing = list(db.scalars(
        select(OntologyInstance).where(OntologyInstance.scenario_id == scenario.id)
    ))
    instances: list[OntologyInstance] = []
    for spec in specs:
        source_ref = f"{EXAMPLE_SOURCE_PREFIX}{spec['contract_key']}"
        matches = [item for item in existing if item.source_ref == source_ref]
        if len(matches) > 1:
            raise RuntimeError(f"示例对象来源标识不唯一：{source_ref}")
        instance = matches[0] if matches else None
        if instance is None:
            instance = OntologyInstance(
                scenario_id=scenario.id,
                entity_id=entities[spec["entity"]].id,
                name=spec["name"],
                source_ref=source_ref,
            )
            db.add(instance)
            existing.append(instance)
        instance.entity_id = entities[spec["entity"]].id
        instance.name = spec["name"]
        instance.attributes = dict(spec["attributes"])
        instance.source = "manual"
        instance.source_ref = source_ref
        instance.source_metadata = {
            "record_kind": "example",
            "contract_key": spec["contract_key"],
            "notice": "仅用于展示本体结构，不代表真实工资或项目台账数据",
        }
        instance.state = spec["state"]
        instance.quality = {"record_kind": "example", "verified_business_fact": False}
        instance.access_scope = "tenant"
        instances.append(instance)
    db.flush()
    return instances


def _legacy_function_matches(function: FunctionDefinition) -> bool:
    expected = function_definition_service.normalize_definition(FUNCTION_SPEC)
    return all(getattr(function, field) == value for field, value in expected.items())


def _legacy_action_matches(
    action: OntologyAction,
    *,
    entity_id: str,
    input_schema: dict[str, Any],
) -> bool:
    return (
        action.entity_id == entity_id
        and action.description == ACTION_DESCRIPTION
        and action.input_schema == input_schema
        and action.executor_type == "unbound"
        and action.executor_config == {}
        and action.precondition == ACTION_PRECONDITION
        and action.postcondition == ACTION_POSTCONDITION
        and action.enabled is False
        and action.requires_confirmation is True
        and action.idempotency_required is True
        and action.permission_scope == "scenario"
        and action.access_scope == "tenant"
    )


def _legacy_rule_matches(
    rule: OntologyRule,
    *,
    entity_id: str,
    action_id: str,
) -> bool:
    return (
        rule.entity_id == entity_id
        and rule.description == RULE_DESCRIPTION
        and rule.condition == RULE_CONDITION
        and rule.action_on_match == ACTION_NAME
        and rule.trigger_action_ids == [action_id]
        and rule.severity == "critical"
        and rule.enabled is False
    )


def _legacy_event_matches(
    event: OntologyEvent,
    *,
    payload_schema: dict[str, Any],
) -> bool:
    return (
        event.description == EVENT_DESCRIPTION
        and event.payload_schema == payload_schema
        and event.trigger_source == EVENT_TRIGGER_SOURCE
        and event.enabled is False
    )


def _workflow_graph(action_id: str) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    nodes = [
        {
            "id": "start",
            "type": "start",
            "name": "开始",
            "position": {"x": 0, "y": 100},
            "data": {"label": "开始"},
        },
        {
            "id": "create_warning",
            "type": "action",
            "name": ACTION_NAME,
            "position": {"x": 180, "y": 100},
            "data": {"label": ACTION_NAME, "action_id": action_id},
        },
        {
            "id": "review",
            "type": "approval",
            "name": "监管人员复核",
            "position": {"x": 360, "y": 100},
            "data": {
                "label": "监管人员复核",
                "timeout_seconds": 86400,
                "on_timeout": "reject",
            },
        },
        {
            "id": "end",
            "type": "end",
            "name": "结束",
            "position": {"x": 540, "y": 100},
            "data": {"label": "结束"},
        },
    ]
    edges = [
        {"id": "e1", "source": "start", "target": "create_warning", "label": ""},
        {"id": "e2", "source": "create_warning", "target": "review", "label": ""},
        {"id": "e3", "source": "review", "target": "end", "label": ""},
    ]
    return nodes, edges


def _legacy_workflow_matches(
    workflow: OntologyWorkflow,
    *,
    event_id: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
) -> bool:
    return (
        workflow.description == WORKFLOW_DESCRIPTION
        and workflow.trigger_type == "event"
        and workflow.trigger_config == {"event_id": event_id}
        and workflow.steps == []
        and workflow.nodes == nodes
        and workflow.edges == edges
        and workflow.status == "draft"
        and workflow.enabled is False
        and workflow.access_scope == "tenant"
    )


def _upsert_function(db, scenario: BusinessScenario) -> FunctionDefinition:
    definitions = list(db.scalars(
        select(FunctionDefinition).where(FunctionDefinition.scenario_id == scenario.id)
    ))
    function = _claim_named_resource(
        definitions,
        name=FUNCTION_SPEC["name"],
        label="函数定义",
        legacy_matches=_legacy_function_matches,
    )
    normalized = function_definition_service.normalize_definition({
        **FUNCTION_SPEC,
        "description": _marked_description(FUNCTION_SPEC["description"]),
    })
    if function is None:
        function = FunctionDefinition(scenario_id=scenario.id, **normalized)
        db.add(function)
    else:
        for field, value in normalized.items():
            setattr(function, field, value)
    db.flush()
    return function


def _upsert_operations(
    db,
    scenario: BusinessScenario,
    entities: dict[str, OntologyEntity],
) -> tuple[OntologyAction, OntologyRule, OntologyEvent, OntologyWorkflow]:
    action_input_schema = function_definition_service.normalize_schema(
        ACTION_INPUT_SCHEMA,
        label="操作输入契约",
    )
    actions = list(db.scalars(
        select(OntologyAction).where(OntologyAction.scenario_id == scenario.id)
    ))
    action = _claim_named_resource(
        actions,
        name=ACTION_NAME,
        label="操作定义",
        legacy_matches=lambda value: _legacy_action_matches(
            value,
            entity_id=entities["欠薪预警"].id,
            input_schema=action_input_schema,
        ),
    )
    if action is None:
        action = OntologyAction(
            scenario_id=scenario.id,
            entity_id=entities["欠薪预警"].id,
            name=ACTION_NAME,
        )
        db.add(action)
    action.entity_id = entities["欠薪预警"].id
    action.description = _marked_description(ACTION_DESCRIPTION)
    action.input_schema = action_input_schema
    action.executor_type = "unbound"
    action.executor_config = {}
    action.precondition = ACTION_PRECONDITION
    action.postcondition = ACTION_POSTCONDITION
    action.enabled = False
    action.requires_confirmation = True
    action.idempotency_required = True
    action.permission_scope = "scenario"
    action.access_scope = "tenant"
    db.flush()

    rules = list(db.scalars(
        select(OntologyRule).where(OntologyRule.scenario_id == scenario.id)
    ))
    rule = _claim_named_resource(
        rules,
        name=RULE_NAME,
        label="规则定义",
        legacy_matches=lambda value: _legacy_rule_matches(
            value,
            entity_id=entities["工资台账"].id,
            action_id=action.id,
        ),
    )
    if rule is None:
        rule = OntologyRule(scenario_id=scenario.id, name=RULE_NAME)
        db.add(rule)
    rule.entity_id = entities["工资台账"].id
    rule.description = _marked_description(RULE_DESCRIPTION)
    rule.condition = copy.deepcopy(RULE_CONDITION)
    rule.action_on_match = ACTION_NAME
    rule.trigger_action_ids = [action.id]
    rule.severity = "critical"
    rule.enabled = False
    db.flush()

    event_payload_schema = function_definition_service.normalize_schema(
        EVENT_PAYLOAD_SCHEMA,
        label="事件载荷契约",
    )
    events = list(db.scalars(
        select(OntologyEvent).where(OntologyEvent.scenario_id == scenario.id)
    ))
    event = _claim_named_resource(
        events,
        name=EVENT_NAME,
        label="事件定义",
        legacy_matches=lambda value: _legacy_event_matches(
            value,
            payload_schema=event_payload_schema,
        ),
    )
    if event is None:
        event = OntologyEvent(scenario_id=scenario.id, name=EVENT_NAME)
        db.add(event)
    event.description = _marked_description(EVENT_DESCRIPTION)
    event.payload_schema = event_payload_schema
    event.trigger_source = EVENT_TRIGGER_SOURCE
    event.enabled = False
    db.flush()

    nodes, edges = _workflow_graph(action.id)
    workflow_service.validate_workflow_definition(nodes, edges)
    workflow_service.validate_workflow_references(
        db,
        scenario.id,
        steps=[],
        nodes=nodes,
    )
    operations_service.validate_approval_nodes(nodes, [])
    trigger_config = {"event_id": event.id}
    operations_service.validate_trigger_config("event", trigger_config)

    workflows = list(db.scalars(
        select(OntologyWorkflow).where(OntologyWorkflow.scenario_id == scenario.id)
    ))
    workflow = _claim_named_resource(
        workflows,
        name=WORKFLOW_NAME,
        label="工作流定义",
        legacy_matches=lambda value: _legacy_workflow_matches(
            value,
            event_id=event.id,
            nodes=nodes,
            edges=edges,
        ),
    )
    if workflow is None:
        workflow = OntologyWorkflow(scenario_id=scenario.id, name=WORKFLOW_NAME)
        db.add(workflow)
    workflow.description = _marked_description(WORKFLOW_DESCRIPTION)
    workflow.trigger_type = "event"
    workflow.trigger_config = trigger_config
    workflow.steps = []
    workflow.nodes = nodes
    workflow.edges = edges
    workflow.status = "draft"
    workflow.enabled = False
    workflow.access_scope = "tenant"
    db.flush()
    return action, rule, event, workflow


def upgrade(db, *, scenario_id: str | None = None) -> dict[str, Any]:
    """Apply the additive contract upgrade in a dedicated database session."""
    try:
        scenario = _find_scenario(db, scenario_id)
        entities = _upsert_entities(db, scenario)
        relations = _upsert_relations(db, scenario, entities)
        instances = _upsert_example_instances(db, scenario, entities)
        function = _upsert_function(db, scenario)
        action, rule, event, workflow = _upsert_operations(db, scenario, entities)
        property_count = sum(len(spec["properties"]) for spec in ENTITY_SPECS)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {
        "scenario_id": scenario.id,
        "entities": len(entities),
        "properties": property_count,
        "relations": len(relations),
        "example_instances": len(instances),
        "function_id": function.id,
        "action_id": action.id,
        "rule_id": rule.id,
        "event_id": event.id,
        "workflow_id": workflow.id,
    }


def main() -> None:
    from app.config import get_settings

    settings = get_settings()
    if not settings.uses_sqlite_database or settings.minio_configured:
        raise RuntimeError(
            "upgrade_wage_warning 已在仅保留医保审计和代理记账的远端部署中封存；"
            "只能用于隔离 SQLite fixture"
        )
    init_db()
    db = SessionLocal()
    try:
        result = upgrade(db)
        print("欠薪预警本体 contract 恢复完成")
        print(f"场景: {result['scenario_id']}")
        print(
            "正式模型: "
            f"{result['entities']} 实体 / {result['properties']} 属性 / "
            f"{result['relations']} 关系 / {result['example_instances']} 示例实例"
        )
        print("未创建物理 DataMapping、结构化数据源或 Agent")
    finally:
        db.close()


if __name__ == "__main__":
    main()
