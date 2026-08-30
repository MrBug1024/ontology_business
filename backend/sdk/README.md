# Ontology Platform Python SDK

This SDK is shipped as repository source; it is not currently published as an
installable package. Run integrations from the `backend` directory or add the
repository's `backend` directory to `PYTHONPATH` before importing `sdk`.

`CapabilityClient` is a thin client for `/api/external/v2`. It discovers
versioned contracts, submits typed inputs and governed references, and returns
the platform receipt. It does not parse customer data or reproduce Provider
logic. Create a credential with `capabilities:read` and
`capabilities:invoke`; add the independent `assets:write` scope only when the
integration must upload new invocation documents. Copy its token immediately
because it is returned only once.

## Zero-data capability

```python
from sdk import CapabilityClient

with CapabilityClient(
    "https://platform.example.com/api/external/v2",
    "ont_sk_...",
) as client:
    capabilities = client.list_capabilities("scenario-id", environment="prod")
    contract = capabilities[0]
    receipt = client.invoke_capability(
        "scenario-id",
        contract["kind"],
        contract["key"],
        environment="prod",
        inputs={"request": "Summarize the supplied requirements"},
        expected_definition_hash=contract["definition_hash"],
        expected_deployment_fingerprint=contract["deployment_fingerprint"],
    )
```

No `DataSource`, mapping, or managed data reference is required when the
published contract has no managed data ports.

## Discover scenarios and governed input choices

A capability client can bootstrap without a first-party UI. Scenario discovery
uses the API key subject's live scenario ACL and excludes retired scenarios.
For a selectable managed input port, ask the server for options tied to the
exact scenario, capability, port, environment, frozen definition hash and
deployment fingerprint:

```python
scenario = client.list_scenarios()[0]
contract = client.list_capabilities(scenario["id"], environment="prod")[0]
port = next(item for item in contract["data_ports"] if item["allow_override"])
page = client.list_managed_input_options(
    scenario["id"],
    contract["kind"],
    contract["key"],
    port["key"],
    environment="prod",
)

choice = page["items"][0]
receipt = client.invoke_capability(
    scenario["id"],
    contract["kind"],
    contract["key"],
    environment="prod",
    inputs={"threshold": 0.8},
    managed_inputs=[choice["managed_input"]],
    expected_definition_hash=page["definition_hash"],
    expected_deployment_fingerprint=page["deployment_fingerprint"],
)
same_receipt = client.get_invocation_receipt(receipt["invocation_id"])
```

Options contain only logical dataset/head/asset version identities or portable
connector binding keys plus checked signatures. They never contain a
`DataSource` id, object path, physical table/column metadata, connector target,
configuration, or credential. Ordinary typed `inputs` remain ordinary request
data and are never written into the asset catalog by discovery.

## Upload a new invocation document

The external upload endpoint accepts document bytes and logical metadata only.
The platform resolves a tenant-owned managed bucket and returns a logical
`DataAssetVersion`; callers cannot select a bucket, object path, endpoint, or
credential.

```python
contract = client.get_capability(
    "scenario-id", "function", "capability-key", environment="prod"
)
uploaded = client.upload_invocation_attachment(
    "requirements.docx",
    document_bytes,
    content_type=(
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document"
    ),
    expires_in_seconds=3600,
)

receipt = client.invoke_capability(
    "scenario-id",
    "function",
    "capability-key",
    environment="prod",
    inputs={"request": "Produce an implementation-ready specification"},
    managed_inputs=[{
        "port_key": "requirements",
        "asset_version_id": uploaded["version"]["id"],
    }],
    expected_definition_hash=contract["definition_hash"],
    expected_deployment_fingerprint=contract["deployment_fingerprint"],
)
```

Uploads are temporary invocation attachments by contract and use the same
format validation, immutable catalog write, deduplication, expiry, and failed
upload cleanup as the first-party catalog path. The response never contains
MinIO coordinates or storage credentials.

## Different data on each invocation

The caller selects an existing governed catalog reference. Connection strings,
physical table names, SQL, credentials, and internal source IDs are not valid
overrides.

```python
options = client.list_managed_input_options(
    "scenario-id", "function", "capability-key", "records", environment="prod"
)
version_a, version_b = options["items"][:2]

first = client.invoke_capability(
    "scenario-id",
    "function",
    "capability-key",
    environment="prod",
    inputs={"threshold": 0.8},
    managed_inputs=[version_a["managed_input"]],
    expected_definition_hash=options["definition_hash"],
    expected_deployment_fingerprint=options["deployment_fingerprint"],
)

second = client.invoke_capability(
    "scenario-id",
    "function",
    "capability-key",
    environment="prod",
    inputs={"threshold": 0.8},
    managed_inputs=[version_b["managed_input"]],
    expected_definition_hash=options["definition_hash"],
    expected_deployment_fingerprint=options["deployment_fingerprint"],
)

assert first["definition_hash"] == second["definition_hash"]
assert first["data_context_fingerprint"] != second["data_context_fingerprint"]
```

Side-effecting capabilities require an explicit preview/confirm exchange:

```python
action_contract = client.get_capability(
    "scenario-id", "action", "capability-key", environment="prod"
)
preview = client.invoke_capability(
    "scenario-id", "action", "capability-key",
    environment="prod", mode="preview", inputs={"request_id": "R-1001"},
    expected_definition_hash=action_contract["definition_hash"],
    expected_deployment_fingerprint=action_contract["deployment_fingerprint"],
)
confirmed = client.invoke_capability(
    "scenario-id", "action", "capability-key",
    environment="prod", mode="confirm", inputs={"request_id": "R-1001"},
    confirmation=preview["confirmation"],
    idempotency_key="enterprise-agent:R-1001",
    expected_definition_hash=action_contract["definition_hash"],
    expected_deployment_fingerprint=action_contract["deployment_fingerprint"],
)
```

The client sends credentials only in `X-API-Key`; platform RBAC and ACL remain
active on every request.

HTTPS is required by default.  A local mock can opt in with
`allow_insecure_http=True`, but only for `localhost` or a numeric loopback
address; the SDK never follows HTTP redirects while an API key is attached.

`OntologyPlatformClient` remains available only for existing read-only
`/api/external/v1` integrations.
