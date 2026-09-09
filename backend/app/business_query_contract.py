"""Reject retired business selectors instead of silently accepting ignored inputs."""
from __future__ import annotations

from starlette.datastructures import QueryParams
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


RETIRED_QUERY_FIELDS = frozenset({"environment", "runtime_environment", "expected_environment"})


class BusinessQueryContractMiddleware:
    def __init__(self, app: ASGIApp, *, api_prefix: str) -> None:
        self.app = app
        self.api_prefix = api_prefix.rstrip("/") + "/"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path", "").startswith(self.api_prefix):
            query = QueryParams(scope.get("query_string", b""))
            if RETIRED_QUERY_FIELDS.intersection(query):
                response = JSONResponse(status_code=410, content={"detail": {
                    "code": "business_environment_removed",
                    "message": "业务接口不再接受部署环境参数；请使用场景发布版本及受管数据引用",
                }})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
