"""Process-local test secrets for authenticated workflow payload fixtures."""
from __future__ import annotations

import base64
import json
import os
import secrets


# Production has no default key and fails closed.  Tests that exercise the
# durable worker receive an ephemeral key generated before application modules
# are imported; neither the key nor plaintext is written to a fixture database.
_test_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
os.environ.setdefault("WORKFLOW_PAYLOAD_ACTIVE_KEY_ID", "pytest-ephemeral")
os.environ.setdefault(
    "WORKFLOW_PAYLOAD_ENCRYPTION_KEYS",
    json.dumps({"pytest-ephemeral": _test_key}, separators=(",", ":")),
)
