"""Value-free diagnostics for failures behind the public Provider boundary."""
from __future__ import annotations

import logging
from pathlib import PurePath
from types import TracebackType


logger = logging.getLogger(__name__)


def _locations(traceback: TracebackType | None) -> list[str]:
    locations: list[str] = []
    while traceback is not None:
        code = traceback.tb_frame.f_code
        locations.append(f"{PurePath(code.co_filename).name}:{traceback.tb_lineno}")
        traceback = traceback.tb_next
    return locations[-8:]


def log_provider_failure(invocation_id: str, error: Exception) -> None:
    # Exception messages, traceback source lines and locals may contain SQL,
    # credentials or customer values. Only trusted code identities are logged.
    chain: list[dict[str, object]] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(chain) < 4:
        seen.add(id(current))
        chain.append({
            "type": type(current).__name__,
            "locations": _locations(current.__traceback__),
        })
        current = current.__cause__ or current.__context__
    logger.error("Capability Provider failed invocation=%s causes=%s", invocation_id, chain)
