from __future__ import annotations

import base64
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import JSON, Column, MetaData, String, Table, create_engine, insert, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from app.models import WorkflowRun
from app.services import workflow_payload_service


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REVISION_PATH = (
    BACKEND_ROOT
    / "migrations"
    / "versions"
    / "20260829_09_encrypt_workflow_run_inputs.py"
)


def _encoded(byte: int) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode("ascii").rstrip("=")


def _settings(*, active: str = "active", include_old: bool = True) -> SimpleNamespace:
    keys = {"active": _encoded(17)}
    if include_old:
        keys["old"] = _encoded(23)
    return SimpleNamespace(
        workflow_payload_active_key_id=active,
        workflow_payload_encryption_keys=json.dumps(keys),
    )


def _context() -> dict[str, str]:
    return workflow_payload_service.payload_context(
        run_id="run-secure-payload",
        scenario_id="scenario-secure-payload",
        workflow_id="workflow-secure-payload",
        environment="dev",
        definition_hash="a" * 64,
    )


def test_aes_gcm_envelope_round_trips_and_rejects_every_authenticated_tamper() -> None:
    keyring = workflow_payload_service.load_keyring(_settings())
    marker = "customer-secret-operation-value"
    payload = {"reference": marker, "count": 3, "nested": {"enabled": True}}

    sealed = workflow_payload_service.seal_payload(
        payload,
        context=_context(),
        keyring=keyring,
    )
    stored = json.dumps(
        {
            "envelope": sealed.envelope,
            "summary": sealed.summary,
            "digest": sealed.digest,
        },
        sort_keys=True,
    )
    assert marker not in stored
    assert "reference" not in stored
    assert sealed.envelope["alg"] == "A256GCM"
    assert sealed.summary["value_type_counts"] == {
        "integer": 1,
        "object": 1,
        "string": 1,
    }
    assert workflow_payload_service.open_payload(
        sealed.envelope,
        context=_context(),
        summary=sealed.summary,
        digest=sealed.digest,
        keyring=keyring,
    ) == payload

    tampered_envelope = copy.deepcopy(sealed.envelope)
    tampered_envelope["nonce"] = base64.urlsafe_b64encode(b"x" * 12).decode().rstrip("=")
    with pytest.raises(workflow_payload_service.WorkflowPayloadError) as ciphertext_error:
        workflow_payload_service.open_payload(
            tampered_envelope,
            context=_context(),
            summary=sealed.summary,
            digest=sealed.digest,
            keyring=keyring,
        )
    assert ciphertext_error.value.code == "workflow_payload_authentication_failed"

    tampered_context = {**_context(), "workflow_id": "another-workflow"}
    with pytest.raises(workflow_payload_service.WorkflowPayloadError):
        workflow_payload_service.open_payload(
            sealed.envelope,
            context=tampered_context,
            summary=sealed.summary,
            digest=sealed.digest,
            keyring=keyring,
        )

    tampered_summary = {**sealed.summary, "field_count": 99}
    with pytest.raises(workflow_payload_service.WorkflowPayloadError):
        workflow_payload_service.open_payload(
            sealed.envelope,
            context=_context(),
            summary=tampered_summary,
            digest=sealed.digest,
            keyring=keyring,
        )


def test_key_rotation_keeps_old_payload_recoverable_and_missing_key_fails_closed() -> None:
    old_ring = workflow_payload_service.load_keyring(_settings(active="old"))
    sealed = workflow_payload_service.seal_payload(
        {"operation": "old-value"},
        context=_context(),
        keyring=old_ring,
    )
    rotated_ring = workflow_payload_service.load_keyring(_settings(active="active"))
    assert workflow_payload_service.open_payload(
        sealed.envelope,
        context=_context(),
        summary=sealed.summary,
        digest=sealed.digest,
        keyring=rotated_ring,
    ) == {"operation": "old-value"}

    without_old = workflow_payload_service.load_keyring(
        _settings(active="active", include_old=False)
    )
    with pytest.raises(workflow_payload_service.WorkflowPayloadError) as exc:
        workflow_payload_service.open_payload(
            sealed.envelope,
            context=_context(),
            summary=sealed.summary,
            digest=sealed.digest,
            keyring=without_old,
        )
    assert exc.value.code == "workflow_payload_key_unavailable"


def test_missing_or_weak_key_configuration_is_rejected_without_fallback() -> None:
    with pytest.raises(workflow_payload_service.WorkflowPayloadError) as missing:
        workflow_payload_service.load_keyring(
            SimpleNamespace(
                workflow_payload_active_key_id="",
                workflow_payload_encryption_keys="",
            )
        )
    assert missing.value.code == "workflow_payload_key_unconfigured"

    with pytest.raises(workflow_payload_service.WorkflowPayloadError) as weak:
        workflow_payload_service.load_keyring(
            SimpleNamespace(
                workflow_payload_active_key_id="weak",
                workflow_payload_encryption_keys=json.dumps(
                    {"weak": base64.urlsafe_b64encode(b"too-short").decode()}
                ),
            )
        )
    assert weak.value.code == "invalid_key_configuration"


def test_workflow_run_orm_has_only_encrypted_payload_columns_on_both_dialects() -> None:
    columns = set(WorkflowRun.__table__.c.keys())
    assert "input_params" not in columns
    assert {"input_payload", "input_summary", "input_digest"}.issubset(columns)
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        ddl = str(CreateTable(WorkflowRun.__table__).compile(dialect=dialect))
        assert "input_payload" in ddl
        assert "input_summary" in ddl
        assert "input_digest" in ddl
        assert "input_params" not in ddl


def _load_revision():
    spec = importlib.util.spec_from_file_location("workflow_payload_revision", REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_data_transform_encrypts_and_reversibly_downgrades_legacy_rows() -> None:
    revision = _load_revision()
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    runs = Table(
        "workflow_runs",
        metadata,
        Column("id", String, primary_key=True),
        Column("scenario_id", String, nullable=False),
        Column("workflow_id", String, nullable=False),
        Column("environment", String, nullable=False),
        Column("definition_hash", String, nullable=False),
        Column("input_payload", JSON, nullable=False),
        Column("input_summary", JSON, nullable=True),
        Column("input_digest", String, nullable=True),
    )
    metadata.create_all(engine)
    marker = "legacy-customer-operation"
    with engine.begin() as connection:
        connection.execute(
            insert(runs).values(
                id="run-legacy",
                scenario_id="scenario-legacy",
                workflow_id="workflow-legacy",
                environment="dev",
                definition_hash="b" * 64,
                input_payload={"operation": marker},
            )
        )
        assert revision._encrypt_existing_rows(connection) == 1
        encrypted = connection.execute(select(runs)).mappings().one()
        assert marker not in json.dumps(dict(encrypted), sort_keys=True)
        assert encrypted["input_payload"]["contract"] == (
            workflow_payload_service.ENVELOPE_CONTRACT
        )
        assert revision._decrypt_existing_rows(connection) == 1
        restored = connection.execute(select(runs.c.input_payload)).scalar_one()
        assert restored == {"operation": marker}
    engine.dispose()
