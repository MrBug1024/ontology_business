from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import mcp
from mcp.client import sse as sse_module
from mcp.client import streamable_http as streamable_http_module

from app.database import Base
from app.models import MCPConfig, Tenant, User
from app.routers import mcp as mcp_router
from app.schemas import MCPConfigIn, MCPStandardImportIn
from app.services import mcp_service, permission_service
from app.services.auth_service import get_tenant_db


class MCPStandardConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        tenant = Tenant(id="tenant-mcp-config", name="MCP 测试租户")
        user = User(
            id="user-mcp-config",
            tenant_id=tenant.id,
            email="mcp-config@example.test",
            password_hash="test-only",
            status="active",
        )
        self.db.add_all([tenant, user])
        self.db.commit()
        permission_service.ensure_organization(self.db, tenant.id, owner_user_id=user.id)
        self.db.commit()
        self.db.info["tenant_id"] = tenant.id
        self.db.info["user_id"] = user.id
        self.app = FastAPI()
        self.app.include_router(mcp_router.router)

        def tenant_db_override():
            yield self.db

        self.app.dependency_overrides[get_tenant_db] = tenant_db_override

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def standard_payload(url: str = "https://example.test/mcp") -> MCPStandardImportIn:
        return MCPStandardImportIn.model_validate({
            "mcpServers": {
                "search": {
                    "type": "http",
                    "url": url,
                    "headers": {"Authorization": "Bearer test-only"},
                }
            }
        })

    def test_standard_http_alias_normalizes_and_dry_run_is_side_effect_free(self) -> None:
        payload = self.standard_payload()
        internal = payload.internal_configs()[0]
        self.assertEqual(internal.name, "search")
        self.assertEqual(internal.transport, "streamable_http")
        self.assertEqual(internal.headers["Authorization"], "Bearer test-only")

        result = mcp_router.import_standard_mcp(
            payload, dry_run=True, conflict_policy="error", db=self.db
        )
        self.assertTrue(result.dry_run)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.items[0].header_keys, ["Authorization"])
        self.assertNotIn("test-only", result.model_dump_json())
        self.assertEqual(self.db.scalar(select(func.count()).select_from(MCPConfig)), 0)

    def test_import_route_validates_atomically_and_never_echoes_header_values(self) -> None:
        client = TestClient(self.app)
        body = {
            "mcpServers": {
                "search": {
                    "type": "http",
                    "url": "https://example.test/mcp",
                    "headers": {"Authorization": "Bearer route-test-only"},
                }
            }
        }
        preview = client.post("/mcp/import?dry_run=true", json=body)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertNotIn("route-test-only", preview.text)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(MCPConfig)), 0)

        invalid = {
            "mcpServers": {
                **body["mcpServers"],
                "broken": {
                    "type": "http",
                    "url": "https://example.test/other",
                    "headers": {"Authorization": 123},
                },
            }
        }
        rejected = client.post("/mcp/import", json=invalid)
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(MCPConfig)), 0)

        committed = client.post("/mcp/import", json=body)
        self.assertEqual(committed.status_code, 200, committed.text)
        self.assertNotIn("route-test-only", committed.text)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(MCPConfig)), 1)

    def test_import_is_atomic_redacts_values_and_has_explicit_conflict_policy(self) -> None:
        created = mcp_router.import_standard_mcp(
            self.standard_payload(), dry_run=False, conflict_policy="error", db=self.db
        )
        self.assertEqual(created.created, 1)
        self.assertEqual(created.configs[0].headers, {"Authorization": ""})
        stored = self.db.scalar(select(MCPConfig))
        self.assertEqual(stored.headers["Authorization"], "Bearer test-only")

        with self.assertRaises(HTTPException) as conflict:
            mcp_router.import_standard_mcp(
                self.standard_payload("https://replacement.test/mcp"),
                dry_run=False,
                conflict_policy="error",
                db=self.db,
            )
        self.assertEqual(conflict.exception.status_code, 409)
        self.assertEqual(self.db.scalar(select(MCPConfig)).url, "https://example.test/mcp")

        replaced = mcp_router.import_standard_mcp(
            self.standard_payload("https://replacement.test/mcp"),
            dry_run=False,
            conflict_policy="replace",
            db=self.db,
        )
        self.assertEqual(replaced.replaced, 1)
        self.assertEqual(self.db.scalar(select(MCPConfig)).url, "https://replacement.test/mcp")

    def test_invalid_transport_contracts_and_header_types_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            MCPConfigIn(name="broken", transport="stdio", command="")
        with self.assertRaises(ValueError):
            MCPStandardImportIn.model_validate({
                "mcpServers": {
                    "broken": {
                        "type": "http",
                        "url": "not-a-url",
                        "headers": {"Authorization": 1},
                    }
                }
            })
        with self.assertRaises(ValueError):
            MCPConfigIn(
                name="unsafe-header",
                transport="streamable_http",
                url="https://example.test/mcp",
                headers={"Host": "internal.test"},
            )
        with self.assertRaises(ValueError):
            MCPConfigIn(name="   ", transport="stdio", command="python")
        with self.assertRaises(ValueError):
            MCPConfigIn(
                name="query-secret",
                transport="streamable_http",
                url="https://example.test/mcp?access_token=test-only",
            )
        with self.assertRaises(ValueError):
            MCPConfigIn(
                name="duplicate-header",
                transport="streamable_http",
                url="https://example.test/mcp",
                headers={"Authorization": "one", "authorization": "two"},
            )
        with self.assertRaises(ValueError):
            MCPConfigIn(
                name="bad-header-name",
                transport="streamable_http",
                url="https://example.test/mcp",
                headers={"Bad Header": "value"},
            )
        with self.assertRaises(ValueError):
            MCPConfigIn(
                name="private-target",
                transport="streamable_http",
                url="https://127.0.0.1/mcp",
            )

        self.assertEqual(
            mcp_router._public_endpoint("https://example.test/mcp?session=test-only#fragment"),
            "https://example.test/mcp",
        )

    def test_public_endpoint_removes_all_credential_bearing_components(self) -> None:
        self.assertEqual(
            mcp_router._public_endpoint(
                "https://legacy-user:legacy-pass@example.test:8443/mcp?token=secret#fragment"
            ),
            "https://example.test:8443/mcp",
        )
        self.assertEqual(mcp_router._public_endpoint("ftp://example.test/mcp"), "")
        self.assertEqual(mcp_router._public_endpoint("https://example.test:bad/mcp"), "")
        self.assertEqual(mcp_router._public_endpoint("https://exa mple.test/mcp"), "")
        self.assertEqual(mcp_router._public_endpoint("https://example.test\\@evil.test/mcp"), "")
        self.assertEqual(mcp_router._public_endpoint("https://example.test/%zz"), "")
        self.assertEqual(mcp_router._public_endpoint("not-an-endpoint"), "")

    def test_legacy_rows_have_safe_compatible_output(self) -> None:
        legacy = SimpleNamespace(
            id="legacy-http",
            name="legacy",
            transport="http",
            command="old-command-that-will-not-run",
            args=["--legacy"],
            url="https://example.test/mcp?session=legacy-value",
            env={"LEGACY": "secret"},
            headers={"Authorization": "Bearer secret"},
            enabled=False,
            created_at=datetime.now(timezone.utc),
        )
        result = mcp_router._out(legacy)
        self.assertEqual(result.transport, "streamable_http")
        self.assertNotIn("legacy-value", result.url)
        self.assertEqual(result.command, "")
        self.assertEqual(result.args, [])
        self.assertEqual(result.headers, {"Authorization": ""})
        self.assertEqual(result.env, {"LEGACY": ""})

    def test_legacy_stdio_cli_credentials_are_never_returned(self) -> None:
        legacy = SimpleNamespace(
            id="legacy-stdio",
            name="legacy stdio",
            transport="stdio",
            command="node",
            args=["server.js", "--token", "legacy-cli-secret"],
            url="",
            env={},
            headers={},
            enabled=False,
            created_at=datetime.now(timezone.utc),
        )
        result = mcp_router._out(legacy)
        self.assertEqual(result.command, "")
        self.assertEqual(result.args, [])
        self.assertNotIn("legacy-cli-secret", result.model_dump_json())

        malformed = SimpleNamespace(**{**legacy.__dict__, "args": "--token=malformed-secret"})
        malformed_result = mcp_router._out(malformed)
        self.assertEqual(malformed_result.command, "")
        self.assertEqual(malformed_result.args, [])
        self.assertNotIn("malformed-secret", malformed_result.model_dump_json())

    def test_stdio_import_preview_does_not_reflect_command_credentials(self) -> None:
        payload = MCPStandardImportIn.model_validate({
            "mcpServers": {
                "disabled-legacy": {
                    "type": "stdio",
                    "command": "Bearer import-preview-secret",
                    "disabled": True,
                }
            }
        })
        result = mcp_router.import_standard_mcp(
            payload,
            dry_run=True,
            conflict_policy="error",
            db=self.db,
        )
        self.assertEqual(result.items[0].endpoint, "")
        self.assertNotIn("import-preview-secret", result.model_dump_json())

    def test_blank_value_preserves_same_secret_and_omitted_key_deletes_it(self) -> None:
        previous = {"Authorization": "Bearer old", "X-Remove": "old"}
        self.assertEqual(
            mcp_router._merge_map(previous, {"Authorization": "", "X-New": "new"}),
            {"Authorization": "Bearer old", "X-New": "new"},
        )


