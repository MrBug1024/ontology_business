from types import SimpleNamespace

import pytest

from app.services import candidate_governance_service as governance


@pytest.mark.parametrize('raw,expected', [('medium', 'warning'), ('critical', 'critical'), (None, 'info')])
def test_candidate_rule_formalization_uses_supported_severity(raw, expected):
    row = SimpleNamespace(id='candidate', revision=0, resource_kind='rule', resource_key='rule.check',
        payload={'key': 'rule.check', 'name': 'Check', 'severity': raw,
                 'condition': {'op': '==', 'field': 'complete', 'value': True}})
    item, _warnings = governance._canonical_item(row,
        reference_indexes=governance._ReferenceIndexes(selected={}, existing={}))
    assert item['severity'] == expected
    assert row.payload['severity'] == raw
    assert item['enabled'] is False


def test_candidate_rule_formalization_rejects_unknown_severity():
    row = SimpleNamespace(id='candidate', revision=0, resource_kind='rule', resource_key='rule.check',
        payload={'key': 'rule.check', 'name': 'Check', 'severity': 'unsupported'})
    with pytest.raises(ValueError):
        governance._canonical_item(row,
            reference_indexes=governance._ReferenceIndexes(selected={}, existing={}))
