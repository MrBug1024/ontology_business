"""Statically trusted platform capability Providers."""
from __future__ import annotations

from .semantic_dataset_query import SemanticDatasetQueryProvider


def trusted_capability_providers() -> tuple[SemanticDatasetQueryProvider, ...]:
    return (SemanticDatasetQueryProvider(),)


__all__ = ["SemanticDatasetQueryProvider", "trusted_capability_providers"]
