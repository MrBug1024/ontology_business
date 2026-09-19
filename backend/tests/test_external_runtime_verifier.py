"""Deployment checks fail closed on missing or excessive ownership privileges."""
from __future__ import annotations

import pytest

from scripts.verify_postgresql_runtime import _validate_external_asset_privileges


def test_external_asset_runtime_privileges_allow_creation_and_cleanup_only():
    _validate_external_asset_privileges({"select": True, "insert": True, "delete": True})


@pytest.mark.parametrize("privilege", ["select", "insert", "delete", "update", "truncate", "references", "trigger"])
def test_external_asset_runtime_privilege_drift_is_rejected(privilege):
    privileges = {"select": True, "insert": True, "delete": True}
    privileges[privilege] = not privileges.get(privilege, False)
    with pytest.raises(RuntimeError, match=privilege.upper()):
        _validate_external_asset_privileges(privileges)
