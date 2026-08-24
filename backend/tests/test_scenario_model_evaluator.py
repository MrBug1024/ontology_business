from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import BusinessScenario, Tenant
from app.services import scenario_model_compiler, scenario_model_evaluator


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "construction_v1.json"


class ScenarioModelEvaluatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        tenant = Tenant(id="tenant-construction-eval", name="建筑黄金评估租户")
        self.scenario = BusinessScenario(
            id="scenario-construction-eval",
            tenant_id=tenant.id,
            name="建筑项目风险管控",
            namespace="construction",
        )
        self.db.add_all([tenant, self.scenario])
        self.db.commit()
        self.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        self.source_bundle = scenario_model_compiler.build_source_bundle(
            "", self.fixture["documents"]
        )

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _normalize(self, key: str) -> dict:
        return scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            self.fixture[key],
            source_bundle=self.source_bundle,
        )

    def test_construction_v1_scores_each_semantic_layer_without_llm(self) -> None:
        with patch.object(
            scenario_model_compiler.llm_service,
            "chat",
            side_effect=AssertionError("黄金样本评估不得调用 LLM"),
        ):
            gold = self._normalize("gold_model")
            candidate = self._normalize("benchmark_candidate")
            report = scenario_model_evaluator.evaluate_scenario_model(candidate, gold)

        self.assertEqual(gold["unresolved"], [])
        self.assertEqual(candidate["unresolved"], [])
        self.assertEqual(
            report["categories"],
            ["object", "property", "relation", "constraint", "rule"],
        )
        for category, expected in self.fixture["expected_metrics"].items():
            actual = report["metrics"][category]
            self.assertEqual(actual["true_positive"], expected["tp"])
            self.assertEqual(actual["false_positive"], expected["fp"])
            self.assertEqual(actual["false_negative"], expected["fn"])
            self.assertAlmostEqual(actual["precision"], expected["precision"])
            self.assertAlmostEqual(actual["recall"], expected["recall"])
            self.assertAlmostEqual(actual["f1"], expected["f1"])

    def test_gold_self_evaluation_is_perfect_and_reproducible(self) -> None:
        gold = self._normalize("gold_model")
        first = scenario_model_evaluator.evaluate_scenario_model(gold, gold)
        second = scenario_model_evaluator.evaluate_scenario_model(gold, gold)
        self.assertEqual(first, second)
        for category in scenario_model_evaluator.CATEGORIES:
            metric = first["metrics"][category]
            self.assertEqual(metric["precision"], 1.0)
            self.assertEqual(metric["recall"], 1.0)
            self.assertEqual(metric["f1"], 1.0)
            self.assertEqual(metric["missing"], [])
            self.assertEqual(metric["unexpected"], [])
        self.assertEqual(first["metrics"]["micro"]["f1"], 1.0)
        self.assertEqual(first["metrics"]["macro"]["f1"], 1.0)

    def test_diagnostics_name_the_missing_and_unexpected_business_atoms(self) -> None:
        gold = self._normalize("gold_model")
        candidate = self._normalize("benchmark_candidate")
        report = scenario_model_evaluator.evaluate_scenario_model(candidate, gold)

        missing_properties = report["metrics"]["property"]["missing"]
        unexpected_objects = report["metrics"]["object"]["unexpected"]
        self.assertIn(["项目", "项目名称"], missing_properties)
        self.assertIn(["设备"], unexpected_objects)
        self.assertTrue(report["metrics"]["constraint"]["missing"])
        self.assertTrue(report["metrics"]["rule"]["unexpected"])


if __name__ == "__main__":
    unittest.main()
