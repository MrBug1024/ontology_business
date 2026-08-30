"""Compatibility import for the relocated trusted capability package.

New platform code must import the Provider package.  This module remains only
for older verification scripts and integrations that imported the former
service path directly.
"""
from ..providers.medical_audit.service import *  # noqa: F401,F403
from ..providers.medical_audit import service as _service


def __getattr__(name: str):
    return getattr(_service, name)
