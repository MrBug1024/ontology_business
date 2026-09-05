"""Domain-separated authenticated storage for durable Agent turn inputs."""
from __future__ import annotations

import base64
import copy
from dataclasses import dataclass
import json
import secrets
from typing import Any, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import workflow_payload_service


ENVELOPE_CONTRACT = "agent-turn-payload/v1"
ALGORITHM = "AES-256-GCM"
MAX_PAYLOAD_BYTES = 1_048_576


class AgentTurnPayloadError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SealedAgentTurnPayload:
    envelope: dict[str, str]
    summary: dict[str, Any]
    digest: str


def _canonical(value: Mapping[str, Any], *, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        document = copy.deepcopy(dict(value))
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AgentTurnPayloadError(
            "invalid_agent_turn_payload",
            f"{label}必须是可规范化 JSON 对象",
        ) from exc
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise AgentTurnPayloadError(
            "agent_turn_payload_too_large",
            f"{label}超过安全大小限制",
        )
    return document, encoded


def _derive(key: bytes, *, purpose: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"ontology-platform/agent-turn-payload/v1",
        info=purpose,
    ).derive(key)


def _digest(key: bytes, context_bytes: bytes, payload_bytes: bytes) -> str:
    signer = hmac.HMAC(_derive(key, purpose=b"input-digest"), hashes.SHA256())
    signer.update(b"agent-turn-input-digest/v1\x00")
    signer.update(context_bytes)
    signer.update(b"\x00")
    signer.update(payload_bytes)
    return signer.finalize().hex()


def _summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    inputs = payload.get("inputs") if isinstance(payload.get("inputs"), Mapping) else {}
    managed = payload.get("managed_inputs")
    attachments = payload.get("attachments")
    capability = payload.get("capability")
    return {
        "format": "agent-turn-input-summary/v1",
        "input_fields": sorted(str(key)[:180] for key in inputs)[:200],
        "managed_input_count": len(managed) if isinstance(managed, list) else 0,
        "attachment_count": len(attachments) if isinstance(attachments, list) else 0,
        "has_capability_target": isinstance(capability, Mapping),
    }


def _aad(context: Mapping[str, Any], summary: Mapping[str, Any], digest: str) -> bytes:
    _, encoded = _canonical(
        {"context": dict(context), "summary": dict(summary), "digest": digest},
        label="Agent Turn 认证上下文",
    )
    return encoded


def seal_payload(
    payload: Mapping[str, Any],
    *,
    context: Mapping[str, Any],
) -> SealedAgentTurnPayload:
    keyring = workflow_payload_service.load_keyring()
    plain, payload_bytes = _canonical(payload, label="Agent Turn 输入")
    _, context_bytes = _canonical(context, label="Agent Turn 上下文")
    root_key = keyring.keys[keyring.active_key_id]
    encryption_key = _derive(root_key, purpose=b"encryption")
    digest = _digest(root_key, context_bytes, payload_bytes)
    summary = _summary(plain)
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(encryption_key).encrypt(
        nonce,
        payload_bytes,
        _aad(context, summary, digest),
    )
    return SealedAgentTurnPayload(
        envelope={
            "contract": ENVELOPE_CONTRACT,
            "alg": ALGORITHM,
            "key_id": keyring.active_key_id,
            "nonce": base64.urlsafe_b64encode(nonce).decode("ascii").rstrip("="),
            "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii").rstrip("="),
        },
        summary=summary,
        digest=digest,
    )


def _decode(value: str) -> bytes:
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as exc:
        raise AgentTurnPayloadError(
            "invalid_agent_turn_payload_envelope",
            "Agent Turn 输入载荷编码无效",
        ) from exc


def open_payload(
    envelope: Mapping[str, Any],
    *,
    context: Mapping[str, Any],
    summary: Mapping[str, Any],
    digest: str,
) -> dict[str, Any]:
    expected_keys = {"contract", "alg", "key_id", "nonce", "ciphertext"}
    if set(envelope) != expected_keys:
        raise AgentTurnPayloadError(
            "invalid_agent_turn_payload_envelope",
            "Agent Turn 输入不是受支持的加密信封",
        )
    if envelope.get("contract") != ENVELOPE_CONTRACT or envelope.get("alg") != ALGORITHM:
        raise AgentTurnPayloadError(
            "invalid_agent_turn_payload_envelope",
            "Agent Turn 输入载荷版本或算法不受支持",
        )
    keyring = workflow_payload_service.load_keyring()
    key_id = str(envelope.get("key_id") or "")
    root_key = keyring.keys.get(key_id)
    if root_key is None:
        raise AgentTurnPayloadError(
            "agent_turn_payload_key_unavailable",
            "当前部署缺少该 Agent Turn 所需的历史解密密钥",
        )
    nonce = _decode(str(envelope.get("nonce") or ""))
    ciphertext = _decode(str(envelope.get("ciphertext") or ""))
    if len(nonce) != 12 or len(ciphertext) < 16:
        raise AgentTurnPayloadError(
            "invalid_agent_turn_payload_envelope",
            "Agent Turn 输入载荷加密参数无效",
        )
    try:
        payload_bytes = AESGCM(_derive(root_key, purpose=b"encryption")).decrypt(
            nonce,
            ciphertext,
            _aad(context, summary, digest),
        )
        payload = json.loads(payload_bytes)
    except (InvalidTag, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AgentTurnPayloadError(
            "agent_turn_payload_authentication_failed",
            "Agent Turn 输入载荷认证失败，已阻止执行",
        ) from exc
    if not isinstance(payload, dict):
        raise AgentTurnPayloadError(
            "invalid_agent_turn_payload_envelope",
            "Agent Turn 输入解封后不是 JSON 对象",
        )
    _, context_bytes = _canonical(context, label="Agent Turn 上下文")
    expected_digest = _digest(root_key, context_bytes, payload_bytes)
    if not secrets.compare_digest(expected_digest, str(digest or "")):
        raise AgentTurnPayloadError(
            "agent_turn_payload_authentication_failed",
            "Agent Turn 输入摘要校验失败，已阻止执行",
        )
    return copy.deepcopy(payload)


__all__ = [
    "AgentTurnPayloadError",
    "SealedAgentTurnPayload",
    "open_payload",
    "seal_payload",
]
