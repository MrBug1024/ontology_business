"""Statically trusted platform capability Providers."""
from __future__ import annotations

from .semantic_audit import SemanticAuditProvider
from .semantic_dataset_query import SemanticDatasetQueryProvider


def trusted_capability_providers() -> tuple[
    SemanticDatasetQueryProvider | SemanticAuditProvider, ...
]:
    return (SemanticDatasetQueryProvider(), SemanticAuditProvider())


__all__ = [
    "SemanticAuditProvider",
    "SemanticDatasetQueryProvider",
    "trusted_capability_providers",
]
