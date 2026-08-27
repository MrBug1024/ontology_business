"""Optional Redis-backed JSON cache with database-first failure semantics."""
from __future__ import annotations

from functools import lru_cache
import json
import logging
import re
from typing import Any, Callable
from urllib.parse import quote

from ..config import Settings, get_settings


logger = logging.getLogger(__name__)

DEFAULT_CACHE_TTL_SECONDS = 300
MAX_CACHE_TTL_SECONDS = 24 * 60 * 60
_NAMESPACE_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,160}")


class RedisJsonCache:
    """A bounded cache only; authoritative data remains in PostgreSQL/MinIO."""

    def __init__(
        self,
        client: Any | None,
        *,
        namespace: str = "ontology-business:dev:cache:v1",
        default_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        normalized_namespace = str(namespace or "").strip(":")
        if not _NAMESPACE_PATTERN.fullmatch(normalized_namespace):
            raise ValueError("Redis cache namespace is invalid")
        self._client = client
        self.namespace = normalized_namespace
        self.default_ttl_seconds = self._bounded_ttl(default_ttl_seconds)
        self._failure_logged = False

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        client_factory: Callable[..., Any] | None = None,
    ) -> "RedisJsonCache":
        configured = settings or get_settings()
        namespace = f"ontology-business:{configured.runtime_environment}:cache:v1"
        if not configured.redis_configured:
            return cls(None, namespace=namespace)
        if client_factory is None:
            try:
                from redis import Redis
            except ImportError:
                logger.warning("Redis client is unavailable; cache is disabled")
                return cls(None, namespace=namespace)
            client_factory = Redis
        try:
            client = client_factory(
                host=configured.redis_host.strip(),
                port=configured.redis_port,
                password=configured.redis_password or None,
                db=0,
                decode_responses=True,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
            )
        except Exception as exc:  # noqa: BLE001 - cache construction must not stop startup.
            logger.warning(
                "Redis client initialization failed; cache is disabled (%s)",
                type(exc).__name__,
            )
            client = None
        return cls(client, namespace=namespace)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def healthcheck(self) -> bool:
        """Return whether the optional Redis cache is reachable right now.

        ``False`` covers both an unconfigured cache and a configured cache that
        cannot be reached.  The cache remains optional in either case, so this
        signal must not be used to reject requests backed by authoritative
        storage.
        """
        if self._client is None:
            return False
        try:
            return bool(self._client.ping())
        except Exception as exc:  # noqa: BLE001 - health checks also degrade safely.
            self._degrade(exc)
            return False

    @staticmethod
    def _bounded_ttl(value: int) -> int:
        try:
            ttl = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Redis cache TTL must be an integer") from exc
        if ttl <= 0:
            raise ValueError("Redis cache TTL must be positive")
        return min(ttl, MAX_CACHE_TTL_SECONDS)

    def cache_key(self, logical_key: str) -> str:
        value = str(logical_key or "")
        if not value or len(value) > 512 or any(ord(char) < 32 for char in value):
            raise ValueError("Redis cache key is invalid")
        return f"{self.namespace}:{quote(value, safe='-_.~')}"

    def _degrade(self, exc: BaseException) -> None:
        if not self._failure_logged:
            logger.warning(
                "Redis cache operation failed; using authoritative storage (%s)",
                type(exc).__name__,
            )
            self._failure_logged = True
        # A cache outage must not add the socket timeout to every authoritative
        # storage request. Recovery is intentionally process-scoped: a restarted
        # worker constructs a fresh client and probes Redis again.
        self._client = None

    def get_json(self, logical_key: str, default: Any = None) -> Any:
        if self._client is None:
            return default
        try:
            raw = self._client.get(self.cache_key(logical_key))
        except Exception as exc:  # noqa: BLE001 - a cache miss is the safe fallback.
            self._degrade(exc)
            return default
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            try:
                self._client.delete(self.cache_key(logical_key))
            except Exception as exc:  # noqa: BLE001
                self._degrade(exc)
            return default

    def set_json(
        self,
        logical_key: str,
        value: Any,
        *,
        ttl_seconds: int | None = None,
    ) -> bool:
        if self._client is None:
            return False
        try:
            ttl = self._bounded_ttl(
                self.default_ttl_seconds if ttl_seconds is None else ttl_seconds
            )
            payload = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            return bool(
                self._client.set(self.cache_key(logical_key), payload, ex=ttl)
            )
        except Exception as exc:  # noqa: BLE001 - writes to cache are best effort.
            self._degrade(exc)
            return False

    def delete(self, logical_key: str) -> bool:
        if self._client is None:
            return False
        try:
            return bool(self._client.delete(self.cache_key(logical_key)))
        except Exception as exc:  # noqa: BLE001
            self._degrade(exc)
            return False


@lru_cache
def get_cache() -> RedisJsonCache:
    return RedisJsonCache.from_settings()


def get_json(logical_key: str, default: Any = None) -> Any:
    return get_cache().get_json(logical_key, default)


def healthcheck() -> bool:
    return get_cache().healthcheck()


def set_json(
    logical_key: str,
    value: Any,
    *,
    ttl_seconds: int | None = None,
) -> bool:
    return get_cache().set_json(logical_key, value, ttl_seconds=ttl_seconds)


def delete(logical_key: str) -> bool:
    return get_cache().delete(logical_key)
