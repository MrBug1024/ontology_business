"""Check mapping candidate promotion only in a disposable PostgreSQL database."""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import Session

from app.catalog_schemas import DatasetSchemaCreate, LogicalDatasetCreate
from app.models import BusinessScenario, OntologyEntity, OntologyProperty, SemanticMapping
from app.services import candidate_governance_service as governance
from app.services import catalog_service, semantic_mapping_authoring
from app.services import semantic_mapping_authoring_context, semantic_mapping_contract_service
from app.services.policies import PolicyViolation
from scripts.verify_distillation_publication import main


def seed_mapping(db, suffix='', usage_plane='modeling_material'):
    entity = OntologyEntity(scenario_id='scene_a', name='Subject', api_name='subject' + suffix)
    db.add(entity); db.flush()
    properties = []
    for index in range(3):
        prop = OntologyProperty(entity_id=entity.id, api_name=f'field_{index}', name=f'Field {index}',
            data_type='string', is_key=index == 0, is_title=index == 0)
        db.add(prop); properties.append(prop)
    dataset = catalog_service.create_dataset(db, LogicalDatasetCreate(
        key='modeling_subject' + suffix, name='Synthetic structure', usage_plane=usage_plane))
    schema = catalog_service.create_schema(db, dataset, DatasetSchemaCreate(relations=[{
        'relation_key': 'subjects', 'display_name': 'Subjects', 'fields': [
            {'field_key': f'field_{index}', 'source_name': f'Field {index}', 'logical_type': 'string'}
            for index in range(3)]}]))
    relation = schema.relations[0]
    db.flush()
    return {'entity_id': entity.id, 'dataset_schema_id': schema.id, 'dataset_relation_id': relation.id,
        'mapping_key': 'subjects' + suffix, 'fields': [
            {'ontology_property_id': prop.id, 'dataset_field_id': field.id, 'is_required': index == 0}
            for index, (prop, field) in enumerate(zip(properties, relation.fields))]}


def candidate(db, scenario, payload, key):
    return governance.create_manual_candidate(db, scenario, tenant_id='tenant_a', created_by_user_id='user_a',
        resource_kind='semantic_mapping', resource_key=key, title='Subject fields', payload=payload)


