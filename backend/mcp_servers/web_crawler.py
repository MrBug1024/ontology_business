"""网络爬虫 MCP 服务（stdio）。

为审计 Agent 提供外部知识库 / 网络检索能力：
- fetch_url(url)   抓取网页并提取正文文本
- search_web(query) 通过 DuckDuckGo HTML 搜索获取相关网页链接与摘要

用于在规则涉及药品时，联网获取药品规格、医保限定支付条件等辅助判断信息。
"""
from __future__ import annotations

import asyncio
import json
import re

import httpx

from mcp.server.mcpserver import MCPServer

server = MCPServer(name="web-crawler", description="网络爬虫 / 外部知识库检索服务")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|tr|li|h[1-6]|section|article)>", "\n", html)
    html = re.sub(r"(?i)<[^>]+>", " ", html)
    for a, b in (
        ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
        ("&quot;", '"'), ("&#39;", "'"), ("&ldquo;", '"'), ("&rdquo;", '"'),
    ):
        html = html.replace(a, b)
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n\s*\n+", "\n", html)
    return html.strip()


async def _fetch(url: str) -> str:
    async with httpx.AsyncClient(
        headers=_HEADERS, follow_redirects=True, timeout=30.0
    ) as client:
        r = await client.get(url)
        r.raise_for_status()
        text = _strip_html(r.text)
        return text[:12000]


@server.tool(description="抓取指定 URL 的网页内容并提取正文文本，用于获取药品规格、医保限定支付条件等外部知识。参数 url 为完整网页地址。")
async def fetch_url(url: str) -> str:
    try:
        return await asyncio.wait_for(_fetch(url), timeout=35)
    except Exception as exc:  # noqa: BLE001
        return f"抓取失败: {exc}"


@server.tool(description="通过 DuckDuckGo 搜索关键词，返回相关网页的标题、链接与摘要。参数 query 为搜索词（如药品名 + 规格 / 医保限定支付条件）。")
async def search_web(query: str) -> str:
    try:
        async with httpx.AsyncClient(
            headers=_HEADERS, follow_redirects=True, timeout=30.0
        ) as client:
            r = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
            )
            r.raise_for_status()
            html = r.text
        results = []
        for m in re.finditer(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            html,
            re.S,
        ):
            href, title = m.group(1), _strip_html(m.group(2))
            snippet = ""
            sm = re.search(
                r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
                html[m.end(): m.end() + 2000],
                re.S,
            )
            if sm:
                snippet = _strip_html(sm.group(1))
            if href.startswith("//"):
                href = "https:" + href
            results.append({"title": title, "url": href, "snippet": snippet})
            if len(results) >= 8:
                break
        if not results:
            return "未搜索到相关结果"
        return json.dumps(results, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return f"搜索失败: {exc}"


if __name__ == "__main__":
    server.run("stdio")
