from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services import doc_parser


def _ocr_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "ocr_base_url": "https://ocr.example.test",
        "ocr_endpoint_path": "/v2/documents/parse",
        "ocr_allowed_hosts": "ocr.example.test",
        "ocr_private_host_allowlist": "",
        "ocr_api_key": "synthetic-ocr-key",
        "ocr_engine": "configured-engine",
        "ocr_language": "configured-language",
        "ocr_timeout_seconds": 37.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_ocr_settings_defaults_are_disabled_and_values_are_bounded() -> None:
    fields = Settings.model_fields
    assert fields["ocr_base_url"].default == ""
    assert fields["ocr_endpoint_path"].default == ""
    assert fields["ocr_allowed_hosts"].default == ""
    assert fields["ocr_private_host_allowlist"].default == ""
    assert fields["ocr_engine"].default == ""
    assert fields["ocr_language"].default == ""

    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+psycopg://user:password@db.example.test/platform",
            ocr_engine="x" * 129,
            _env_file=None,
        )


def test_incomplete_ocr_configuration_does_not_send_document() -> None:
    settings = _ocr_settings(ocr_endpoint_path="", ocr_engine="", ocr_language="")
    with (
        patch.object(doc_parser, "get_settings", return_value=settings),
        patch("httpx.Client") as client,
    ):
        result = doc_parser.parse_bytes(b"image-bytes", "scan.png")

    assert result == {
        "status": "error",
        "text": "",
        "message": "未配置 OCR 服务，无法解析图片",
    }
    client.assert_not_called()


@pytest.mark.parametrize(
    "overrides",
    (
        {"ocr_base_url": "http://ocr.example.test"},
        {"ocr_endpoint_path": "//other-host.example.test/v2/documents/parse"},
    ),
)
def test_unsafe_ocr_endpoint_configuration_fails_closed(
    overrides: dict[str, object],
) -> None:
    settings = _ocr_settings(**overrides)
    with (
        patch.object(doc_parser, "get_settings", return_value=settings),
        patch("httpx.Client") as client,
    ):
        result = doc_parser.parse_bytes(b"image-bytes", "scan.png")

    assert result["status"] == "error"
    assert result["message"] == "未配置 OCR 服务，无法解析图片"
    client.assert_not_called()


def test_unallowlisted_or_private_ocr_target_fails_closed() -> None:
    private_dns = [
        (
            doc_parser.socket.AF_INET,
            doc_parser.socket.SOCK_STREAM,
            6,
            "",
            ("127.0.0.1", 443),
        )
    ]
    settings = _ocr_settings()
    with (
        patch.object(doc_parser, "get_settings", return_value=settings),
        patch.object(doc_parser.socket, "getaddrinfo", return_value=private_dns),
        patch("httpx.Client") as client,
    ):
        private_result = doc_parser.parse_bytes(b"image-bytes", "scan.png")

    assert private_result["status"] == "error"
    assert private_result["message"] == "未配置 OCR 服务，无法解析图片"
    client.assert_not_called()

    settings = _ocr_settings(ocr_allowed_hosts="other.example.test")
    with (
        patch.object(doc_parser, "get_settings", return_value=settings),
        patch.object(doc_parser.socket, "getaddrinfo") as resolve,
        patch("httpx.Client") as client,
    ):
        unallowlisted_result = doc_parser.parse_bytes(
            b"image-bytes", "scan.png"
        )

    assert unallowlisted_result["status"] == "error"
    resolve.assert_not_called()
    client.assert_not_called()


def test_explicit_private_ocr_host_allowlist_is_pinned() -> None:
    private_dns = [
        (
            doc_parser.socket.AF_INET,
            doc_parser.socket.SOCK_STREAM,
            6,
            "",
            ("10.0.0.8", 443),
        )
    ]
    settings = _ocr_settings(
        ocr_private_host_allowlist="ocr.example.test",
    )
    with (
        patch.object(doc_parser, "get_settings", return_value=settings),
        patch.object(doc_parser.socket, "getaddrinfo", return_value=private_dns),
    ):
        config = doc_parser._ocr_configuration()

    assert config is not None
    assert config.target.hostname == "ocr.example.test"
    assert config.target.address == "10.0.0.8"


