from types import SimpleNamespace

import pytest

from app.services import workflow_ontology_contract as contracts
from app.services.policies import PolicyViolation, validate_workflow_graph


def workflow():
    return SimpleNamespace(nodes=[{'id': 'start', 'type': 'start'}, {'id': 'rule', 'type': 'rule'},
        {'id': 'yes', 'type': 'end'}, {'id': 'no', 'type': 'end'}], edges=[
            {'source': 'start', 'target': 'rule'}, {'source': 'rule', 'target': 'yes', 'label': 'true'},
            {'source': 'rule', 'target': 'no', 'label': 'false'}], trigger_config={
                'ontology_contract': {'version': 1, 'output_node_ids': ['yes', 'no'],
                    'output_schema': {'type': 'object', 'properties': {'route': {'type': 'string'}},
                        'required': ['route'], 'additionalProperties': False}}})


@pytest.mark.parametrize('node', ['yes', 'no'])
def test_each_declared_branch_validates_and_completes_the_same_contract(node):
    flow = workflow()
    contracts.validate_declaration(flow, SimpleNamespace(entities={}))
    assert contracts.validate_output(flow, node, {'route': 'valid'})
    with pytest.raises(PolicyViolation, match='不符合已声明契约'):
        contracts.validate_output(flow, node, {'route': 1})
    contracts.require_output_completion(flow, [{'node': node, 'status': 'success', 'contract_validation': 'passed'}])


def test_branch_graph_requires_explicit_complete_output_set():
    flow = workflow()
    validate_workflow_graph(flow.nodes, flow.edges, output_node_ids=['yes', 'no'])
    for outputs in ([], ['yes'], ['yes', 'no', 'unknown']):
        with pytest.raises(PolicyViolation):
            validate_workflow_graph(flow.nodes, flow.edges, output_node_ids=outputs)


def test_contract_cannot_mix_singular_and_plural_or_omit_a_branch():
    flow = workflow()
    flow.trigger_config['ontology_contract']['output_node_id'] = 'yes'
    with pytest.raises(PolicyViolation):
        contracts.validate_declaration(flow, SimpleNamespace(entities={}))
    flow.trigger_config['ontology_contract'].pop('output_node_id')
    flow.trigger_config['ontology_contract']['output_node_ids'] = ['yes']
    with pytest.raises(PolicyViolation):
        contracts.validate_declaration(flow, SimpleNamespace(entities={}))


def test_missing_business_output_cannot_complete_a_branch_workflow():
    with pytest.raises(PolicyViolation, match='尚未产生'):
        contracts.require_output_completion(workflow(), [{'node': 'yes', 'status': 'success'}])
