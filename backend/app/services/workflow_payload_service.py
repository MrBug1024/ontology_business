"""Authenticated storage boundary for durable asynchronous workflow inputs.

Only this module may turn a caller's workflow parameter document into the
database envelope consumed by a worker.  Keys remain deployment configuration;
the database stores only a key identifier, AES-256-GCM nonce/ciphertext, a
keyed audit digest and a value-free public summary.

There is intentionally no plaintext or hash-only compatibility fallback.  A
missing key, altered context, malformed legacy row or invalid authentication
tag fails closed before a workflow can execute.
"""
from __future__ import annotations

import base64
import binascii
import copy
import json
import re
import secrets
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..config import get_settings


ENVELOPE_CONTRACT = "workflow-run-input-envelope/v1"
SUMMARY_CONTRACT = "workflow-run-input-summary/v1"
AAD_CONTRACT = "workflow-run-input-aad/v1"
ALGORITHM = "A256GCM"
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_KEYS = 16


class WorkflowPayloadError(RuntimeError):
    """Safe, stable error raised before plaintext can cross the storage edge."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code or "workflow_payload_error")
        self.message = str(message or "工作流输入载荷不可用")


@dataclass(frozen=True, slots=True)
class WorkflowPayloadKeyring:
    active_key_id: str
    keys: Mapping[str, bytes]


@dataclass(frozen=True, slots=True)
class SealedWorkflowPayload:
    envelope: dict[str, str]
    summary: dict[str, Any]
    digest: str


def _canonical_document(value: Any, *, label: str) -> tuple[dict[str, Any], bytes]:
    if not isinstance(value, Mapping):
        raise WorkflowPayloadError(
            "invalid_workflow_payload",
            f"{label}必须是 JSON 对象",
        )
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        plain = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WorkflowPayloadError(
            "invalid_workflow_payload",
            f"{label}必须是可序列化的 JSON 对象",
        ) from exc
    if not isinstance(plain, dict):
        raise WorkflowPayloadError(
            "invalid_workflow_payload",
            f"{label}必须是 JSON 对象",
        )
    return plain, encoded


def _decode_base64(
    value: str,
    *,
    label: str,
    error_code: str = "invalid_key_configuration",
) -> bytes:
    text = str(value or "").strip()
    if not text:
        raise WorkflowPayloadError(error_code, f"{label}不能为空")
    padding = "=" * (-len(text) % 4)
    try:
        return base64.b64decode(
            (text + padding).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise WorkflowPayloadError(
            error_code,
            f"{label}必须是 URL-safe base64",
        ) from exc


def _encode_base64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _key_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkflowPayloadError(
                "invalid_key_configuration",
                "工作流载荷密钥配置包含重复 key id",
            )
        result[key] = value
    return result


def load_keyring(settings: Any | None = None) -> WorkflowPayloadKeyring:
    """Load a bounded rotation-aware key ring without ever returning secrets."""

    settings = settings or get_settings()
    raw = str(getattr(settings, "workflow_payload_encryption_keys", "") or "").strip()
    active = str(getattr(settings, "workflow_payload_active_key_id", "") or "").strip()
    if not raw or not active:
        raise WorkflowPayloadError(
            "workflow_payload_key_unconfigured",
            "工作流输入加密密钥未配置，已阻止持久化或执行",
        )
    try:
        document = json.loads(raw, object_pairs_hook=_key_pairs)
    except WorkflowPayloadError:
        raise
    except json.JSONDecodeError as exc:
        raise WorkflowPayloadError(
            "invalid_key_configuration",
            "工作流载荷密钥环必须是 JSON 对象",
        ) from exc
    if not isinstance(document, dict) or not document or len(document) > _MAX_KEYS:
        raise WorkflowPayloadError(
            "invalid_key_configuration",
            f"工作流载荷密钥环必须包含 1 至 {_MAX_KEYS} 个密钥",
        )
    keys: dict[str, bytes] = {}
    for key_id, encoded_key in document.items():
        if not _KEY_ID_RE.fullmatch(str(key_id)) or not isinstance(encoded_key, str):
            raise WorkflowPayloadError(
                "invalid_key_configuration",
                "工作流载荷 key id 或密钥格式无效",
            )
        key = _decode_base64(encoded_key, label=f"工作流载荷密钥 {key_id}")
        if len(key) != 32:
            raise WorkflowPayloadError(
                "invalid_key_configuration",
                "工作流载荷密钥必须解码为 32 字节 AES-256 密钥",
            )
        keys[str(key_id)] = key
    if active not in keys:
        raise WorkflowPayloadError(
            "invalid_key_configuration",
            "当前工作流载荷 key id 不在已配置密钥环中",
        )
    return WorkflowPayloadKeyring(active_key_id=active, keys=keys)


def _run_context(
    *,
    run_id: str,
    scenario_id: str,
    workflow_id: str,
    environment: str,
    definition_hash: str,
) -> dict[str, str]:
    values = {
        "contract": AAD_CONTRACT,
        "run_id": str(run_id or "").strip(),
        "scenario_id": str(scenario_id or "").strip(),
        "workflow_id": str(workflow_id or "").strip(),
        "environment": str(environment or "").strip(),
        "definition_hash": str(definition_hash or "").strip(),
    }
    if any(not values[key] for key in ("run_id", "scenario_id", "workflow_id", "environment")):
        raise WorkflowPayloadError(
            "invalid_workflow_payload_context",
            "工作流输入载荷缺少稳定运行上下文",
        )
    return values


def payload_context(
    *,
    run_id: str,
    scenario_id: str,
    workflow_id: str,
    environment: str,
    definition_hash: str,
) -> dict[str, str]:
    """Build the stable authenticated context used by runtime and migration."""

    return _run_context(
        run_id=run_id,
        scenario_id=scenario_id,
        workflow_id=workflow_id,
        environment=environment,
        definition_hash=definition_hash,
    )


def _context_for_run(run: Any) -> dict[str, str]:
    return _run_context(
        run_id=str(getattr(run, "id", "") or ""),
        scenario_id=str(getattr(run, "scenario_id", "") or ""),
        workflow_id=str(getattr(run, "workflow_id", "") or ""),
        environment=str(getattr(run, "environment", "") or ""),
        definition_hash=str(getattr(run, "definition_hash", "") or ""),
    )


def _value_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list):
        return "array"
    return "string"


def _summary(payload: Mapping[str, Any], *, digest: str = "") -> dict[str, Any]:
    kinds = Counter(_value_kind(value) for value in payload.values())
    result: dict[str, Any] = {
        "contract": SUMMARY_CONTRACT,
        "redacted": True,
        "payload_available": True,
        "payload_present": bool(payload),
        "field_count": len(payload),
        "value_type_counts": dict(sorted(kinds.items())),
    }
    if digest:
        result["payload_fingerprint"] = digest
    return result


def summarize_for_public(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a value-free projection suitable for APIs and rendered logs."""

    try:
        plain, _ = _canonical_document(payload or {}, label="输入参数")
    except WorkflowPayloadError:
        return {
            "contract": SUMMARY_CONTRACT,
            "redacted": True,
            "payload_available": False,
            "payload_present": False,
            "field_count": 0,
            "value_type_counts": {},
        }
    return _summary(plain)


