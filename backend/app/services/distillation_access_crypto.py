"""Domain-separated AEAD bound to workspace, project, grant and exact target."""
from __future__ import annotations

import base64
import json
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .workflow_payload_service import load_keyring


DOMAIN = b"distillation-system-access/v1"


def _key(root: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=DOMAIN, info=b"credentials").derive(root)


def _context(row) -> bytes:
    return DOMAIN + json.dumps([row.tenant_id, row.project_id, row.id, row.target_hash, row.auth_type], separators=(",", ":")).encode()


def seal(row, username: str, secret: str) -> dict:
    try:
        ring = load_keyring()
        nonce = secrets.token_bytes(12)
        plaintext = json.dumps({"username": username, "secret": secret}).encode()
        value = AESGCM(_key(ring.keys[ring.active_key_id])).encrypt(nonce, plaintext, _context(row))
        return {"key_id": ring.active_key_id, "nonce": base64.b64encode(nonce).decode(), "ciphertext": base64.b64encode(value).decode()}
    except Exception as exc:
        raise ValueError("系统调查凭据加密不可用，请管理员配置工作流负载加密密钥") from exc


def credentials(row) -> dict[str, str]:
    try:
        ring = load_keyring()
        envelope = row.credential_envelope
        raw = AESGCM(_key(ring.keys[envelope["key_id"]])).decrypt(base64.b64decode(envelope["nonce"], validate=True),
            base64.b64decode(envelope["ciphertext"], validate=True), _context(row))
        return json.loads(raw)
    except Exception as exc:
        raise ValueError("系统调查凭据不可用，请重新授权") from exc


def authorization_header(row) -> str:
    value = credentials(row)
    if row.auth_type == "basic":
        return "Basic " + base64.b64encode((value["username"] + ":" + value["secret"]).encode()).decode()
    if row.auth_type == "bearer":
        return "Bearer " + value["secret"]
    raise ValueError("该系统使用网页登录，请调用浏览器调查工具")
