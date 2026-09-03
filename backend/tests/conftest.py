"""Keep unit tests independent from a developer's production-like ``.env``."""
from __future__ import annotations

import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def isolate_runtime_environment(monkeypatch: pytest.MonkeyPatch):
    """Default tests to mutable dev definitions unless a test overrides it."""
    monkeypatch.setenv("RUNTIME_ENVIRONMENT", "dev")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
