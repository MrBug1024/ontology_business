from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app import config
from app.config import Settings
from app.services.cache_service import MAX_CACHE_TTL_SECONDS, RedisJsonCache


def _settings(**overrides: Any) -> Settings:
    values = {
        "database_url": "",
        "annual_mysql_host": "",
        "annual_mysql_port": 3306,
        "annual_mysql_database": "",
        "annual_mysql_user": "",
        "annual_mysql_password": "",
        "redis_host": "",
        "redis_port": 6379,
        "redis_password": "",
        "minio_aliyun_endpoint": "",
        "minio_aliyun_access_key_id": "",
        "minio_aliyun_access_key_secret": "",
        "minio_aliyun_file_path": "",
        "minio_bucketname": "",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_mysql_fields_build_an_escaped_sqlalchemy_url() -> None:
    settings = _settings(
        annual_mysql_host="db.example.test",
        annual_mysql_port=3307,
        annual_mysql_database="ontology/business",
        annual_mysql_user="service@tenant",
        annual_mysql_password="p@ss:/#word",
    )

    url = make_url(settings.database_url)
    assert url.drivername == "mysql+pymysql"
    assert url.username == "service@tenant"
    assert url.password == "p@ss:/#word"
    assert url.host == "db.example.test"
    assert url.port == 3307
    assert url.database == "ontology/business"
    assert url.query["charset"] == "utf8mb4"


def test_explicit_database_url_wins_and_sqlite_remains_the_fallback() -> None:
    explicit = _settings(
        database_url="sqlite:///:memory:",
        annual_mysql_host="ignored.example.test",
        annual_mysql_database="ignored",
        annual_mysql_user="ignored",
    )
    fallback = _settings()

    assert explicit.database_url == "sqlite:///:memory:"
    assert explicit.uses_sqlite_database is True
    assert make_url(fallback.database_url).get_backend_name() == "sqlite"
    assert Path(make_url(fallback.database_url).database or "").name == "platform.db"


def test_partial_mysql_configuration_fails_closed() -> None:
    with pytest.raises(ValidationError, match="ANNUAL_MYSQL_DATABASE"):
        _settings(annual_mysql_host="db.example.test")


def test_remote_mysql_and_minio_do_not_create_local_data_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    buckets_dir = data_dir / "buckets"
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "BUCKETS_DIR", buckets_dir)
    monkeypatch.setattr(config, "SKILLS_DIR", skills_dir)
    settings = _settings(
        database_url="mysql+pymysql://service:secret@db.example.test/platform",
        minio_aliyun_endpoint="minio.example.test",
        minio_aliyun_access_key_id="access",
        minio_aliyun_access_key_secret="secret",
        minio_bucketname="ontology",
    )

    config.ensure_runtime_directories(settings)

    assert skills_dir.is_dir()
    assert not data_dir.exists()
    assert not buckets_dir.exists()


def test_local_storage_keeps_runtime_directory_compatibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "runtime-data"
    buckets_dir = data_dir / "buckets"
    skills_dir = tmp_path / "skills"
    database_path = tmp_path / "custom" / "platform.db"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "BUCKETS_DIR", buckets_dir)
    monkeypatch.setattr(config, "SKILLS_DIR", skills_dir)
    settings = _settings(database_url=f"sqlite:///{database_path.as_posix()}")

    config.ensure_runtime_directories(settings)

    assert data_dir.is_dir()
    assert buckets_dir.is_dir()
    assert skills_dir.is_dir()
    assert database_path.parent.is_dir()


class MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiries: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str, *, ex: int) -> bool:
        self.values[key] = value
        self.expiries[key] = ex
        return True

    def delete(self, key: str) -> int:
        existed = key in self.values
        self.values.pop(key, None)
        self.expiries.pop(key, None)
        return int(existed)

    def ping(self) -> bool:
        return True


class BrokenRedis:
    def ping(self) -> None:
        raise ConnectionError("offline")

    def get(self, _key: str) -> None:
        raise ConnectionError("offline")

    def set(self, _key: str, _value: str, *, ex: int) -> None:
        raise ConnectionError(f"offline:{ex}")

    def delete(self, _key: str) -> None:
        raise ConnectionError("offline")


def test_json_cache_is_namespaced_bounded_and_unicode_safe() -> None:
    client = MemoryRedis()
    cache = RedisJsonCache(
        client,
        namespace="ontology-business:test:cache:v1",
    )

    assert cache.set_json(
        "tenant:医保",
        {"name": "医保违规审计", "count": 2},
        ttl_seconds=MAX_CACHE_TTL_SECONDS + 100,
    )
    stored_key = cache.cache_key("tenant:医保")
    assert stored_key.startswith("ontology-business:test:cache:v1:")
    assert "tenant%3A" in stored_key
    assert client.expiries[stored_key] == MAX_CACHE_TTL_SECONDS
    assert cache.get_json("tenant:医保") == {"count": 2, "name": "医保违规审计"}
    assert cache.delete("tenant:医保")
    assert cache.get_json("tenant:医保", {"miss": True}) == {"miss": True}


def test_json_cache_failure_and_invalid_payload_degrade_to_miss() -> None:
    broken = RedisJsonCache(BrokenRedis(), namespace="ontology-business:test:cache:v1")
    assert broken.get_json("key", "fallback") == "fallback"
    assert broken.set_json("key", {"value": 1}) is False
    assert broken.delete("key") is False

    client = MemoryRedis()
    cache = RedisJsonCache(client, namespace="ontology-business:test:cache:v1")
    client.values[cache.cache_key("bad-json")] = "{"
    assert cache.get_json("bad-json", "fallback") == "fallback"
    assert cache.cache_key("bad-json") not in client.values


def test_cache_healthcheck_has_explicit_optional_failure_semantics() -> None:
    assert RedisJsonCache(None).healthcheck() is False
    assert RedisJsonCache(MemoryRedis()).healthcheck() is True
    assert RedisJsonCache(BrokenRedis()).healthcheck() is False


def test_cache_client_factory_uses_redis_settings_without_connecting() -> None:
    captured: dict[str, Any] = {}
    client = MemoryRedis()

    def factory(**kwargs: Any) -> MemoryRedis:
        captured.update(kwargs)
        return client

    settings = _settings(
        database_url="sqlite:///:memory:",
        redis_host="redis.example.test",
        redis_port=6380,
        redis_password="secret",
        runtime_environment="staging",
    )
    cache = RedisJsonCache.from_settings(settings, client_factory=factory)

    assert cache.enabled is True
    assert cache.namespace == "ontology-business:staging:cache:v1"
    assert captured["host"] == "redis.example.test"
    assert captured["port"] == 6380
    assert captured["password"] == "secret"
    assert captured["decode_responses"] is True
