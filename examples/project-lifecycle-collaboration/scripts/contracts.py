"""Local value contracts; no platform, identity provider, or storage client."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

MAX_JSON_BYTES = 2 * 1024 * 1024
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "references" / "contract.json"


class ContractError(ValueError):
    pass


def _reject_constant(value: str) -> None:
    raise ContractError("non_finite_json")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate_json_key")
        result[key] = value
    return result


def load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ContractError("json_too_large")
    try:
        value = json.loads(raw, parse_constant=_reject_constant,
                           object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError) as exc:
        raise ContractError("invalid_json") from exc
    if not isinstance(value, dict):
        raise ContractError("object_required")
    return value


def validate(kind: str, value: dict[str, Any]) -> None:
    schema = load_json(SCHEMA_PATH)
    if kind not in schema["$defs"]:
        raise ContractError("unknown_contract")
    schema["$ref"] = f"#/$defs/{kind}"
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    error = next(validator.iter_errors(value), None)
    if error:
        # Do not print supplied values, customer text, or authentication evidence.
        raise ContractError(f"schema_invalid:{kind}:{error.validator}")


def index_by(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    result = {item[key]: item for item in items}
    if len(result) != len(items):
        raise ContractError(f"duplicate_identity:{key}")
    return result


def check_context(context: dict[str, Any]) -> None:
    validate("context", context)
    index_by(context["participants"], "participant_id")
    index_by(context["evidence"], "evidence_id")
    index_by(context["review_contents"], "review_id")


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def require_same(left: dict[str, Any], right: dict[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        if left[key] != right[key]:
            raise ContractError(f"binding_mismatch:{key}")
