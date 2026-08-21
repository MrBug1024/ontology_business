"""Read-only access to code-versioned, governed ontology starter kits.

Starter kits are deliberately static assets rather than tenant data.  Before an
asset is made available, this service checks all of the following server-side:

* only catalogued directories and fixed filenames can be read;
* both asset manifests agree with a freshly recomputed SHA-256 fingerprint;
* sensitive values, connector configuration and runtime/business data are
  rejected rather than silently redacted;
* the payload remains compatible with ``package_service.validate_package``.

The returned package is a fresh deep copy.  Callers can pass it to the existing
read-only package preview/proposal workflow, but cannot mutate the static source
or bypass the package governance boundary.
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..starter_kit_schemas import StarterKitArtifact, StarterKitSummary
from . import package_service


STARTER_KIT_ROOT = (Path(__file__).resolve().parent.parent / "starter_kits").resolve()
STARTER_KIT_IDS = ("retail", "bookkeeping", "supply-chain")

_MANIFEST_FILE = "manifest.json"
_PACKAGE_FILE = "package.json"
_KIT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_MANIFEST_FIELDS = frozenset({
    "id",
    "name",
    "industry",
    "version",
    "description",
    "package_file",
    "fingerprint",
    "resource_counts",
})
_PACKAGE_TOP_LEVEL_FIELDS = frozenset({"format", "version", "manifest", "resources"})

# These field names describe connection targets or executable connector state.
# A starter kit can declare ontology concepts, but it must not embed a target that
# becomes executable simply because a tenant imports it.
_FORBIDDEN_CONFIGURATION_FIELDS = frozenset({
    "config",
    "configuration",
    "url",
    "uri",
    "endpoint",
    "baseurl",
    "baseuri",
    "host",
    "hostname",
    "port",
    "dsn",
    "connectionstring",
    "connectionurl",
    "databaseurl",
    "jdbcurl",
    "datasourceurl",
    "datasourceconfig",
    "connectionconfig",
    "connectorconfig",
    "connectorurl",
    "mcpconfig",
    "llmconfig",
    "runtimeconfig",
    "headers",
    "cookies",
})
_CONNECTION_URL_PATTERN = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]*://|jdbc:[a-z])")

# Runtime history and concrete business records are intentionally outside the
# ontology-resource-package format.  Keep the list specific so valid declarative
# event schemas (``resources.events``) remain possible.
_FORBIDDEN_RUNTIME_DATA_FIELDS = frozenset({
    "objects",
    "instances",
    "records",
    "rows",
    "rawdata",
    "sampledata",
    "fixturedata",
    "executionlog",
    "executionlogs",
    "runtimelog",
    "runtimelogs",
    "workflowruns",
    "actionexecutionlogs",
    "eventenvelopes",
})


class StarterKitError(ValueError):
    """Base error for a starter-kit asset that cannot be safely used."""


class StarterKitNotFoundError(StarterKitError):
    """The requested identifier is not part of the fixed starter-kit catalog."""


class StarterKitIntegrityError(StarterKitError):
    """A static asset is malformed, altered, or inconsistent with its manifest."""


class StarterKitSafetyError(StarterKitError):
    """A starter kit attempts to carry secrets, connector configuration, or data."""

    def __init__(self, code: str, path: str) -> None:
        self.code = code
        self.path = path
        super().__init__(f"Starter Kit 安全校验未通过: {code} ({path})")


class StarterKitValidationError(StarterKitError):
    """The resource package fails the platform's portable package contract."""

    def __init__(self, errors: list[Mapping[str, Any]]) -> None:
        self.errors = [dict(error) for error in errors]
        codes = ", ".join(str(error.get("code", "invalid_package")) for error in self.errors)
        super().__init__(f"Starter Kit 与资源包契约不兼容: {codes}")


def _field_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StarterKitIntegrityError("Starter Kit JSON 包含重复字段")
        result[key] = value
    return result


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StarterKitIntegrityError("无法读取 Starter Kit 静态资产") from exc
    try:
        value = json.loads(raw, object_pairs_hook=_json_object_without_duplicates)
    except (json.JSONDecodeError, StarterKitIntegrityError) as exc:
        if isinstance(exc, StarterKitIntegrityError):
            raise
        raise StarterKitIntegrityError("Starter Kit JSON 格式无效") from exc
    if not isinstance(value, dict):
        raise StarterKitIntegrityError("Starter Kit JSON 根节点必须是对象")
    return value


def _kit_directory(starter_kit_id: str) -> Path:
    identifier = str(starter_kit_id or "").strip()
    if not _KIT_ID_PATTERN.fullmatch(identifier) or identifier not in STARTER_KIT_IDS:
        raise StarterKitNotFoundError("未找到指定的 Starter Kit")
    root = STARTER_KIT_ROOT.resolve()
    directory = (root / identifier).resolve()
    if root not in directory.parents or directory.parent != root:
        raise StarterKitIntegrityError("Starter Kit 路径无效")
    return directory