def _fingerprint_key(key: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"ontology-platform/workflow-payload/v1",
        info=b"workflow-payload-fingerprint",
    ).derive(key)


def _digest(key: bytes, context_bytes: bytes, payload_bytes: bytes) -> str:
    signer = hmac.HMAC(_fingerprint_key(key), hashes.SHA256())
    signer.update(b"workflow-run-input-digest/v1\x00")
    signer.update(context_bytes)
    signer.update(b"\x00")
    signer.update(payload_bytes)
    return signer.finalize().hex()


def _aad_bytes(
    context: Mapping[str, Any],
    summary: Mapping[str, Any],
    digest: str,
) -> bytes:
    _, encoded = _canonical_document(
        {
            "context": dict(context),
            "input_digest": digest,
            "input_summary": dict(summary),
        },
        label="工作流载荷认证上下文",
    )
    return encoded


def seal_payload(
    payload: Mapping[str, Any],
    *,
    context: Mapping[str, Any],
    keyring: WorkflowPayloadKeyring | None = None,
) -> SealedWorkflowPayload:
    """Seal one JSON object using AES-256-GCM and context-bound AAD."""

    keyring = keyring or load_keyring()
    context_document, context_bytes = _canonical_document(
        context,
        label="工作流载荷上下文",
    )
    plain, payload_bytes = _canonical_document(payload, label="工作流输入参数")
    key = keyring.keys[keyring.active_key_id]
    digest = _digest(key, context_bytes, payload_bytes)
    summary = _summary(plain, digest=digest)
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(
        nonce,
        payload_bytes,
        _aad_bytes(context_document, summary, digest),
    )
    return SealedWorkflowPayload(
        envelope={
            "contract": ENVELOPE_CONTRACT,
            "alg": ALGORITHM,
            "key_id": keyring.active_key_id,
            "nonce": _encode_base64(nonce),
            "ciphertext": _encode_base64(ciphertext),
        },
        summary=summary,
        digest=digest,
    )


