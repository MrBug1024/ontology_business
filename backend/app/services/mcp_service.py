"""MCP 服务：连接 MCP Server（stdio / sse / streamable_http），列出并调用工具。"""
from __future__ import annotations

import asyncio
from typing import Any

from ..models import MCPConfig


def _run(coro):
    """在同步上下文中运行协程（避免与已有事件循环冲突）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


async def _list_tools_async(cfg: MCPConfig) -> list[dict[str, Any]]:
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client.streamable_http import streamable_http_client

    if cfg.transport == "stdio":
        params = StdioServerParameters(command=cfg.command, args=cfg.args or [], env=cfg.env or None)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.list_tools()
                return _tools_to_dict(res.tools)
    if cfg.transport == "sse":
        async with sse_client(cfg.url, headers=cfg.headers or None) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.list_tools()
                return _tools_to_dict(res.tools)
    if cfg.transport in ("streamable_http", "http"):
        async with streamable_http_client(cfg.url, headers=cfg.headers or None) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.list_tools()
                return _tools_to_dict(res.tools)
    raise ValueError(f"未知 MCP 传输类型: {cfg.transport}")


async def _call_tool_async(cfg: MCPConfig, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client.streamable_http import streamable_http_client

    if cfg.transport == "stdio":
        params = StdioServerParameters(command=cfg.command, args=cfg.args or [], env=cfg.env or None)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool(name, arguments)
                return _content_to_text(res)
    if cfg.transport == "sse":
        async with sse_client(cfg.url, headers=cfg.headers or None) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool(name, arguments)
                return _content_to_text(res)
    if cfg.transport in ("streamable_http", "http"):
        async with streamable_http_client(cfg.url, headers=cfg.headers or None) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool(name, arguments)
                return _content_to_text(res)
    raise ValueError(f"未知 MCP 传输类型: {cfg.transport}")


def _tools_to_dict(tools) -> list[dict[str, Any]]:
    out = []
    for t in tools:
        out.append(
            {
                "name": t.name,
                "description": getattr(t, "description", "") or "",
                "input_schema": getattr(t, "inputSchema", {}) or {},
            }
        )
    return out


def _content_to_text(res) -> dict[str, Any]:
    parts: list[str] = []
    for c in getattr(res, "content", []) or []:
        if hasattr(c, "text"):
            parts.append(c.text)
        else:
            parts.append(str(c))
    return {"status": "success" if not getattr(res, "isError", False) else "error", "text": "\n".join(parts)}


def list_tools(cfg: MCPConfig) -> list[dict[str, Any]]:
    return _run(_list_tools_async(cfg))


def call_tool(cfg: MCPConfig, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return _run(_call_tool_async(cfg, name, arguments))


def test_connection(cfg: MCPConfig) -> tuple[bool, str]:
    try:
        tools = list_tools(cfg)
        return True, f"连接成功，发现 {len(tools)} 个工具"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
