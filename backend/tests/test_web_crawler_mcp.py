from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from backend.mcp_servers import web_crawler


BACKEND_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = BACKEND_ROOT / "mcp_servers" / "web_crawler.py"


def test_standalone_web_tools_fail_closed_without_echoing_input() -> None:
    inputs = (
        (
            web_crawler.fetch_url,
            "https://127.0.0.1:8443/private?credential=do-not-echo",
        ),
        (web_crawler.search_web, "private search terms must not be echoed"),
    )

    messages: list[str] = []
    for tool, untrusted_input in inputs:
        with pytest.raises(web_crawler.WebAccessDisabledError) as raised:
            asyncio.run(tool(untrusted_input))
        message = str(raised.value)
        assert untrusted_input not in message
        messages.append(message)

    assert messages == [web_crawler.WEB_ACCESS_DISABLED_MESSAGE] * len(inputs)


def test_disabled_server_has_no_network_implementation_or_domain_policy() -> None:
    source = SERVER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        str(node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )

    forbidden_network_modules = {
        "aiohttp",
        "dns",
        "http.client",
        "httpx",
        "requests",
        "socket",
        "urllib.request",
    }
    assert not imported_modules.intersection(forbidden_network_modules)

    normalized = source.casefold()
    for marker in ("medical", "insurance", "drug", "医保", "药品", "处方"):
        assert marker.casefold() not in normalized


def test_disabled_server_is_not_wired_into_the_platform_application() -> None:
    references: list[Path] = []
    for module in (BACKEND_ROOT / "app").rglob("*.py"):
        source = module.read_text(encoding="utf-8")
        if "mcp_servers.web_crawler" in source or "mcp_servers import web_crawler" in source:
            references.append(module)

    assert references == []
