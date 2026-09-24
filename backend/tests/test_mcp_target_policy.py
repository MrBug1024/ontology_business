import ipaddress

import pytest

from app.schemas import MCPConfigIn
from app.services import mcp_service


def test_mcp_schema_accepts_private_and_loopback_urls():
    for url in (
        "https://127.0.0.1:9443/mcp",
        "https://10.20.30.40/mcp",
        "https://[::1]:9443/mcp",
    ):
        config = MCPConfigIn(name="internal", transport="streamable_http", url=url)
        assert config.url == url


def test_mcp_target_pins_private_dns_result(monkeypatch):
    monkeypatch.setattr(
        mcp_service.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("172.20.0.15", 443)),
        ],
    )

    target = mcp_service._assert_safe_remote_target("https://mcp.internal.example/mcp")

    assert target.hostname == "mcp.internal.example"
    assert target.address == "172.20.0.15"


def test_mcp_target_still_rejects_unusable_urls(monkeypatch):
    with pytest.raises(ValueError, match="用户凭据"):
        mcp_service._assert_safe_remote_target("https://user:secret@127.0.0.1/mcp")

    def fail_resolution(*_args, **_kwargs):
        raise OSError("synthetic DNS failure")

    monkeypatch.setattr(mcp_service.socket, "getaddrinfo", fail_resolution)
    with pytest.raises(ValueError, match="无法解析"):
        mcp_service._assert_safe_remote_target("https://missing.internal.example/mcp")


def test_mcp_target_literal_private_address_is_pinned():
    target = mcp_service._assert_safe_remote_target("https://10.0.0.8/mcp")

    assert target.address == str(ipaddress.ip_address("10.0.0.8"))
