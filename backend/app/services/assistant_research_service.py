"""Governed, read-only domain research adapter for the business advisor.

Deployments opt in by configuring a server-owned search endpoint and an exact
host allowlist.  Search results are normalized into citation cards and never
become ontology definitions automatically.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import get_settings


class ResearchUnavailable(RuntimeError):
    """Research is not configured or the configured provider failed safely."""


def _allowed_hosts() -> set[str]:
    return {
        value.strip().casefold()
        for value in get_settings().assistant_research_allowed_hosts.split(",")
        if value.strip()
    }


def _endpoint() -> str:
    settings = get_settings()
    if not settings.assistant_research_enabled or not settings.assistant_research_endpoint:
        raise ResearchUnavailable("行业知识检索尚未配置")
    endpoint = settings.assistant_research_endpoint.strip()
    parsed = urlsplit(endpoint)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise ResearchUnavailable("行业知识检索端点必须是无凭据的 HTTPS 地址")
    if host not in _allowed_hosts():
        raise ResearchUnavailable("行业知识检索端点不在部署允许的来源范围内")
    return endpoint


def _source(item: Any, index: int, retrieved_at: str) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    url = str(item.get("url") or "").strip()
    title = str(item.get("title") or "").strip()
    if not url or not title:
        return None
    parsed = urlsplit(url)
    if parsed.scheme not in {"https"} or not parsed.netloc:
        return None
    return {
        "id": f"research-{index}",
        "kind": "web_research",
        "title": title[:300],
        "filename": title[:300],
        "url": url[:2048],
        "snippet": str(item.get("snippet") or item.get("text") or "")[:2_000],
        "published_at": str(item.get("published_at") or "")[:80],
        "retrieved_at": retrieved_at,
        "citation_id": f"W{index}",
    }


def search(query: str, *, limit: int = 8) -> dict[str, Any]:
    endpoint = _endpoint()
    normalized_query = str(query or "").strip()
    if not normalized_query:
        raise ResearchUnavailable("行业知识检索需要明确的问题")
    limit = max(1, min(int(limit), 20))
    settings = get_settings()
    try:
        with httpx.Client(
            timeout=httpx.Timeout(settings.assistant_research_timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.post(endpoint, json={"query": normalized_query, "limit": limit})
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ResearchUnavailable("行业知识检索服务暂时不可用") from exc
    raw_results = body.get("results") if isinstance(body, dict) else body
    if not isinstance(raw_results, list):
        raise ResearchUnavailable("行业知识检索服务返回了无效结果")
    retrieved_at = datetime.now(timezone.utc).isoformat()
    sources = [
        source
        for index, item in enumerate(raw_results[:limit], 1)
        if (source := _source(item, index, retrieved_at)) is not None
    ]
    return {
        "query": normalized_query,
        "retrieved_at": retrieved_at,
        "sources": sources,
        "provider": "configured_search_endpoint",
        "read_only": True,
        "formalization_allowed": False,
    }

