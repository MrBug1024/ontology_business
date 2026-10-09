"""Preserve explicit handoff relations through compilation and candidate edits."""
from __future__ import annotations

import copy

from .construction_source_requirements import entity_requirements, structured_items
from .ontology_service import effective_relation_cardinality_limits


_CARDINALITY_TYPES = {'one_to_one': '1:1', 'one_to_many': '1:N', 'many_to_many': 'N:M'}


def relation_requirement_issues(requirements: list, relation: dict) -> list[dict]:
    limits = effective_relation_cardinality_limits(relation.get('relation_type'), relation.get('constraints'))
    issues = []
    for requirement in requirements:
        direct = (relation.get('source') == requirement['source']
                  and relation.get('target') == requirement['target'])
        reverse = (relation.get('source') == requirement['target']
                   and relation.get('target') == requirement['source'])
        expected = effective_relation_cardinality_limits(requirement['relation_type'], {})
        if reverse and not direct:
            expected = {f'{side}_{bound}': expected[f'{other}_{bound}']
                        for side, other in (('source', 'target'), ('target', 'source'))
                        for bound in ('min', 'max')}
        if not (direct or reverse) or limits != expected:
            issues.append({'code': 'source_relation_contract_mismatch', 'blocking': True,
                'message': f"关系“{relation.get('name') or relation.get('key')}”未落实交接的端点与基数要求",
                'source_refs': requirement.get('source_refs', []),
                'affected_change_keys': [relation['key']],
                'resolution_hint': '按原交接修正关系方向及有效基数；不要让专家重填已提供的事实。'})
    return issues


def validate_relation_requirements(raw: dict, entities: list[dict], relations: list[dict],
                                   source: dict, *, task_scope: str = '') -> list[dict]:
    if task_scope not in {'', 'ontology'}:
        return []
    expected_entities = entity_requirements(source.get('paragraphs', []))
    normalized_entities = {item['key']: item for item in entities}
    endpoints = {}
    for candidate in raw.get('entities', []):
        entity = normalized_entities.get(candidate.get('key'))
        if entity is None:
            continue
        identity = ({'kind': 'existing', 'id': entity['existing_id']} if entity.get('existing_id')
                    else {'kind': 'generated', 'key': entity['key']})
        for binding in candidate.get('source_entity_bindings') or []:
            for key, requirement in expected_entities.items():
                if binding.get('source_key') == key[1] and binding.get('source_ref') in requirement['source_refs']:
                    endpoints.setdefault(key, []).append(identity)
    issues = []
    for document_id, document in structured_items(source.get('paragraphs', []), 'relations').items():
        for requirement in document['items']:
            relation_type = _CARDINALITY_TYPES.get(requirement.get('cardinality'))
            if relation_type is None:
                continue
            source_refs = endpoints.get((document_id, requirement.get('source')), [])
            target_refs = endpoints.get((document_id, requirement.get('target')), [])
            if len(source_refs) != 1 or len(target_refs) != 1:
                continue  # Missing/ambiguous entity coverage has its own blocker.
            source_ref, target_ref = source_refs[0], target_refs[0]
            matches = [r for r in relations if (r['source'], r['target']) in (
                (source_ref, target_ref), (target_ref, source_ref))]
            if len(matches) != 1:
                issues.append({'code': 'missing_source_relation', 'blocking': True,
                    'message': '交接中的关系尚未唯一落实到定义', 'source_refs': document['source_refs'],
                    'affected_change_keys': [r['key'] for r in matches],
                    'resolution_hint': '完整建模该关系并明确来源端点，不能仅标记段落已覆盖。'})
                continue
            relation = matches[0]
            saved = {'source': copy.deepcopy(source_ref), 'target': copy.deepcopy(target_ref),
                     'relation_type': relation_type, 'source_refs': document['source_refs']}
            relation.setdefault('_construction_relation_requirements', []).append(saved)
            issues.extend(relation_requirement_issues([saved], relation))
    return issues
