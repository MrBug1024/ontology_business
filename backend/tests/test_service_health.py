from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from app import main


class _Connection:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def __enter__(self):
        if self.fail:
            raise ConnectionError("database unavailable")
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, _statement) -> None:
        return None


class _Engine:
    dialect = SimpleNamespace(name="mysql")

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def connect(self) -> _Connection:
        return _Connection(fail=self.fail)


def _settings(*, redis_configured: bool = True):
    return SimpleNamespace(
        minio_aliyun_endpoint="minio.example.test",
        minio_aliyun_access_key_id="access",
        minio_aliyun_access_key_secret="secret",
        minio_bucketname="ontology",
        uses_sqlite_database=False,
        redis_configured=redis_configured,
    )


def _body(response) -> dict:
    return json.loads(bytes(response.body))


def test_health_reports_all_remote_dependencies_ready() -> None:
    with (
        patch.object(main, "engine", _Engine()),
        patch.object(main, "settings", _settings()),
        patch.object(
            main.object_storage_service,
            "configuration",
            return_value=SimpleNamespace(configured=True),
        ),
        patch.object(main.object_storage_service, "healthcheck", return_value=True),
        patch.object(main.cache_service, "healthcheck", return_value=True),
    ):
        response = main.health()

    assert response.status_code == 200
    assert _body(response)["status"] == "ok"


def test_health_returns_503_when_authoritative_database_or_minio_is_down() -> None:
    with (
        patch.object(main, "engine", _Engine(fail=True)),
        patch.object(main, "settings", _settings()),
        patch.object(
            main.object_storage_service,
            "configuration",
            return_value=SimpleNamespace(configured=True),
        ),
        patch.object(main.object_storage_service, "healthcheck", return_value=True),
        patch.object(main.cache_service, "healthcheck", return_value=True),
    ):
        database_response = main.health()

    with (
        patch.object(main, "engine", _Engine()),
        patch.object(main, "settings", _settings()),
        patch.object(
            main.object_storage_service,
            "configuration",
            return_value=SimpleNamespace(configured=True),
        ),
        patch.object(main.object_storage_service, "healthcheck", return_value=False),
        patch.object(main.cache_service, "healthcheck", return_value=True),
    ):
        minio_response = main.health()

    assert database_response.status_code == 503
    assert _body(database_response)["status"] == "unavailable"
    assert minio_response.status_code == 503
    assert _body(minio_response)["status"] == "unavailable"


def test_health_treats_redis_as_optional_cache() -> None:
    with (
        patch.object(main, "engine", _Engine()),
        patch.object(main, "settings", _settings()),
        patch.object(
            main.object_storage_service,
            "configuration",
            return_value=SimpleNamespace(configured=True),
        ),
        patch.object(main.object_storage_service, "healthcheck", return_value=True),
        patch.object(main.cache_service, "healthcheck", return_value=False),
    ):
        response = main.health()

    payload = _body(response)
    assert response.status_code == 200
    assert payload["status"] == "degraded"
    assert payload["dependencies"]["redis"]["authoritative"] is False