def verify(engine):
    with Session(engine, expire_on_commit=False) as db:
        db.info.update(tenant_id='tenant_a', user_id='user_a')
        scenario = db.get(BusinessScenario, 'scene_a')
        full = seed_mapping(db)
        db.commit()
        runtime = seed_mapping(db, '_runtime', 'invocation_input')
        invalid_field = deepcopy(full)
        invalid_field['fields'][0]['dataset_field_id'] = runtime['fields'][0]['dataset_field_id']
        invalid_property = deepcopy(full)
        invalid_property['fields'][0]['ontology_property_id'] = runtime['fields'][0]['ontology_property_id']
        for rejected in (runtime, invalid_field, invalid_property, {**full, 'fields': full['fields'][1:]}):
            try:
                semantic_mapping_authoring.validate(db, scenario, rejected)
            except (PolicyViolation, catalog_service.CatalogError) as error:
                if rejected is invalid_field or rejected is invalid_property:
                    assert '第1项' in str(error), str(error)
            else:
                raise AssertionError('Non-modeling source, foreign property/field or missing primary key accepted')
        db.commit()
        first = {**full, 'fields': full['fields'][:1]}
        row = candidate(db, scenario, first, 'semantic_mapping.subjects')
        evaluation = governance.evaluate_candidates(db, scenario, [row])
        assert evaluation.eligible, evaluation.blockers
        _, promoted = governance.promote_candidates(db, scenario, tenant_id='tenant_a', created_by_user_id='user_a',
            expected_revisions={row.id: row.revision})
        mapping_id = promoted['promoted'][0]['formal_resource_id']
        db.commit()
        mapping = db.get(SemanticMapping, mapping_id)
        assert mapping.status == 'active' and len(mapping.field_mappings) == 1
        original_field_id = mapping.field_mappings[0].id
        context = semantic_mapping_authoring_context.catalog(db, scenario)
        assert any(item.get('dataset_schema_id') == full['dataset_schema_id'] for item in context)
        assert all('versions' not in item and 'records' not in item for item in context)
        update = {**full, 'existing_id': mapping.id, 'expected_updated_at': mapping.updated_at.isoformat(),
            'fields': full['fields'][1:]}
        update_row = candidate(db, scenario, update, 'semantic_mapping.subjects_extension')
        # A bad second candidate keeps the whole batch from writing.
        invalid = candidate(db, scenario, {**full, 'mapping_key': 'invalid', 'dataset_relation_id': 'missing'},
            'semantic_mapping.invalid')
        try:
            governance.promote_candidates(db, scenario, tenant_id='tenant_a', created_by_user_id='user_a',
                expected_revisions={item.id: item.revision for item in (update_row, invalid)})
        except governance.CandidatePromotionBlocked:
            pass
        else:
            raise AssertionError('Invalid atomic batch accepted')
        assert len(db.get(SemanticMapping, mapping_id).field_mappings) == 1
        _, result = governance.promote_candidates(db, scenario, tenant_id='tenant_a', created_by_user_id='user_a',
            expected_revisions={update_row.id: update_row.revision})
        assert result['counts']['semantic_mappings_updated'] == 1
        db.commit(); db.expire_all()
        mapping = db.get(SemanticMapping, mapping_id)
        assert len(mapping.field_mappings) == 3
        assert original_field_id in {field.id for field in mapping.field_mappings}
        contracts = semantic_mapping_contract_service.load_live_contracts(db, scenario_id='scene_a', tenant_id='tenant_a')
        assert len(contracts[mapping_id]['fields']) == 3
        for bad in (update, {**full, 'mapping_key': 'duplicate'},
                    {**full, 'entity_id': 'foreign'}, {**full, 'dataset_schema_id': 'foreign'}):
            try:
                semantic_mapping_authoring.validate(db, scenario, bad)
            except (PolicyViolation, catalog_service.CatalogError):
                pass
            else:
                raise AssertionError('Stale, duplicate or foreign mapping accepted')
        db.rollback()
    with Session(engine) as other:
        other.info.update(tenant_id='tenant_b', user_id='user_b')
        try:
            semantic_mapping_authoring.validate(other, other.get(BusinessScenario, 'scene_b'), update)
        except (PolicyViolation, catalog_service.CatalogError):
            pass
        else:
            raise AssertionError('Cross-tenant candidate accepted')
    verify_concurrent_update(engine)
    print('PASS: Catalog candidate creation, atomic promotion, additive update, preserved IDs, portable contract, stale/foreign/duplicate denial')


def verify_concurrent_update(engine):
    with Session(engine) as db:
        db.info.update(tenant_id='tenant_a', user_id='user_a')
        scenario = db.get(BusinessScenario, 'scene_a')
        full = seed_mapping(db, '_race')
        row = semantic_mapping_authoring.apply(db, scenario, {**full, 'fields': full['fields'][:1]})
        db.commit()
        update = {**full, 'existing_id': row.id, 'expected_updated_at': row.updated_at.isoformat()}
    barrier = Barrier(2)

    def append(index):
        with Session(engine) as db:
            db.info.update(tenant_id='tenant_a', user_id='user_a')
            scenario = db.get(BusinessScenario, 'scene_a')
            payload = {**update, 'fields': full['fields'][index:index + 1]}
            semantic_mapping_authoring.validate(db, scenario, payload)
            db.commit()
            barrier.wait(timeout=10)
            try:
                semantic_mapping_authoring.apply(db, scenario, payload)
                db.commit()
                return 'applied'
            except PolicyViolation:
                db.rollback()
                return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(append, [1, 2]))
    assert sorted(results) == ['applied', 'conflict'], results
    with Session(engine) as db:
        mapping = db.get(SemanticMapping, update['existing_id'])
        assert len(mapping.field_mappings) == 2
    print('PASS: concurrent append has exactly one winner; stale contender cannot overwrite')


if __name__ == '__main__':
    try:
        main(additional_checks=verify)
    except Exception as error:
        print('FAILED:', type(error).__name__)
        if isinstance(error, AssertionError):
            print(str(error))
        raise SystemExit(1)
