from app.config import Settings
from app.services.auth_request_security import (
    LocalDevelopmentCORSMiddleware,
    allowed_cookie_origins,
    is_allowed_cookie_origin,
    is_local_development_origin,
)


def test_default_local_origins_cover_vite_fallback_port():
    settings = Settings(_env_file=None, postgresql_host="db", postgresql_user="app")

    assert allowed_cookie_origins(settings) == {
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    }


def test_development_accepts_any_ip_origin_without_fixed_ports():
    settings = Settings(_env_file=None, postgresql_host="db", postgresql_user="app")

    for origin in (
        "http://localhost:4173",
        "http://127.0.0.1:9000",
        "https://192.168.1.20:5173",
        "http://10.20.30.40:3000",
        "http://172.20.0.5:8080",
        "https://8.8.8.8:9443",
        "http://[::1]:5173",
    ):
        assert is_local_development_origin(origin)
        assert is_allowed_cookie_origin(settings, origin)


def test_development_accepts_ip_public_app_url():
    Settings(
        _env_file=None,
        postgresql_host="db",
        postgresql_user="app",
        public_app_url="http://192.168.1.20:5173",
    )


def test_development_does_not_accept_non_ip_or_malformed_origins():
    settings = Settings(_env_file=None, postgresql_host="db", postgresql_user="app")

    for origin in (
        "https://example.com:5173",
        "http://127.999.1.1:5173",
        "null",
    ):
        assert not is_local_development_origin(origin)
        assert not is_allowed_cookie_origin(settings, origin)


def test_production_requires_explicit_origin_for_private_lan():
    settings = Settings(
        _env_file=None,
        postgresql_host="db",
        postgresql_user="app",
        runtime_environment="prod",
        public_app_url="https://platform.example.com",
        auth_cookie_secure=True,
    )

    assert not is_allowed_cookie_origin(settings, "http://192.168.1.20:5173")
    assert is_allowed_cookie_origin(settings, "https://platform.example.com")


def test_cors_uses_the_same_local_origin_policy():
    middleware = LocalDevelopmentCORSMiddleware(
        lambda scope, receive, send: None,
        allow_local_development_origins=True,
        allow_credentials=True,
    )

    assert middleware.is_allowed_origin("http://192.168.1.20:5173")
    assert middleware.is_allowed_origin("http://localhost:4173")
    assert middleware.is_allowed_origin("https://8.8.8.8:9443")
    assert not middleware.is_allowed_origin("https://example.com:5173")
