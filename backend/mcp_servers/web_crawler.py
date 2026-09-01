"""Fail-closed standalone MCP placeholder for outbound web access.

The project does not currently provide a transport that binds hostname policy,
DNS resolution, and the connected peer to one server-managed static allowlist.
URL pre-validation alone would remain vulnerable to DNS rebinding, so these
tools deliberately perform no network I/O.
"""
from __future__ import annotations

from typing import NoReturn

from mcp.server.fastmcp import FastMCP


WEB_ACCESS_DISABLED_MESSAGE = (
    "Outbound web access is disabled because this standalone MCP has no "
    "server-managed static host allowlist and DNS-pinned transport."
)


class WebAccessDisabledError(RuntimeError):
    """Stable, input-free error for deliberately unavailable web access."""


server = FastMCP(
    name="Web Access (Disabled)",
    instructions=(
        "Outbound web access is unavailable until the deployment provides a "
        "static host allowlist and a DNS-pinned HTTPS transport."
    ),
)


def _raise_disabled() -> NoReturn:
    raise WebAccessDisabledError(WEB_ACCESS_DISABLED_MESSAGE)


@server.tool(
    name="fetch_url",
    description="Unavailable: outbound document retrieval is disabled by policy.",
)
async def fetch_url(url: str) -> str:
    del url
    _raise_disabled()


@server.tool(
    name="search_web",
    description="Unavailable: outbound search is disabled by policy.",
)
async def search_web(query: str) -> str:
    del query
    _raise_disabled()


if __name__ == "__main__":
    server.run(transport="stdio")