def _require_string(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise StarterKitIntegrityError(f"Starter Kit manifest.{field} 无效")
    return value


def _validate_resource_counts(value: Any, field: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise StarterKitIntegrityError(f"Starter Kit {field} 必须是对象")
    actual: dict[str, int] = {}
    expected = set(package_service.RESOURCE_KINDS)
    if {str(key) for key in value} != expected:
        raise StarterKitIntegrityError(f"Starter Kit {field} 必须覆盖全部资源集合")
    for kind in package_service.RESOURCE_KINDS:
        count = value.get(kind)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise StarterKitIntegrityError(f"Starter Kit {field}.{kind} 必须是非负整数")
        actual[kind] = count
    return actual


def _validate_catalog_manifest(value: Mapping[str, Any], starter_kit_id: str) -> dict[str, Any]:
    unexpected = {str(key) for key in value} - _MANIFEST_FIELDS
    missing = _MANIFEST_FIELDS - {str(key) for key in value}
    if unexpected or missing:
        raise StarterKitIntegrityError("Starter Kit manifest 字段不完整或包含未支持字段")
    manifest = dict(value)
    if _require_string(manifest.get("id"), "id") != starter_kit_id:
        raise StarterKitIntegrityError("Starter Kit manifest.id 与目录不一致")
    for field in ("name", "industry", "version"):
        _require_string(manifest.get(field), field)
    _require_string(manifest.get("description"), "description", allow_empty=True)
    if manifest.get("package_file") != _PACKAGE_FILE:
        raise StarterKitIntegrityError("Starter Kit 必须使用固定 package.json 资产")
    fingerprint = _require_string(manifest.get("fingerprint"), "fingerprint")
    if not _FINGERPRINT_PATTERN.fullmatch(fingerprint):
        raise StarterKitIntegrityError("Starter Kit manifest.fingerprint 格式无效")
    manifest["resource_counts"] = _validate_resource_counts(
        manifest.get("resource_counts"), "manifest.resource_counts"
    )
    return manifest


def _sensitive_path(value: Any, path: str = "$") -> str | None:
    """Return a safe JSON path if package_service would redact supplied content."""
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}"
            # Use the same normalizer/classifier as portable package handling so
            # changes there cannot let a secret through this static boundary.
            if package_service._is_sensitive_key(key):  # noqa: SLF001 - shared policy
                return child_path
            nested = _sensitive_path(child, child_path)
            if nested:
                return nested
        return None
    if isinstance(value, list):
        for index, child in enumerate(value):
            nested = _sensitive_path(child, f"{path}[{index}]")
            if nested:
                return nested
        return None
    if isinstance(value, str) and package_service.redact_sensitive(value) != value:
        return path
    return None


def _forbidden_static_content_path(value: Any, path: str = "$") -> tuple[str, str] | None:
    """Identify connector/runtime payloads that must never be bundled in a kit."""
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            token = _field_token(key)
            child_path = f"{path}.{key}"
            if token in _FORBIDDEN_CONFIGURATION_FIELDS:
                return ("connector_configuration_forbidden", child_path)
            if token in _FORBIDDEN_RUNTIME_DATA_FIELDS:
                return ("runtime_or_business_data_forbidden", child_path)
            if token == "executorconfig" and child not in ({}, None):
                return ("connector_configuration_forbidden", child_path)
            if token == "triggerconfig" and child not in ({}, None):
                return ("connector_configuration_forbidden", child_path)
            nested = _forbidden_static_content_path(child, child_path)
            if nested:
                return nested
        return None
    if isinstance(value, list):
        for index, child in enumerate(value):
            nested = _forbidden_static_content_path(child, f"{path}[{index}]")
            if nested:
                return nested
    elif isinstance(value, str) and _CONNECTION_URL_PATTERN.search(value):
        # Configuration can be hidden below a neutral key or prose field.  No
        # URL-like connection target belongs in a static starter-kit artifact.
        return ("connector_configuration_forbidden", path)
    return None


def validate_starter_kit_package(package: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Reject unsafe starter-kit content, then apply the portable package contract.

    Unlike generic package validation, this function intentionally *does not*
    redact and continue.  Static, repository-owned starter assets must fail
    closed if they contain a secret or a runtime configuration/data field.
    """
    if not isinstance(package, Mapping):
        raise StarterKitValidationError([{
            "code": "invalid_package",
            "path": "",
            "message": "资源包必须是 JSON 对象",
        }])
    root_fields = {str(key) for key in package}
    unsupported = root_fields - _PACKAGE_TOP_LEVEL_FIELDS
    if unsupported:
        raise StarterKitSafetyError("unsupported_package_root", "$." + sorted(unsupported)[0])
    secret_path = _sensitive_path(package)
    if secret_path:
        raise StarterKitSafetyError("sensitive_content_forbidden", secret_path)
    forbidden = _forbidden_static_content_path(package)
    if forbidden:
        raise StarterKitSafetyError(*forbidden)
    validation = package_service.validate_package(package)
    if not validation["valid"]:
        raise StarterKitValidationError(validation["errors"])
    return validation


def _resource_counts_from_package(package: Mapping[str, Any]) -> dict[str, int]:
    resources = package.get("resources")
    if not isinstance(resources, Mapping):
        raise StarterKitIntegrityError("Starter Kit package.resources 无效")
    counts: dict[str, int] = {}
    for kind in package_service.RESOURCE_KINDS:
        values = resources.get(kind)
        if not isinstance(values, list):
            raise StarterKitIntegrityError(f"Starter Kit package.resources.{kind} 无效")
        counts[kind] = len(values)
    return counts


def _verified_artifact(starter_kit_id: str) -> StarterKitArtifact:
    directory = _kit_directory(starter_kit_id)
    catalog_manifest = _validate_catalog_manifest(
        _read_json_object(directory / _MANIFEST_FILE), starter_kit_id
    )
    package = _read_json_object(directory / _PACKAGE_FILE)
    validation = validate_starter_kit_package(package)

    normalized_package = validation["normalized"]
    package_manifest = normalized_package.get("manifest")
    if not isinstance(package_manifest, Mapping):
        raise StarterKitIntegrityError("Starter Kit package.manifest 无效")
    package_manifest = dict(package_manifest)
    # Recompute from the repository asset rather than trusting either manifest.
    # Static resources are kept in the same canonical order as package_service,
    # so this raw package SHA-256 must agree with its normalized validation hash.
    computed_fingerprint = package_service.package_fingerprint(package)
    package_fingerprint = package_manifest.get("fingerprint")
    if not isinstance(package_fingerprint, str) or package_fingerprint != computed_fingerprint:
        raise StarterKitIntegrityError("Starter Kit package 指纹不匹配")
    if catalog_manifest["fingerprint"] != computed_fingerprint:
        raise StarterKitIntegrityError("Starter Kit manifest 指纹不匹配")
    if validation["fingerprint"] != computed_fingerprint:
        raise StarterKitIntegrityError("Starter Kit 指纹校验结果不一致")
    expected_counts = _resource_counts_from_package(normalized_package)
    if catalog_manifest["resource_counts"] != expected_counts:
        raise StarterKitIntegrityError("Starter Kit manifest 资源计数不匹配")
    if package_manifest.get("resource_counts") != expected_counts:
        raise StarterKitIntegrityError("Starter Kit package 资源计数不匹配")
    if package_manifest.get("starter_kit_id") != starter_kit_id:
        raise StarterKitIntegrityError("Starter Kit package 标识不匹配")
    if package_manifest.get("kit_version") != catalog_manifest["version"]:
        raise StarterKitIntegrityError("Starter Kit package 版本不匹配")
    for field in ("name", "industry", "description"):
        if package_manifest.get(field) != catalog_manifest[field]:
            raise StarterKitIntegrityError(f"Starter Kit package manifest.{field} 不匹配")

    return StarterKitArtifact(
        id=catalog_manifest["id"],
        name=catalog_manifest["name"],
        industry=catalog_manifest["industry"],
        version=catalog_manifest["version"],
        description=catalog_manifest["description"],
        fingerprint=computed_fingerprint,
        resource_counts=expected_counts,
        package=copy.deepcopy(normalized_package),
    )


def list_starter_kits() -> list[StarterKitSummary]:
    """Return catalog metadata in fixed, code-defined order after verification."""
    return [get_starter_kit(starter_kit_id) for starter_kit_id in STARTER_KIT_IDS]


def get_starter_kit(starter_kit_id: str) -> StarterKitSummary:
    """Return verified metadata for one starter kit without exposing a live source."""
    artifact = _verified_artifact(starter_kit_id)
    return StarterKitSummary(
        id=artifact.id,
        name=artifact.name,
        industry=artifact.industry,
        version=artifact.version,
        description=artifact.description,
        fingerprint=artifact.fingerprint,
        resource_counts=copy.deepcopy(artifact.resource_counts),
    )


def load_starter_kit(starter_kit_id: str) -> dict[str, Any]:
    """Load a verified portable package as a fresh mutable copy for proposal preview."""
    return copy.deepcopy(_verified_artifact(starter_kit_id).package)


def load_starter_kit_artifact(starter_kit_id: str) -> StarterKitArtifact:
    """Load verified metadata and package for internal callers that need both."""
    artifact = _verified_artifact(starter_kit_id)
    return artifact.model_copy(deep=True)
