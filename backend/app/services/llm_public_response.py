"""Bounded extraction of answer text for completion checks and tool context."""
from __future__ import annotations


class PublicResponseText:
    """Recognize leading think tags across tokens, never buffering their body."""

    _OPEN = '<think>'
    _CLOSE = ('</think>', '<\\think>')
    _MAX_PREFIX = 4096

    def __init__(self) -> None:
        self._state = 'prefix'
        self._pending = ''
        self._removed = False

    def feed(self, text: str) -> str:
        if self._state == 'body':
            return text
        pending = self._pending + text
        self._pending = ''
        while True:
            if self._state == 'reasoning':
                lowered = pending.lower()
                positions = [index for marker in self._CLOSE if (index := lowered.find(marker)) >= 0]
                if not positions:
                    self._pending = pending[-7:]
                    return ''
                pending = pending[min(positions) + 8:]
                self._state = 'prefix'
                self._removed = True
            stripped = pending.lstrip()
            if not stripped or self._OPEN.startswith(stripped.lower()):
                if stripped.lower() == self._OPEN:
                    self._state = 'reasoning'
                    return ''
                if len(pending) > self._MAX_PREFIX:
                    raise ValueError('模型回答的前导空白超过显示边界')
                self._pending = pending
                return ''
            if stripped.lower().startswith(self._OPEN):
                self._state = 'reasoning'
                pending = stripped[len(self._OPEN):]
                continue
            self._state = 'body'
            return stripped if self._removed else pending

    def finish(self) -> str:
        # An unfinished reasoning block or tag must never become public text.
        pending, self._pending = self._pending, ''
        if self._state == 'reasoning' or self._OPEN.startswith(pending.lstrip().lower()):
            return ''
        return pending
