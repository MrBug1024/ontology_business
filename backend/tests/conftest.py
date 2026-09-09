"""Process-local test secrets for authenticated workflow payload fixtures."""
from __future__ import annotations

import base64
import json
import os
import secrets

import pytest


# Production has no default key and fails closed.  Tests that exercise the
# durable worker receive an ephemeral key generated before application modules
# are imported; neither the key nor plaintext is written to a fixture database.
_test_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
os.environ.setdefault("WORKFLOW_PAYLOAD_ACTIVE_KEY_ID", "pytest-ephemeral")
os.environ.setdefault(
    "WORKFLOW_PAYLOAD_ENCRYPTION_KEYS",
    json.dumps({"pytest-ephemeral": _test_key}, separators=(",", ":")),
)

# Test processes may run as a different OS account from the API. Never create
# cache locks or spill directories at a path taken from the developer's .env.
os.environ["DATASET_CACHE_DIRECTORY"] = ""
os.environ["DATASET_DUCKDB_TEMP_DIRECTORY"] = ""

# Register the release audit table before unit fixtures create their schema.
from app import approval_models, release_models  # noqa: E402, F401


@pytest.fixture(autouse=True)
def isolated_dataset_cache(tmp_path, monkeypatch):
    from app.services import dataset_query_service

    monkeypatch.setattr(dataset_query_service, "_CACHE_ROOT", tmp_path / "dataset-cache")
