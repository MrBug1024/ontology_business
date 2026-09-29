import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

from app.services import scenario_model_compiler as compiler
from app.services import distillation_model_coverage as coverage


def inputs():
    content = json.dumps({'entities': [{'key': 'source_record', 'name': 'Source record',
        'attributes': ['Identifier (unique)', 'Amount', 'Occurred at']}], 'decision': 'continue'})
    document = {'id': 'handoff', 'filename': 'Governed contract', 'parsed_text': content,
        'content_hash': hashlib.sha256(content.encode()).hexdigest(), 'status': 'parsed',
        'usage_plane': 'modeling_material'}
    source = compiler.build_source_bundle('', [], complete_handoffs=[document])
    ref = next(ref for ref, key in coverage.source_entities(source['paragraphs']))
    raw = {key: [] for key in (*compiler._MODEL_OUTPUT_RESOURCE_SECTIONS, 'coverage', 'unresolved')}
    raw['schema_version'] = compiler.SCHEMA_VERSION
    raw['entities'] = [{'key': 'record', 'name': 'Record', 'evidence_refs': [ref], 'confidence': 1,
        'properties': [{'name': 'id', 'data_type': 'string', 'is_key': True, 'is_title': True}]}]
    raw['coverage'] = [{'source_ref': p['ref'], 'status': 'modeled' if p['ref'] == ref else 'context',
                       'reason': 'Entity definition or handoff decision',
                       'change_keys': ['record'] if p['ref'] == ref else []} for p in source['paragraphs']]
    scenario = SimpleNamespace(id='synthetic', namespace='test', entities=[], relations=[],
        function_definitions=[], actions=[], rules=[], events=[], workflows=[], data_mappings=[], relation_data_mappings=[])
    return raw, source, scenario, ref


def normalize(raw, source, scenario):
    return compiler.normalize_scenario_model(None, scenario, raw, source_bundle=source)


def test_source_citation_alone_cannot_hide_missing_entity_attributes():
    raw, source, scenario, ref = inputs()
    initial = normalize(raw, source, scenario)
    assert any(i['code'] == 'incomplete_source_attributes' and i['blocking'] for i in initial['unresolved'])
    raw['entities'][0]['source_entity_bindings'] = [{'source_ref': ref, 'source_key': 'source_record',
        'attribute_map': {'Identifier (unique)': 'id'}}]
    result = normalize(raw, source, scenario)
    issue = next(i for i in result['unresolved'] if i['code'] == 'incomplete_source_attributes')
    assert 'Amount' in issue['message'] and 'Occurred at' in issue['message']


def test_complete_explicit_source_mapping_passes_without_guessing_names():
    raw, source, scenario, ref = inputs()
    raw['entities'][0]['properties'] += [{'name': 'amount', 'data_type': 'number'},
                                       {'name': 'when', 'data_type': 'datetime'}]
    raw['entities'][0]['source_entity_bindings'] = [{'source_ref': ref, 'source_key': 'source_record',
        'attribute_map': {'Identifier (unique)': 'id', 'Amount': 'amount', 'Occurred at': 'when'}}]
    assert normalize(raw, source, scenario)['unresolved'] == []
    for replacement in ({'Identifier (unique)': 'id', 'Amount': 'absent', 'Occurred at': 'when'},
                        {'Identifier (unique)': 'id', 'Amount': 'id', 'Occurred at': 'when'}):
        changed = deepcopy(raw)
        changed['entities'][0]['source_entity_bindings'][0]['attribute_map'] = replacement
        assert any(i['code'] == 'incomplete_source_attributes' for i in normalize(changed, source, scenario)['unresolved'])


def test_foreign_source_binding_is_rejected_and_plain_text_is_not_reinterpreted():
    raw, source, scenario, ref = inputs()
    raw['entities'][0]['source_entity_bindings'] = [{'source_ref': 'foreign:p1',
        'source_key': 'source_record', 'attribute_map': {}}]
    assert any(i['code'] == 'incomplete_source_attributes' for i in normalize(raw, source, scenario)['unresolved'])
    for paragraph in source['paragraphs']:
        paragraph.pop('structured_handoff', None)
    assert not any(i['code'] == 'incomplete_source_attributes' for i in normalize(raw, source, scenario)['unresolved'])


def test_chunk_merge_preserves_distinct_entity_source_bindings():
    first = {'key': 'record', 'name': 'Record', 'properties': [],
             'source_entity_bindings': [{'source_ref': 'first:p1', 'source_key': 'one',
                                         'attribute_map': {'Identifier': 'id'}}]}
    second = {**deepcopy(first), 'source_entity_bindings': [{'source_ref': 'second:p1',
              'source_key': 'two', 'attribute_map': {'Amount': 'amount'}}]}
    issues = []
    compiler._merge_resource_fragment(first, second, section='entities', unresolved=issues)
    assert issues == []
    assert len(first['source_entity_bindings']) == 2


def test_malformed_fragment_bindings_remain_invalid_without_crashing_the_merge():
    target = {'source_entity_bindings': 3}
    incoming = {'source_entity_bindings': [{'source_ref': 'doc:p1', 'source_key': 'record', 'attribute_map': {}}]}
    coverage.align_binding_fragments(target, incoming)
    assert target['source_entity_bindings'] == 3


def test_large_entity_fragments_keep_coverage_for_every_original_attribute():
    attributes = [f'Field {i:03}: source attribute with a precise meaning' for i in range(210)]
    content = json.dumps({'entities': [{'key': 'record', 'name': 'Record', 'attributes': attributes}]})
    document = {'id': 'handoff', 'filename': 'Large governed contract', 'parsed_text': content,
        'content_hash': hashlib.sha256(content.encode()).hexdigest(), 'status': 'parsed',
        'usage_plane': 'modeling_material'}
    source = compiler.build_source_bundle('', [], complete_handoffs=[document])
    expected = coverage.source_entities(source['paragraphs'])
    assert len(expected) > 1
    assert all(key == 'record' and values == attributes for (_, key), values in expected.items())
    ref = next(iter(expected))[0]
    candidate = {'key': 'candidate', 'name': 'Record', 'properties': [{'name': 'id'}], 'evidence_refs': [ref]}
    issues = coverage.validate_entity_coverage({'entities': [candidate]}, [candidate], source, {})
    assert issues and issues[0]['code'] == 'incomplete_source_attributes'
