"""Carry server-resolved identities into editable compiler candidates."""
from __future__ import annotations

import copy
from typing import Any

from .ontology_service import _RELATION_CONSTRAINT_KEYS


def optional_relation_constraints(value: Any) -> Any:
    """Compiler null means unspecified for declared optional constraints only."""
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    return {key: copy.deepcopy(item) for key, item in value.items()
            if item is not None or key not in _RELATION_CONSTRAINT_KEYS}


def project_identity(section: str, raw: dict[str, Any],
                     normalized: dict[str, Any] | None) -> dict[str, Any]:
    """Preserve invalid authoring values while retaining resolved references.

    This accepts a peer from the same compiler result, never looks up resources
    by display name, and never changes validation or promotion eligibility.
    """
    result = copy.deepcopy(raw)
    result.pop('_construction_requirements', None)
    result.pop('_construction_relation_requirements', None)
    if normalized is not None and section == 'functions' and raw.get('schema_source') == 'provider_manifest':
        for field in ('input_schema', 'output_schema'):
            result[field] = copy.deepcopy(normalized[field])
    if normalized is not None and section == 'rules':
        for field in ('entity', 'trigger_actions'):
            if field in normalized:
                result[field] = copy.deepcopy(normalized[field])
    if normalized is not None and section == 'workflows':
        peers = normalized.get('nodes') or []
        for node in result.get('nodes') or []:
            if not isinstance(node, dict) or not isinstance(node.get('data', {}), dict):
                continue
            matches = [peer for peer in peers if peer.get('id') == node.get('id')
                       and peer.get('type') == node.get('type')]
            if len(matches) == 1 and 'resource' in matches[0].get('data', {}):
                node.setdefault('data', {})['resource'] = copy.deepcopy(matches[0]['data']['resource'])
        if 'trigger_event' in normalized:
            result['trigger_event'] = copy.deepcopy(normalized['trigger_event'])
    if normalized is None or section not in {'entities', 'relations'}:
        return result
    fields = ['existing_id', 'operation', 'api_name']
    if section == 'relations':
        fields.extend(['source', 'target', 'inverse_relation'])
        if '_construction_relation_requirements' in normalized:
            result['_construction_relation_requirements'] = copy.deepcopy(normalized['_construction_relation_requirements'])
        # Carry only lossless syntax normalization; malformed authoring values
        # remain editable and must continue to fail formal validation.
        from .scenario_model_compiler import normalize_relation_type, _normalize_compiler_relation_constraints
        relation_type = normalize_relation_type(raw.get('relation_type'))
        if relation_type:
            try:
                constraints, _inverse = _normalize_compiler_relation_constraints(
                    raw.get('constraints'), relation_type=relation_type)
            except ValueError:
                pass
            else:
                result['relation_type'] = relation_type
                result['constraints'] = constraints
    for field in fields:
        if field in normalized:
            result[field] = copy.deepcopy(normalized[field])
    if section == 'entities':
        if '_construction_requirements' in normalized:
            result['_construction_requirements'] = copy.deepcopy(normalized['_construction_requirements'])
        peers = normalized.get('properties') or []
        for prop in result.get('properties') or []:
            if not isinstance(prop, dict):
                continue
            matches = [peer for peer in peers if isinstance(peer, dict)
                       and peer.get('name') == prop.get('name')]
            if len(matches) != 1:
                continue
            for field in ('api_name', '_operation'):
                if field in matches[0]:
                    prop[field] = copy.deepcopy(matches[0][field])
    return result
