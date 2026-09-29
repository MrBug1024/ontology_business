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
    if normalized is not None and section == 'functions' and raw.get('schema_source') == 'provider_manifest':
        for field in ('input_schema', 'output_schema'):
            result[field] = copy.deepcopy(normalized[field])
    if normalized is None or section not in {'entities', 'relations'}:
        return result
    fields = ['existing_id', 'operation', 'api_name']
    if section == 'relations':
        fields.extend(['source', 'target', 'inverse_relation'])
    for field in fields:
        if field in normalized:
            result[field] = copy.deepcopy(normalized[field])
    if section == 'entities':
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
