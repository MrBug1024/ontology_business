"""Explicit, exact-version compatibility adapters for trusted query Providers."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def resolve_config_extension(
    *,
    provider_key: str,
    provider_version: str,
    extra_fields: set[str],
    provider_config: Mapping[str, Any],
    input_schema: Any,
    compatibility_mode: bool,
) -> tuple[dict[str, Any], Any] | None:
    """Return a frozen adapter only for a known historical Provider identity."""

    if (
        provider_key != "builtin.semantic-dataset-query"
        or provider_version != "1.0.0"
        or extra_fields != {"rule_query"}
    ):
        return None
    if not compatibility_mode:
        raise ValueError(
            "legacy rule_query must migrate to the versioned semantic audit Provider"
        )
    from .semantic_audit import legacy_dataset_query_binding

    normalized, extension = legacy_dataset_query_binding(
        provider_config.get("rule_query"),
        input_schema=input_schema,
    )
    return {"rule_query": normalized}, extension


__all__ = ["resolve_config_extension"]
