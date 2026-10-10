"""A tiny local stand-in 'intranet system' for connector end-to-end runs."""
from __future__ import annotations

import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>区域医保结算系统</title></head>
<body style="font-family: sans-serif; margin: 32px">
<h1>区域医保结算系统 · 查询工作台</h1>
<p>当前登录角色：只读审计员（E2E 演示站点）</p>
<table border="1" cellpadding="6" style="border-collapse: collapse">
<tr><th>结算单号</th><th>参保人</th><th>金额（元）</th><th>状态</th></tr>
<tr><td>JS-2026-0001</td><td>张*三</td><td>1,284.50</td><td>已结算</td></tr>
<tr><td>JS-2026-0002</td><td>李*四</td><td>986.00</td><td>待复核</td></tr>
<tr><td>JS-2026-0003</td><td>王*五</td><td>3,420.75</td><td>已结算</td></tr>
</table>
<p><label>结算单号查询 <input type="text" name="order_no" placeholder="输入结算单号"></label>
<button type="button">查询</button></p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8978
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
