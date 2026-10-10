"""Remote investigation browser driven through a member-machine connector.

Implements the same duck-typed surface as ``InvestigationBrowser`` used by the
browser tool catalog. Every operation re-runs the server-side authorization
callback before dispatching one connector op; credentials travel only inside
the initial ``browser.open`` call over the authenticated WebSocket and never
enter logs or checkpoints. The connector enforces the same read-only network
policy locally (scope pinning, method allowlist, mutation blocking) while the
server remains the authority for authorization and audit.
"""
from __future__ import annotations

import logging

from ..distillation_target_schemas import TargetSystem
from .distillation_browser_network import BrowserAccessError
from .distillation_investigation_connector_gateway import ConnectorUnavailable, call as connector_call

logger = logging.getLogger(__name__)


class ConnectorInvestigation:
    def __init__(self, session_id: str, target: TargetSystem, authorize, credentials=None):
        self.connector_session_id = session_id
        self.target, self.authorize = target, authorize
        self._credentials = dict(credentials or {})

    def _invoke(self, op: str, args: dict) -> dict:
        self.authorize()
        try:
            return connector_call(self.connector_session_id, op, args)
        except ConnectorUnavailable as exc:
            raise BrowserAccessError(str(exc)) from exc

    def open(self) -> dict:
        payload = self._invoke("browser.open", {
            "target": self.target.model_dump(mode="json"),
            "credentials": self._credentials,
        })
        self._credentials.clear()
        return payload

    def prepare(self) -> None:
        self._invoke("browser.prepare", {})

    def settle(self) -> None:
        self._invoke("browser.prepare", {})

    def navigate(self, path: str) -> dict:
        return self._invoke("browser.navigate", {"path": path})

    def snapshot(self, offset=0) -> dict:
        return self._invoke("browser.snapshot", {"offset": int(offset)})

    def fill(self, page_id: str, ref: str, value: str) -> dict:
        return self._invoke("browser.fill", {"page_id": page_id, "ref": ref, "value": value})

    def click(self, page_id: str, ref: str, intent: str) -> dict:
        return self._invoke("browser.click", {"page_id": page_id, "ref": ref, "intent": intent})

    def login(self, page_id: str, username_ref: str, password_ref: str, submit_ref: str) -> dict:
        return self._invoke("browser.login", {
            "page_id": page_id, "username_ref": username_ref,
            "password_ref": password_ref, "submit_ref": submit_ref})

    def close(self) -> None:
        # Best effort: the connector also cleans its browser up on disconnect.
        try:
            connector_call(self.connector_session_id, "browser.close", {}, timeout=10)
        except Exception:  # noqa: BLE001
            logger.info("Connector browser close skipped session=%s", self.connector_session_id)
