from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.models import OntologyProperty
from app.services.ontology_service import allocate_resource_api_name


def _allocate(db, *, entity='subject', value='', key='entity.subject.field'):
    return allocate_resource_api_name(db, OntologyProperty, scope_field='entity_id',
        scope_id=entity, value=value, display_name='字段', prefix='property', stable_key=key)


def test_pending_properties_reserve_distinct_names_without_autoflush(monkeypatch):
    with Session(autoflush=False) as db:
        monkeypatch.setattr(db, 'execute', lambda *_: SimpleNamespace(scalar_one_or_none=lambda: None))
        first = _allocate(db, key='entity.subject.字段甲')
        db.add(OntologyProperty(entity_id='subject', name='字段甲', api_name=first))
        second = _allocate(db, key='entity.subject.字段乙')
        assert second != first
        assert _allocate(db, entity='another', key='entity.subject.字段乙') == first
        with pytest.raises(ValueError, match='api_name 已存在'):
            _allocate(db, value=first)
