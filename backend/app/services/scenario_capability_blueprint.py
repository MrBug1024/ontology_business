"""Project frozen business meaning without exporting algorithms or runtime data."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

from pydantic import ValidationError

from ..scenario_capability_blueprint_schemas import ScenarioCapabilityBlueprintOut
from . import release_service, workflow_ontology_contract
from .capability_contracts import canonical_json


VERSION = 'scenario-capability-blueprint.v1'
MAX_BLUEPRINT_BYTES = 128 * 1024
GROUPS = {'function': 'functions', 'action': 'actions', 'rule': 'rules',
          'workflow': 'workflows', 'event': 'events'}
ROLES = {
    'function': '以冻结的输入输出契约在平台计算；插件调用平台，不复制计算实现。',
    'rule': '在平台进行业务判定；规则不满足可以是成功调用的业务结果。',
    'action': '在平台执行受治理操作；副作用、确认和幂等由服务端裁决。',
    'workflow': '在平台编排能力与人工交互；已入队不代表业务已完成。',
    'event': '描述业务事件与流程触发语义；事件不是当前插件的直接调用入口。',
}
STAGES = [
    {'key': 'materials', 'label': '建模资料', 'contribution': '帮助理解事实、术语、流程、约束和待澄清问题。',
     'boundary': '资料正文不进入本画像，不自动成为正式调用输入。'},
    {'key': 'distillation', 'label': '业务蒸馏', 'contribution': '形成业务目标、任务、规则、流程、验收和缺口的候选规格。',
     'boundary': '本发布未冻结的蒸馏文档不能作为既有 Release 的来源证明；当前草稿不能改变冻结语义。'},
    {'key': 'ontology', 'label': '本体模型', 'contribution': '提供对象、属性、身份、关系和能力绑定的共同业务语言。',
     'boundary': '对象图和事件本身不是调用工具；纯计算能力可以没有本体对象。'},
    {'key': 'capabilities', 'label': '能力建设', 'contribution': '提供函数、规则、Action、Workflow 和事件的受治理定义。',
     'boundary': '本画像是理解参考；权限、readiness、确认、输入和执行由统一服务端实时裁决。'},
    {'key': 'plugin', 'label': '插件交付', 'contribution': '提供宿主入口、Skill 方法、薄客户端和冻结契约参考。',
     'boundary': '仅封装选中能力；本次 typed input 或受管输入须显式提供，编码扩展不自动变为运行依赖。'},
]


def _groups(definition) -> dict[str, Mapping]:
    result = {}
    for kind, name in GROUPS.items():
        values = getattr(definition, name, None)
        if not isinstance(values, Mapping):
            raise ValueError('冻结能力画像缺少完整定义，请重新读取明确发布')
        result[kind] = values
    return result


def _references(kind: str, resource) -> set[tuple[str, str]]:
    if kind == 'rule':
        return {('action', key) for key in (getattr(resource, 'trigger_action_ids', None) or [])}
    if kind == 'workflow':
        # Reuse the release dependency contract. Values/params/node prompts are
        # never copied to this projection.
        raw = {key: getattr(resource, key, None) for key in ('nodes', 'steps', 'trigger_type', 'trigger_config')}
        return {(ref_kind, key) for ref_kind, keys in release_service._workflow_reference_ids(raw).items()
                for key in keys}
    return set()


def _refs(values) -> list[dict]:
    return [{'kind': kind, 'key': key} for kind, key in sorted(values)]


def _semantic(kind: str, resource, visible: set[tuple[str, str]]) -> dict:
    entity_key = getattr(resource, 'entity_id', None)
    object_keys = {entity_key} if entity_key else set()
    nodes = (getattr(resource, 'nodes', None) or getattr(resource, 'steps', None) or []) if kind == 'workflow' else []
    counts = Counter(node.get('type', 'unknown') for node in nodes if isinstance(node, Mapping))
    bindings, outputs = [], []
    if kind == 'workflow':
        contract = workflow_ontology_contract.contract_for(resource)
        if contract:
            object_keys.update(contract.entity_ids)
            object_keys.update(binding.entity_id for binding in contract.input_bindings)
            bindings = [{'path': binding.path, 'object_key': binding.entity_id,
                         'many': binding.many, 'partial': binding.partial} for binding in contract.input_bindings]
            outputs = workflow_ontology_contract.output_nodes(contract)
    return {'role': ROLES[kind], 'runtime_kind': getattr(resource, 'runtime_kind', None) if kind == 'function' else None,
            'trigger_type': getattr(resource, 'trigger_type', 'manual') if kind == 'workflow' else None,
            'node_counts': [{'kind': key, 'count': count} for key, count in sorted(counts.items())],
            'requires_approval': bool(counts.get('approval')),
            'object_keys': sorted(object_keys), 'dependencies': _refs(_references(kind, resource) & visible),
            'input_bindings': bindings, 'output_node_keys': outputs}


def _ontology(definition, readable_property_keys: set[str]) -> dict:
    entities = getattr(definition, 'entities', None)
    relations = getattr(definition, 'relations', None)
    if not isinstance(entities, Mapping) or not isinstance(relations, Mapping):
        raise ValueError('冻结能力画像缺少完整本体定义')
    objects = []
    for key, entity in sorted(entities.items()):
        properties = []
        for prop in sorted(entity.properties, key=lambda item: item.id):
            if prop.id not in readable_property_keys:
                continue
            properties.append({'key': prop.id, **{name: getattr(prop, name, '') or ''
                for name in ('api_name', 'name', 'data_type', 'description')},
                **{name: bool(getattr(prop, name, False)) for name in ('is_key', 'is_required', 'is_title', 'is_enum')},
                'enum_values': list(getattr(prop, 'enum_values', None) or [])})
        objects.append({'key': key, **{name: getattr(entity, name, '') or ''
            for name in ('api_name', 'name', 'description')}, 'properties': properties})
    relation_items = [{'key': key, **{name: getattr(relation, name, '') or ''
        for name in ('api_name', 'name', 'description')},
        'source_object_key': relation.source_entity_id, 'target_object_key': relation.target_entity_id,
        'cardinality': relation.relation_type} for key, relation in sorted(relations.items())]
    return {'objects': objects, 'relations': relation_items}


def blueprint(definition, *, scenario: dict, deployment: dict, available: list[dict],
              selected: list[dict], readable_property_keys: set[str], invocation_authorized: set[tuple[str, str]]) -> dict:
    """One complete authorized semantic view of the exact frozen definition.

    Catalog visibility and current execution ACLs arrive from the application
    service. Internal references never create discoverable invocation entries.
    """
    if getattr(definition, 'source', None) != 'release':
        raise ValueError('能力画像必须来自明确冻结发布')
    if any(getattr(definition, key, None) != deployment[key] for key in ('release_id', 'snapshot_id', 'definition_hash')):
        raise ValueError('冻结能力画像与发布身份不一致')
    groups = _groups(definition)
    catalog = {(item['kind'], item['key']): item for item in available}
    chosen = {(item['kind'], item['key']) for item in selected}
    if not chosen.issubset(catalog):
        raise ValueError('所选能力不属于授权发布画像')
    dependencies = set()
    pending = list(chosen)
    visited = set()
    while pending:
        ref = pending.pop()
        if ref in visited:
            continue
        visited.add(ref)
        kind, key = ref
        resource = groups[kind].get(key)
        if resource is None:
            raise ValueError('冻结画像依赖缺失，请重新验证发布')
        direct = _references(kind, resource)
        dependencies.update(direct)
        pending.extend(direct - visited)
    capabilities = []
    visible = set(catalog) | {('event', key) for key in groups['event']}
    for kind, key in sorted(visible):
        resource = groups[kind].get(key)
        if resource is None:
            raise ValueError('冻结能力目录与画像不一致')
        item = catalog.get((kind, key), {})
        capabilities.append({'kind': kind, 'key': key, 'name': getattr(resource, 'name', ''),
            'description': getattr(resource, 'description', '') or '',
            'selected': (kind, key) in chosen, 'available': (kind, key) in catalog,
            'dependency': (kind, key) in dependencies, 'invocation_supported': kind != 'event',
            'invocation_authorized': kind != 'event' and (kind, key) in invocation_authorized,
            'enabled': bool(getattr(resource, 'enabled', True)),
            'ready': bool(item.get('readiness', {}).get('ready', False)), 'semantic': _semantic(kind, resource, visible)})
    result = release_service.safe_snapshot_content({'version': VERSION, 'completeness': 'complete_authorized_projection',
        'scenario': scenario, 'deployment': {**deployment, 'definition_source': 'release'}, 'stages': STAGES,
        'ontology': _ontology(definition, readable_property_keys), 'capabilities': capabilities,
        'coverage': {'selected': _refs(chosen), 'available': _refs(catalog), 'dependencies': _refs(dependencies & visible),
                     'unselected_available': _refs(set(catalog) - chosen)}})
    if len(canonical_json(result).encode('utf-8')) > MAX_BLUEPRINT_BYTES:
        raise ValueError('完整能力画像超过编码预算，请缩小能力范围或精简定义')
    try:
        return ScenarioCapabilityBlueprintOut.model_validate(result).model_dump(mode='json')
    except ValidationError:
        raise ValueError('冻结能力画像不符合完整语义契约，请检查发布定义') from None
