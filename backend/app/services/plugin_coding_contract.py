"""Resolve full, immutable authoring schemas through the existing capability catalog."""
from __future__ import annotations

from . import capability_application_service, permission_service, release_service
from .plugin_coding_validation import safe_text
from .capability_contracts import canonical_json
from .plugin_client_contract import client_contract
from .plugin_host_profile import delivery_profile
from .scenario_capability_blueprint import blueprint


def authoring_contract(db, manifest: dict, *, blueprint_selection: list[dict] | None = None) -> dict:
    scenario, _ = release_service._scenario_for_manage(db, manifest['scenario']['id'])
    deployment, _ = capability_application_service.resolve_deployment(db, scenario,
        release_id=manifest['deployment']['release_id'])
    identity = manifest['deployment']
    if (not deployment.snapshot_id or deployment.release_id != identity['release_id']
            or deployment.definition_hash != identity['definition_hash']
            or identity.get('snapshot_id') is not None and deployment.snapshot_id != identity['snapshot_id']):
        raise ValueError('编码场景目标与发布身份不一致，请重新验证发布')
    available = capability_application_service.list_capabilities(db, scenario,
        release_id=identity['release_id'], definition=deployment.definition)
    indexed = {(item['kind'], item['key']): item for item in available}
    capabilities = []
    for selected in manifest['capabilities']:
        item = indexed.get((selected['kind'], selected['key']))
        if item is None or item['definition_hash'] != manifest['deployment']['definition_hash']:
            raise ValueError('编码契约缺失或发布身份不一致，请重新验证发布')
        capabilities.append({key: item[key] for key in (
            'kind', 'key', 'name', 'description', 'definition_hash', 'input_schema', 'output_schema',
            'side_effect', 'requires_confirmation', 'idempotency_required', 'data_ports')})
        capabilities[-1]['readiness'] = item.get('readiness', {'ready': False, 'issues': []})
    # Only frozen semantic text enters authoring context. Live draft descriptions
    # and modeling documents must not redefine the goal of an existing release.
    snapshot = release_service._snapshot_for_scenario(db, scenario, deployment.snapshot_id)
    frozen = snapshot.content['scenario']
    safe_scenario = release_service.safe_snapshot_content({
        'name': frozen['name'], 'description': frozen.get('description') or '',
    })
    scenario_context = {'id': manifest['scenario']['id'], **{
        key: value if isinstance(value, str) else '' for key, value in safe_scenario.items()
    }}
    definition = deployment.definition
    readable_properties = {prop.id for entity in definition.entities.values() for prop in entity.properties
                           if permission_service.can_read_property(db, prop)}
    invocation_authorized = {(kind, key) for kind, key in indexed
        if capability_application_service._permission_allowed(db, definition, kind,
            getattr(definition, {'function': 'functions', 'action': 'actions', 'rule': 'rules', 'workflow': 'workflows'}[kind])[key],
            'execute')}
    scenario_blueprint = blueprint(definition, scenario=scenario_context,
        deployment={'release_id': deployment.release_id, 'snapshot_id': deployment.snapshot_id,
                    'definition_hash': deployment.definition_hash},
        available=available, selected=manifest['capabilities'] if blueprint_selection is None else blueprint_selection,
        readable_property_keys=readable_properties,
        invocation_authorized=invocation_authorized)
    result = {**manifest, 'scenario': scenario_context, 'capabilities': capabilities,
              'client_contract': client_contract(), 'scenario_blueprint': scenario_blueprint,
              'delivery_profile': delivery_profile(manifest.get('host', 'claude_code'))}
    encoded = canonical_json(result)
    if len(encoded.encode('utf-8')) > 128 * 1024:
        raise ValueError('完整契约超过单次编码预算，请缩小本插件能力范围')
    safe_text(encoded)
    return result
