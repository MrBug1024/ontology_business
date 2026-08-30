"""Static application-owned registry of installed trusted Provider packages."""
from __future__ import annotations

from typing import Any

from .medical_audit import MedicalAuditProvider


def trusted_capability_providers() -> tuple[Any, ...]:
    """Return concrete trusted providers; never import a database-selected path."""

    return (MedicalAuditProvider(),)


__all__ = ["trusted_capability_providers"]