def test_configured_ocr_request_uses_exact_server_owned_values_and_tls() -> None:
    response = Mock(status_code=200)
    response.json.return_value = {"status": "success", "text": "parsed text"}
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.return_value = response
    public_dns = [
        (
            doc_parser.socket.AF_INET,
            doc_parser.socket.SOCK_STREAM,
            6,
            "",
            ("93.184.216.34", 443),
        )
    ]
    settings = _ocr_settings()
    with (
        patch.object(doc_parser, "get_settings", return_value=settings),
        patch.object(doc_parser.socket, "getaddrinfo", return_value=public_dns),
        patch.object(
            doc_parser,
            "_PinnedOCRTransport",
            return_value=Mock(),
        ) as transport_factory,
        patch("httpx.Client", return_value=client) as client_factory,
    ):
        result = doc_parser.parse_bytes(b"image-bytes", "scan.png")

    assert result["status"] == "success"
    assert result["text"] == "parsed text"
    transport_factory.assert_called_once()
    target = transport_factory.call_args.args[0]
    assert target.hostname == "ocr.example.test"
    assert target.address == "93.184.216.34"
    client_factory.assert_called_once_with(
        transport=transport_factory.return_value,
        timeout=37.0,
        verify=True,
        follow_redirects=False,
        trust_env=False,
    )
    client.post.assert_called_once()
    url = client.post.call_args.args[0]
    kwargs = client.post.call_args.kwargs
    assert url == "https://ocr.example.test/v2/documents/parse"
    assert kwargs["data"] == {
        "backend": "configured-engine",
        "lang_list": "configured-language",
        "table_enable": "false",
        "auto_rotate": "true",
    }
    assert kwargs["headers"] == {"Authorization": "synthetic-ocr-key"}


def test_pinned_ocr_transport_preserves_logical_tls_origin() -> None:
    class RecordingTransport(httpx.BaseTransport):
        def __init__(self) -> None:
            self.request: httpx.Request | None = None

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            self.request = request
            return httpx.Response(200, request=request)

    inner = RecordingTransport()
    target = doc_parser._OCRTarget(
        scheme="https",
        hostname="ocr.example.test",
        port=443,
        authority="ocr.example.test",
        address="93.184.216.34",
    )
    transport = doc_parser._PinnedOCRTransport(target, inner=inner)
    request = httpx.Request(
        "POST",
        "https://ocr.example.test/v2/documents/parse",
        content=b"body",
    )

    transport.handle_request(request)

    assert inner.request is not None
    assert inner.request.url.host == "93.184.216.34"
    assert inner.request.headers["Host"] == "ocr.example.test"
    assert inner.request.extensions["sni_hostname"] == "ocr.example.test"


def test_ocr_transport_failure_returns_stable_sanitized_error() -> None:
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.side_effect = RuntimeError(
        "synthetic-ocr-key at https://private-ocr.example.test failed"
    )
    public_dns = [
        (
            doc_parser.socket.AF_INET,
            doc_parser.socket.SOCK_STREAM,
            6,
            "",
            ("93.184.216.34", 443),
        )
    ]
    settings = _ocr_settings()
    with (
        patch.object(doc_parser, "get_settings", return_value=settings),
        patch.object(doc_parser.socket, "getaddrinfo", return_value=public_dns),
        patch.object(doc_parser, "_PinnedOCRTransport", return_value=Mock()),
        patch("httpx.Client", return_value=client),
    ):
        result = doc_parser._ocr_parse(b"image-bytes", "scan.png", is_image=True)

    assert result == {
        "status": "error",
        "text": "",
        "message": "OCR 服务请求失败",
    }
    assert "synthetic-ocr-key" not in result["message"]
    assert "private-ocr.example.test" not in result["message"]