class MCPRuntimeCompatibilityTests(unittest.TestCase):
    @staticmethod
    def pinned_target(port: int = 443) -> mcp_service._PinnedTarget:
        return mcp_service._PinnedTarget(
            scheme="https",
            hostname="example.test",
            port=port,
            authority="example.test" if port == 443 else f"example.test:{port}",
            address="93.184.216.34",
        )

    def test_streamable_http_uses_sdk_http_client_hook_with_headers(self) -> None:
        captured: dict[str, object] = {}

        class FakeHttpClient:
            def __init__(self, **kwargs):
                captured["http_kwargs"] = kwargs
                captured["created_http_client"] = self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        class FakeSession:
            def __init__(self, read, write):
                captured["streams"] = (read, write)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def initialize(self):
                return None

            async def list_tools(self):
                return SimpleNamespace(tools=[])

        @asynccontextmanager
        async def fake_streamable(url, *, http_client=None, terminate_on_close=True):
            captured["url"] = url
            captured["http_client"] = http_client
            yield "read", "write", lambda: None

        config = SimpleNamespace(
            transport="streamable_http",
            url="https://example.test/mcp",
            headers={"Authorization": "Bearer test-only"},
            command="",
            args=[],
            env={},
        )
        with (
            patch.object(httpx, "AsyncClient", FakeHttpClient),
            patch.object(mcp, "ClientSession", FakeSession),
            patch.object(streamable_http_module, "streamable_http_client", fake_streamable),
            patch.object(
                mcp_service,
                "_assert_safe_remote_target",
                return_value=self.pinned_target(),
            ),
        ):
            self.assertEqual(mcp_service.list_tools(config), [])

        self.assertEqual(captured["url"], "https://example.test/mcp")
        self.assertIs(captured["http_client"], captured["created_http_client"])
        self.assertEqual(
            captured["http_kwargs"]["headers"],
            {"Authorization": "Bearer test-only"},
        )
        self.assertIs(captured["http_kwargs"]["trust_env"], False)
        self.assertIs(captured["http_kwargs"]["follow_redirects"], False)
        transport = captured["http_kwargs"]["transport"]
        self.assertIsInstance(transport, mcp_service._PinnedAsyncHTTPTransport)
        self.assertEqual(transport._target, self.pinned_target())

    def test_sse_uses_same_pinned_no_proxy_http_client_factory(self) -> None:
        captured: dict[str, object] = {}

        class FakeHttpClient:
            def __init__(self, **kwargs):
                captured["http_kwargs"] = kwargs

        class FakeSession:
            def __init__(self, read, write):
                captured["streams"] = (read, write)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def initialize(self):
                return None

            async def list_tools(self):
                return SimpleNamespace(tools=[])

        @asynccontextmanager
        async def fake_sse(
            url,
            *,
            headers=None,
            sse_read_timeout=None,
            httpx_client_factory=None,
            **_kwargs,
        ):
            captured["url"] = url
            captured["sse_read_timeout"] = sse_read_timeout
            captured["factory_client"] = httpx_client_factory(
                headers=headers,
                timeout=httpx.Timeout(30.0),
                auth=None,
            )
            yield "read", "write"

        config = SimpleNamespace(
            transport="sse",
            url="https://example.test/sse",
            headers={"Authorization": "Bearer test-only"},
            command="",
            args=[],
            env={},
        )
        with (
            patch.object(httpx, "AsyncClient", FakeHttpClient),
            patch.object(mcp, "ClientSession", FakeSession),
            patch.object(sse_module, "sse_client", fake_sse),
            patch.object(
                mcp_service,
                "_assert_safe_remote_target",
                return_value=self.pinned_target(),
            ),
        ):
            self.assertEqual(mcp_service.list_tools(config), [])

        self.assertEqual(captured["url"], "https://example.test/sse")
        self.assertIsInstance(captured["factory_client"], FakeHttpClient)
        self.assertEqual(
            captured["http_kwargs"]["headers"],
            {"Authorization": "Bearer test-only"},
        )
        self.assertIs(captured["http_kwargs"]["trust_env"], False)
        self.assertIs(captured["http_kwargs"]["follow_redirects"], False)
        transport = captured["http_kwargs"]["transport"]
        self.assertIsInstance(transport, mcp_service._PinnedAsyncHTTPTransport)
        self.assertEqual(transport._target, self.pinned_target())

    def test_pinned_transport_rewrites_network_host_but_preserves_host_and_sni(self) -> None:
        captured: dict[str, object] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = request.url
            captured["host"] = request.headers.get("host")
            captured["sni_hostname"] = request.extensions.get("sni_hostname")
            return httpx.Response(200, text="ok")

        target = self.pinned_target(port=8443)
        transport = mcp_service._PinnedAsyncHTTPTransport(
            target,
            inner=httpx.MockTransport(handler),
        )

        async def exercise() -> None:
            async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
                response = await client.get("https://example.test:8443/mcp")
                self.assertEqual(response.status_code, 200)

        mcp_service._run(exercise())
        self.assertEqual(captured["url"].host, "93.184.216.34")
        self.assertEqual(captured["url"].port, 8443)
        self.assertEqual(captured["host"], "example.test:8443")
        self.assertEqual(captured["sni_hostname"], "example.test")

    def test_pinning_fails_closed_after_unverified_dependency_drift(self) -> None:
        with (
            patch.object(mcp_service.httpcore, "__version__", "2.0.0"),
            self.assertRaisesRegex(RuntimeError, "未经验证"),
        ):
            mcp_service._PinnedAsyncHTTPTransport(
                self.pinned_target(),
                inner=httpx.MockTransport(lambda _request: httpx.Response(200)),
            )

    def test_public_error_redacts_bearer_and_query_credentials(self) -> None:
        message = mcp_service.public_error(
            "request failed https://example.test/mcp?session=secret-query "
            "Authorization=secret-token with Bearer secret-bearer X-Auth: custom-secret",
            SimpleNamespace(headers={"X-Auth": "custom-secret"}, env={}),
        )
        self.assertNotIn("secret-query", message)
        self.assertNotIn("secret-token", message)
        self.assertNotIn("secret-bearer", message)
        self.assertNotIn("custom-secret", message)

    def test_runtime_target_policy_rejects_private_dns_answers(self) -> None:
        settings = SimpleNamespace(
            allow_insecure_mcp_http=False,
            mcp_private_host_allowlist="",
        )
        with (
            patch.object(mcp_service, "get_settings", return_value=settings),
            patch.object(
                mcp_service.socket,
                "getaddrinfo",
                return_value=[(2, 1, 6, "", ("127.0.0.1", 443))],
            ),
            self.assertRaisesRegex(ValueError, "私网"),
        ):
            mcp_service._assert_safe_remote_target("https://rebind.example.test/mcp")


if __name__ == "__main__":
    unittest.main()
