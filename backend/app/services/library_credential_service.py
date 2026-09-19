"""Domain-separated encryption for library connection passwords."""
from __future__ import annotations

import base64
import json
import secrets
from collections.abc import Mapping

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .workflow_payload_service import load_keyring


FIELD = "password_envelope"
CONTRACT = "library-password/v1"


def _key(root: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=CONTRACT.encode(), info=b"encryption").derive(root)


def _context(config: Mapping) -> bytes:
    return json.dumps({key: config.get(key) for key in ("host", "port", "database", "user")},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def seal_config(config: Mapping) -> dict:
    result = dict(config)
    password = result.pop("password", "")
    result.pop(FIELD, None)
    if not password:
        return result
    try:
        ring = load_keyring()
        nonce = secrets.token_bytes(12)
        encrypted = AESGCM(_key(ring.keys[ring.active_key_id])).encrypt(nonce, password.encode(), _context(result))
        result[FIELD] = {"contract": CONTRACT, "key_id": ring.active_key_id,
                         "nonce": base64.b64encode(nonce).decode(), "ciphertext": base64.b64encode(encrypted).decode()}
    except Exception as exc:
        raise ValueError("资料库凭据加密不可用，请管理员配置 WORKFLOW_PAYLOAD_ACTIVE_KEY_ID 和 WORKFLOW_PAYLOAD_ENCRYPTION_KEYS") from exc
    return result


def open_config(config: Mapping) -> dict:
    result = dict(config)
    envelope = result.pop(FIELD, None)
    if envelope is None:
        # Existing PostgreSQL rows remain readable and are sealed on their next
        # explicit edit; new public writes cannot supply an encrypted envelope.
        return result
    try:
        if not isinstance(envelope, dict) or set(envelope) != {"contract", "key_id", "nonce", "ciphertext"}:
            raise ValueError("invalid envelope")
        if envelope["contract"] != CONTRACT:
            raise ValueError("invalid contract")
        ring = load_keyring()
        nonce = base64.b64decode(envelope["nonce"], validate=True)
        ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        result["password"] = AESGCM(_key(ring.keys[envelope["key_id"]])).decrypt(nonce, ciphertext, _context(result)).decode()
    except Exception as exc:
        raise ValueError("资料库凭据不可用，请重新保存连接配置") from exc
    return result


def public_config(config: Mapping) -> dict:
    result = dict(config)
    result.pop("snapshot_file_id", None)
    result.pop("snapshot_sha256", None)
    configured = bool(result.pop(FIELD, None) or result.get("password"))
    for key in ("password", "api_key", "token", "secret", "access_token"):
        result.pop(key, None)
    result["password_configured"] = configured
    return result
