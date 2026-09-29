from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.services import provider_definition_service as providers
from app.services import scenario_model_compiler as compiler


def declaration():
    manifest = next(row for row in providers.list_function_provider_manifests()
                    if row['provider_key'] == 'builtin.semantic-dataset-query')
    return {'name': 'Managed query', 'description': 'Query the explicitly supplied data',
        'tags': [], 'visibility': 'scenario', 'runtime_kind': 'provider',
        'schema_source': 'provider_manifest', 'runtime_config': {
            'provider_key': manifest['provider_key'], 'provider_version': manifest['provider_version'],
            'provider_config': {'semantic_mapping_ids': ['a' * 32]}}}, manifest


def test_fixed_schema_is_resolved_by_exact_provider_identity_and_survives_review():
    from app.services import candidate_identity_projection as projection
    from app.services import candidate_governance_service as governance
    raw, manifest = declaration()
    normalized = compiler._function_definition(raw)
    assert normalized['input_schema'] == manifest['input_schema']
    assert normalized['output_schema'] == manifest['output_schema']
    assert 'schema_source' not in normalized
    projected = projection.project_identity('functions', raw, normalized)
    assert projected['input_schema'] == manifest['input_schema']
    row = SimpleNamespace(id='candidate', revision=0, resource_kind='function', resource_key='query', display_name='Managed query',
                          payload=projected, evidence_refs=[], confidence=1)
    item, _ = governance._canonical_item(row, reference_indexes={})
    assert compiler._function_definition(item) == normalized
    assert 'input_schema' not in raw
    projected['input_schema']['properties'].clear()
    assert normalized['input_schema']['properties']


@pytest.mark.parametrize('change', [
    {'schema_source': 'latest'},
    {'runtime_kind': 'contract'},
    {'input_schema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'output_schema': None},
    {'runtime_config': {'provider_key': 'builtin.semantic-dataset-query', 'provider_version': 'missing',
                        'provider_config': {'semantic_mapping_ids': ['a' * 32]}}},
])
def test_manifest_authoring_rejects_conflicts_and_unknown_versions(change):
    raw, manifest = declaration()
    raw.update(input_schema=manifest['input_schema'], output_schema=manifest['output_schema'])
    raw.update(change)
    with pytest.raises(ValueError):
        compiler._function_definition(raw)


def test_manifest_authoring_rejects_editable_schema(monkeypatch):
    raw, manifest = declaration()
    editable = deepcopy(manifest)
    editable['input_schema_mode'] = 'editable'
    monkeypatch.setattr(providers, 'list_function_provider_manifests', lambda: [editable])
    with pytest.raises(ValueError):
        compiler._function_definition(raw)


def test_explicit_schema_contract_is_not_replaced_by_manifest():
    raw, manifest = declaration()
    raw.pop('schema_source')
    raw['input_schema'] = manifest['input_schema']
    raw['output_schema'] = manifest['output_schema']
    normalized = compiler._function_definition(raw)
    assert normalized['input_schema'] == raw['input_schema']


def test_compiler_materializes_fixed_schemas_before_closed_schema_validation():
    raw, manifest = declaration()
    raw.update(key='query', evidence_refs=['request:p1'], confidence=1)
    scene = SimpleNamespace(id='synthetic', namespace='test', entities=[], relations=[],
        function_definitions=[], actions=[], rules=[], events=[], workflows=[], data_mappings=[], relation_data_mappings=[])
    model = {key: [] for key in (*compiler._MODEL_OUTPUT_RESOURCE_SECTIONS, 'coverage', 'unresolved')}
    model.update(schema_version=compiler.SCHEMA_VERSION, functions=[raw], coverage=[{
        'source_ref': 'request:p1', 'status': 'modeled', 'reason': 'Explicit managed query', 'change_keys': ['query']}])
    result = compiler.normalize_scenario_model(None, scene, model, source_bundle={
        'paragraphs': [{'ref': 'request:p1', 'text': 'Build the managed read-only query with its fixed Provider contract.'}],
        'documents': [], 'fingerprint': 'a' * 64})
    assert result['unresolved'] == []
    assert result['functions'][0]['input_schema'] == manifest['input_schema']
    assert result['draft_candidates'][0]['payload']['output_schema'] == manifest['output_schema']
