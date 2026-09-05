from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.external_api_schemas import ExternalCapabilityInvocationIn
from app.schemas import ChatRequest


@pytest.mark.parametrize("request_type", [ChatRequest, ExternalCapabilityInvocationIn])
def test_protocol_request_allows_distinct_inputs_for_the_same_port(request_type) -> None:
    payload = {
        "managed_inputs": [
            {"port_key": "records", "dataset_version_id": "version-a"},
            {"port_key": "records", "dataset_version_id": "version-b"},
        ],
    }
    if request_type is ChatRequest:
        payload["message"] = "validate"
    request = request_type(**payload)

    assert len(request.managed_inputs) == 2


@pytest.mark.parametrize("request_type", [ChatRequest, ExternalCapabilityInvocationIn])
def test_protocol_request_rejects_exact_duplicate_inputs(request_type) -> None:
    payload = {
        "message": "validate" if request_type is ChatRequest else None,
        "managed_inputs": [
            {"port_key": "records", "dataset_version_id": "version-a"},
            {"port_key": "records", "dataset_version_id": "version-a"},
        ],
    }
    if request_type is not ChatRequest:
        payload.pop("message")

    with pytest.raises(ValidationError, match="相同受管输入"):
        request_type(**payload)