def open_payload(
    envelope: Mapping[str, Any],
    *,
    context: Mapping[str, Any],
    summary: Mapping[str, Any],
    digest: str,
    keyring: WorkflowPayloadKeyring | None = None,
) -> dict[str, Any]:
    """Authenticate and decrypt a stored envelope, never accepting plaintext."""

    keyring = keyring or load_keyring()
    if not isinstance(envelope, Mapping) or set(envelope) != {
        "contract",
        "alg",
        "key_id",
        "nonce",
        "ciphertext",
    }:
        raise WorkflowPayloadError(
            "invalid_workflow_payload_envelope",
            "工作流输入载荷不是受支持的加密信封",
        )
    if envelope.get("contract") != ENVELOPE_CONTRACT or envelope.get("alg") != ALGORITHM:
        raise WorkflowPayloadError(
            "invalid_workflow_payload_envelope",
            "工作流输入载荷版本或算法不受支持",
        )
    key_id = str(envelope.get("key_id") or "")
    key = keyring.keys.get(key_id)
    if key is None:
        raise WorkflowPayloadError(
            "workflow_payload_key_unavailable",
            "当前部署缺少该工作流输入载荷所需的历史解密密钥",
        )
    if not _SHA256_RE.fullmatch(str(digest or "")):
        raise WorkflowPayloadError(
            "invalid_workflow_payload_envelope",
            "工作流输入载荷摘要无效",
        )
    context_document, context_bytes = _canonical_document(
        context,
        label="工作流载荷上下文",
    )
    summary_document, _ = _canonical_document(
        summary,
        label="工作流载荷摘要",
    )
    nonce = _decode_base64(
        str(envelope.get("nonce") or ""),
        label="工作流载荷 nonce",
        error_code="invalid_workflow_payload_envelope",
    )
    ciphertext = _decode_base64(
        str(envelope.get("ciphertext") or ""),
        label="工作流载荷 ciphertext",
        error_code="invalid_workflow_payload_envelope",
    )
    if len(nonce) != 12 or len(ciphertext) < 16:
        raise WorkflowPayloadError(
            "invalid_workflow_payload_envelope",
            "工作流输入载荷加密参数无效",
        )
    try:
        payload_bytes = AESGCM(key).decrypt(
            nonce,
            ciphertext,
            _aad_bytes(context_document, summary_document, str(digest)),
        )
        payload = json.loads(payload_bytes)
    except (InvalidTag, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WorkflowPayloadError(
            "workflow_payload_authentication_failed",
            "工作流输入载荷认证失败，已阻止执行",
        ) from exc
    if not isinstance(payload, dict):
        raise WorkflowPayloadError(
            "invalid_workflow_payload_envelope",
            "工作流输入载荷解封后不是 JSON 对象",
        )
    expected = _digest(key, context_bytes, payload_bytes)
    if not secrets.compare_digest(expected, str(digest)):
        raise WorkflowPayloadError(
            "workflow_payload_authentication_failed",
            "工作流输入载荷摘要校验失败，已阻止执行",
        )
    return copy.deepcopy(payload)


def seal_workflow_run_input(run: Any, payload: Mapping[str, Any]) -> None:
    sealed = seal_payload(payload, context=_context_for_run(run))
    run.input_payload = sealed.envelope
    run.input_summary = sealed.summary
    run.input_digest = sealed.digest


def open_workflow_run_input(run: Any) -> dict[str, Any]:
    return open_payload(
        getattr(run, "input_payload", None) or {},
        context=_context_for_run(run),
        summary=getattr(run, "input_summary", None) or {},
        digest=str(getattr(run, "input_digest", "") or ""),
    )


def public_input_summary(run: Any) -> dict[str, Any]:
    """Project only known value-free fields from the persisted summary."""

    raw = getattr(run, "input_summary", None)
    if not isinstance(raw, Mapping) or raw.get("contract") != SUMMARY_CONTRACT:
        return {
            "contract": SUMMARY_CONTRACT,
            "redacted": True,
            "payload_available": False,
            "payload_present": False,
            "field_count": 0,
            "value_type_counts": {},
        }
    counts = raw.get("value_type_counts")
    safe_counts = {
        key: max(0, value)
        for key, value in (counts.items() if isinstance(counts, Mapping) else [])
        if key in {"null", "boolean", "integer", "number", "string", "array", "object"}
        and isinstance(value, int)
        and not isinstance(value, bool)
    }
    field_count = raw.get("field_count", 0)
    if not isinstance(field_count, int) or isinstance(field_count, bool):
        field_count = 0
    result = {
        "contract": SUMMARY_CONTRACT,
        "redacted": True,
        "payload_available": bool(raw.get("payload_available", False)),
        "payload_present": bool(raw.get("payload_present", False)),
        "field_count": max(0, field_count),
        "value_type_counts": dict(sorted(safe_counts.items())),
    }
    fingerprint = str(raw.get("payload_fingerprint") or "")
    if _SHA256_RE.fullmatch(fingerprint):
        result["payload_fingerprint"] = fingerprint
    return result


__all__ = [
    "ALGORITHM",
    "ENVELOPE_CONTRACT",
    "SUMMARY_CONTRACT",
    "SealedWorkflowPayload",
    "WorkflowPayloadError",
    "WorkflowPayloadKeyring",
    "load_keyring",
    "open_payload",
    "open_workflow_run_input",
    "payload_context",
    "public_input_summary",
    "seal_payload",
    "seal_workflow_run_input",
    "summarize_for_public",
]
