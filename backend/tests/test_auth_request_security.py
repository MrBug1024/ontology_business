from app.config import Settings
from app.services.auth_request_security import allowed_cookie_origins


def test_default_local_origins_cover_vite_fallback_port():
    settings = Settings(_env_file=None, postgresql_host="db", postgresql_user="app")

    assert allowed_cookie_origins(settings) == {
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    }
