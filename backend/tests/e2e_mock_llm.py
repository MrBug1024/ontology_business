"""Tiny OpenAI-compatible provider used only by the isolated browser E2E run.

Run with ``python tests/e2e_mock_llm.py --port 8033`` after seeding the
matching fixture. It deliberately exposes no credentials and never forwards a
request outside localhost.
"""
from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    server_version = "P1E2EMockLLM/1.0"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, payload: dict | str) -> None:
        encoded = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.wfile.write(f"data: {encoded}\n\n".encode("utf-8"))
        self.wfile.flush()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": {"message": "invalid JSON"}})
            return
        if self.path.rstrip("/").endswith("/embeddings"):
            items = request.get("input") or []
            if isinstance(items, str):
                items = [items]
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"object": "embedding", "embedding": [0.1, 0.2, 0.3], "index": index}
                        for index, _ in enumerate(items)
                    ],
                    "model": request.get("model", "e2e-model"),
                    "usage": {"prompt_tokens": 3, "total_tokens": 3},
                },
            )
            return
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json(404, {"error": {"message": "not found"}})
            return

        reply = "E2E 模拟模型已响应：当前上下文已按权限范围处理。"
        model = str(request.get("model") or "e2e-model")
        created = int(time.time())
        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self._sse(
                {
                    "id": "e2e-chat-stream",
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant", "content": reply}, "finish_reason": None}
                    ],
                }
            )
            self._sse(
                {
                    "id": "e2e-chat-stream",
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
            )
            self._sse("[DONE]")
            return
        self._json(
            200,
            {
                "id": "e2e-chat",
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 8, "total_tokens": 16},
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8033)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), _Handler)
    print(f"e2e mock llm listening on http://127.0.0.1:{args.port}/v1", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
