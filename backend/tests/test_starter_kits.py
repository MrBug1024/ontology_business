"""Regression coverage for repository-owned governed starter kits."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import package_service, starter_kit_service


class StarterKitServiceTests(unittest.TestCase):
    def test_catalog_is_deterministic_and_every_asset_matches_package_contract(self) -> None:
        first = starter_kit_service.list_starter_kits()
        second = starter_kit_service.list_starter_kits()

        self.assertEqual(
            [item.id for item in first],
            ["retail", "bookkeeping", "supply-chain"],
        )
        self.assertEqual(
            [item.model_dump() for item in first],
            [item.model_dump() for item in second],
        )

        for summary in first:
            with self.subTest(starter_kit=summary.id):
                package = starter_kit_service.load_starter_kit(summary.id)
                validation = package_service.validate_package(package)
                self.assertTrue(validation["valid"], validation["errors"])
                self.assertEqual(validation["fingerprint"], summary.fingerprint)
                self.assertEqual(package["manifest"]["fingerprint"], summary.fingerprint)
                self.assertEqual(
                    summary.resource_counts,
                    {kind: len(package["resources"][kind]) for kind in package_service.RESOURCE_KINDS},
                )

    def test_load_returns_a_fresh_copy(self) -> None:
        first = starter_kit_service.load_starter_kit("retail")
        first["manifest"]["name"] = "attempted mutation"
        second = starter_kit_service.load_starter_kit("retail")

        self.assertEqual(second["manifest"]["name"], "零售经营 Starter Kit")

    def test_tampered_catalog_fingerprint_is_rejected(self) -> None:
        with self._temporary_retail_kit() as root:
            manifest_path = root / "retail" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["fingerprint"] = "sha256:" + "0" * 64
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            with patch.object(starter_kit_service, "STARTER_KIT_ROOT", root):
                with self.assertRaises(starter_kit_service.StarterKitIntegrityError):
                    starter_kit_service.load_starter_kit("retail")

    def test_sensitive_connector_and_runtime_payloads_fail_closed(self) -> None:
        cases = (
            (
                "sensitive",
                lambda package: package["resources"]["entities"][0].update(
                    {"description": "token=must-not-be-bundled"}
                ),
                "sensitive_content_forbidden",
            ),
            (
                "connector",
                lambda package: package["resources"]["events"][0]["payload_schema"].update(
                    {"endpoint": "https://connector.example.test"}
                ),
                "connector_configuration_forbidden",
            ),
            (
                "connection-url-value",
                lambda package: package["resources"]["events"][0].update(
                    {"description": "jdbc:postgresql://database.example.test:5432/retail"}
                ),
                "connector_configuration_forbidden",
            ),
            (
                "runtime-data",
                lambda package: package["resources"]["events"][0]["payload_schema"].update(
                    {"execution_logs": [{"status": "completed"}]}
                ),
                "runtime_or_business_data_forbidden",
            ),
        )
        for label, mutate, code in cases:
            with self.subTest(label=label), self._temporary_retail_kit() as root:
                package_path = root / "retail" / "package.json"
                package = json.loads(package_path.read_text(encoding="utf-8"))
                mutate(package)
                package_path.write_text(
                    json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                with patch.object(starter_kit_service, "STARTER_KIT_ROOT", root):
                    with self.assertRaises(starter_kit_service.StarterKitSafetyError) as raised:
                        starter_kit_service.load_starter_kit("retail")
                self.assertEqual(raised.exception.code, code)

    def test_unknown_kit_identifier_cannot_be_used_as_a_path(self) -> None:
        for identifier in ("unknown", "../retail", "retail/package.json", "RETAIL"):
            with self.subTest(identifier=identifier):
                with self.assertRaises(starter_kit_service.StarterKitNotFoundError):
                    starter_kit_service.get_starter_kit(identifier)

    @staticmethod
    def _temporary_retail_kit():
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        shutil.copytree(starter_kit_service.STARTER_KIT_ROOT / "retail", root / "retail")

        class _TemporaryKitContext:
            def __enter__(self) -> Path:
                return root

            def __exit__(self, exc_type, exc, traceback) -> None:
                temporary.cleanup()

        return _TemporaryKitContext()


if __name__ == "__main__":
    unittest.main()
