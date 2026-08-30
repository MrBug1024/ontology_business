"""Small dependency-light SDK for the versioned external API."""

from .ontology_platform_sdk import (
    CapabilityClient,
    CapabilityKind,
    ExternalApiError,
    OntologyPlatformClient,
)

__all__ = [
    "CapabilityClient",
    "CapabilityKind",
    "ExternalApiError",
    "OntologyPlatformClient",
]
