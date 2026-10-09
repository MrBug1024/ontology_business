import json
from types import SimpleNamespace

import pytest

from app.services import workflow_ontology_contract as contract
from app.services.workflow_service import evaluate_condition, render_template
from app.services.policies import PolicyViolation
from app.services.ontology_rule_contract import validate_record


def fixture():
    props = [SimpleNamespace(name=name, api_name='public_' + name, data_type='string',
        is_required=True, is_key=name == 'request_id', is_enum=False, enum_values=[], constraints={})
        for name in ['request_id', 'priority', 'applicant_id']]
    entity = SimpleNamespace(id='entity', properties=props, lifecycle_status='active')
    definition = SimpleNamespace(entities={'entity': entity})
    workflow = SimpleNamespace(trigger_config={'ontology_contract': {'entity_ids':['entity'],
        'input_bindings':[{'path':'record','entity_id':'entity','partial':True}]}})
    return workflow, definition, entity


def test_published_input_names_work_in_authored_rules_and_templates():
    workflow, definition, _ = fixture()
    original = {'record': {'public_request_id':'REQ', 'public_priority':'high'}}
    params = contract.runtime_params(workflow, definition, original)
    record = render_template('{{params.record}}', {'params': params})
    assert evaluate_condition({'field':'priority','op':'==','value':'high'}, record)
    assert render_template('{{params.record.request_id}}', {'params': params}) == 'REQ'
    assert json.loads(json.dumps(params)) == original
    assert list(params['record']) == ['public_request_id','public_priority']


def test_alias_views_do_not_fill_missing_business_fields():
    workflow, definition, entity = fixture()
    params = contract.runtime_params(workflow, definition, {'record':{'public_priority':'normal'}})
    record = params['record']
    assert 'applicant_id' not in record
    assert record.get('applicant_id') is None
    with pytest.raises(PolicyViolation, match='applicant_id'):
        validate_record(SimpleNamespace(input_validation='object'), record, entity, ['priority','applicant_id'])


def test_conflicting_alias_values_are_rejected():
    workflow, definition, _ = fixture()
    with pytest.raises(PolicyViolation, match='冲突'):
        contract.runtime_params(workflow, definition, {'record': {'priority':'low','public_priority':'high'}})


def test_only_explicit_bindings_get_alias_views():
    workflow, definition, _ = fixture()
    original = {'record': {'public_priority':'high'}, 'unbound': {'public_priority':'low'}}
    params = contract.runtime_params(workflow, definition, original)
    assert params['unbound'].get('priority') is None
    assert original['record'].get('priority') is None
