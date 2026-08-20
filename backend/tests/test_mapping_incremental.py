from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import BusinessScenario, DataMapping, DataSource, OntologyEntity, OntologyInstance, OntologyProperty
from app.services.ontology_service import import_instances_from_mapping


class MappingIncrementalRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.scenario = BusinessScenario(id="scenario-1", name="映射增量")
        self.source = DataSource(
            id="source-1", scenario_id=self.scenario.id, name="源库", type="sqlite", config={}
        )
        self.entity = OntologyEntity(id="entity-1", scenario_id=self.scenario.id, name="费用单")
        self.key = OntologyProperty(id="property-id", entity_id=self.entity.id, name="id", is_key=True)
        self.amount = OntologyProperty(id="property-amount", entity_id=self.entity.id, name="amount")
        self.mapping = DataMapping(
            id="mapping-1",
            scenario_id=self.scenario.id,
            entity_id=self.entity.id,
            data_source_id=self.source.id,
            table_name="expenses",
            column_map={"id": "id", "amount": "amount"},
        )
        self.db.add_all([self.scenario, self.source, self.entity, self.key, self.amount, self.mapping])
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _import(self, rows: list[list[object]]) -> dict:
        with patch(
            "app.services.datasource_service.run_query",
            return_value={"columns": ["id", "amount"], "rows": rows},
        ):
            return import_instances_from_mapping(self.db, self.scenario, self.mapping, limit=50)

    def test_refresh_updates_one_stable_object_and_records_exact_mapping_lineage(self) -> None:
        first = self._import([["E-100", 10]])
        self.assertEqual(first["instances_created"], 1)
        instance = self.db.scalar(select(OntologyInstance).where(OntologyInstance.entity_id == self.entity.id))
        self.assertIsNotNone(instance)
        assert instance is not None
        self.assertEqual(instance.source_metadata["mapping_id"], self.mapping.id)
        self.assertEqual(instance.source_metadata["data_source_id"], self.source.id)
        self.assertEqual(instance.source_metadata["record_key"], "E-100")
        self.assertTrue(instance.source_ref.startswith(f"{self.source.id}:expenses:"))

        refreshed = self._import([["E-100", 20]])
        self.assertEqual(refreshed["instances_created"], 0)
        self.assertEqual(refreshed["instances_updated"], 1)
        instances = self.db.scalars(select(OntologyInstance).where(OntologyInstance.entity_id == self.entity.id)).all()
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].attributes, {"id": "E-100", "amount": 20})

    def test_keyless_rows_use_content_hash_not_counter_for_idempotency(self) -> None:
        self.key.is_key = False
        self.mapping.column_map = {"amount": "amount"}
        self.db.commit()
        first = self._import([["E-100", 10]])
        second = self._import([["E-100", 10]])
        self.assertEqual(first["instances_created"], 1)
        self.assertEqual(second["instances_created"], 0)
        self.assertEqual(self.db.query(OntologyInstance).count(), 1)


if __name__ == "__main__":
    unittest.main()
