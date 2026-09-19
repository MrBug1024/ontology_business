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

Each credential is bound to exactly one business scenario in its issuing workspace.
Select that scenario when creating the credential (`scenario_id` is required).
REST v1/v2 and Capability MCP restrict discovery, calls, receipts, interactions,
and temporary attachments to that scenario, even if its subject owns other scenarios.
Changing a request's scenario or release id cannot widen this boundary. A key can
use enabled releases of its own scenario; the existing human publication lifecycle
is unchanged. Temporary uploads are private to that scenario and cannot be reused
through a different scenario's key by guessing an asset id.

The scenario-binding migration revokes historical unbound keys because their
intended scenario cannot be inferred safely. They remain visible as requiring
reissuance. Create replacement scenario-bound credentials and update your REST/MCP
clients; old tokens are never automatically reactivated after rollback.

Each credential remains bound to the workspace in which it was issued. Switching
the browser's active workspace does not change SDK authorization. The server
checks the subject's active membership and scenario ACL on every request; a
system superadmin role does not grant workspace access. Removing a member or
changing their workspace role revokes that workspace's credentials. Disabling
the account revokes all of its credentials, and restoring the account does not
restore old tokens.

## Zero-data capability

The `release_id` in the examples is the id of a manually created and enabled
scenario release. Omit it to select the scenario's one enabled release. The
server never falls back to authored definitions for external calls.

### Contract transition (2026-09-08)

Upgrade the SDK and server together. Current `/external/v2` requests no longer
accept `environment`, `runtime_environment` or `expected_environment`: retired
query fields return 410 and closed request bodies reject them with 422. Replace
them with an optional explicit `release_id`. They are not silently ignored.
The immutable definition format is v3 and channel delivery is v1; historical
snapshot formats are read only for authorized pinned history. No parallel
environment-based endpoint is retained. Deployments use separate infrastructure
configuration, never business records or permission labels.

### Consume capabilities from your own Agent

`invoke_capability` executes the selected business contract directly. It does not
require a platform Agent, a chat session or an Agent LLM configuration. The same
contract is available through REST, this SDK and the generic MCP tools
`list_capabilities`, `invoke_capability`, and `get_capability_receipt`. The separate
`invoke_agent` MCP tool delegates a conversation to the platform Agent and is
not required to add scene capabilities to an existing Agent.

Read `receipt["output"]` as the business result and retain its structure, together
with the authoritative status and evidence. For an asynchronous workflow, poll
until its terminal state; its execution result is in `output.result`. The
workflow's explicit ontology output contract, when configured, identifies its
result node and validated output; older workflows retain their execution steps.
`delivery.text` is an optional lossy plain-text summary for existing channel
adapters, not the output contract or an instruction to the caller's LLM. It may
omit nested details. A web Agent can render Markdown; a messaging adapter can
choose plain text without changing the published capability.

Pass the required current context through typed `inputs`. Ordinary structured
inputs do not require creating a DataSource, asset or ontology instance. Managed
uploads/connections are necessary only for contracts that declare those data
ports. Online execution processes these inputs, and durable workflows retain
protected execution data for recovery and audit; this is not a zero-retention or
offline service. Data that must never leave the caller needs an appropriately
trusted local execution package or deployment.

For caller-generated documents, define the business output schema to describe
the content, required format and evidence. The caller's Agent selects its own
authorized renderer, destination and delivery mechanism. Content is a draft,
not proof that a file was created or sent. Platform-generated managed files are
an optional, explicitly selected capability and can still be downloaded through
the authorized attachment endpoint. Do not fabricate artifact IDs or pass a
client filesystem path as a platform data reference.

### Optional plain-message delivery

Poll `client.get_invocation_receipt(invocation_id)`. Deliver changes only when
`receipt["delivery"]["revision"]` changes, to the original upstream conversation,
when using this compatibility presentation. This revision covers the delivery
view only; consumers of full results must also track `output` and `status`.
`delivery.text` is plain text, `interactions` identifies the pending person,
allowed replies, expiry and revision, and `attachments` contains authorized files.
Use `download_invocation_attachment(invocation_id, file_id)` to relay the file;
credentials stay in headers. A queued workflow remains running until its actual
execution finishes. Human retries preserve the original invocation's final result.

An adapter must authenticate the actual sender using that person's authorized
credential or a trusted identity mapping. A shared bot credential does not grant
permission to impersonate named group members. The model may explain a pending
decision; only the human's actual message is submitted to the reply interface.

```python
interaction = receipt["delivery"]["interactions"][0]
kind = "approval" if interaction["kind"] == "workflow_approval" else "confirmation"
result = client.reply_business_interaction(
    kind, interaction["id"], text=human_message,
    message_id=upstream_message_id, expected_revision=interaction["revision"],
    evidence=[{"asset_version_id": uploaded["version"]["id"]}],
)
```

Supply evidence only for business approval; adding inputs to execution confirmation
requires a new preview. A different authorized approver can use
`get_business_approval(approval_id)` with their own credential. MCP exposes
`read_business_approval` and `reply_business_interaction` over the same service.
REST equivalents are GET `/interactions/approval/{id}` and POST
`/interactions/{approval|confirmation}/{id}/reply` beneath `/api/external/v2`.

The validation Agent accepts the human's ordinary confirmation message in the
same conversation. Its session-authenticated adapter restores the exact preview
inputs and delegates to CapabilityInvoker. The existing typed browser endpoint
remains available to compatible clients:
`GET /api/agents/{agent_id}/capability-invocations/{invocation_id}?message_id=...`
reads the current receipt, and `POST` to the same path plus `/confirm` accepts
`{"message_id": "...", "confirmed": true}`. The server verifies conversation
ownership and current capability permissions, restores encrypted preview inputs,
and delegates to the same CapabilityInvoker. The model-visible projection omits
confirmation tokens. This browser adapter does not change the SDK/API-key
confirmation contract or authorize a model to confirm an operation.

After a workflow approval, preceding successful node outputs are restored from
the durable reviewed result. LLM nodes before that approval are not regenerated.
Restart all API/worker instances when deploying this execution change.

```python
from sdk import CapabilityClient

with CapabilityClient(
    "https://platform.example.com/api/external/v2",
    "ont_sk_...",
) as client:
    capabilities = client.list_capabilities("scenario-id", release_id="release-id")
    contract = capabilities[0]
    receipt = client.invoke_capability(
        "scenario-id",
        contract["kind"],
        contract["key"],
        release_id="release-id",
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
exact scenario, capability, port, release, frozen definition hash and
deployment fingerprint:

```python
scenario = client.list_scenarios()[0]
contract = client.list_capabilities(scenario["id"], release_id="release-id")[0]
port = next(item for item in contract["data_ports"] if item["allow_override"])
page = client.list_managed_input_options(
    scenario["id"],
    contract["kind"],
    contract["key"],
    port["key"],
    release_id="release-id",
)

choice = page["items"][0]
receipt = client.invoke_capability(
    scenario["id"],
    contract["kind"],
    contract["key"],
    release_id="release-id",
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
    "scenario-id", "function", "capability-key", release_id="release-id"
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
    release_id="release-id",
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
    "scenario-id", "function", "capability-key", "records", release_id="release-id"
)
version_a, version_b = options["items"][:2]

first = client.invoke_capability(
    "scenario-id",
    "function",
    "capability-key",
    release_id="release-id",
    inputs={"threshold": 0.8},
    managed_inputs=[version_a["managed_input"]],
    expected_definition_hash=options["definition_hash"],
    expected_deployment_fingerprint=options["deployment_fingerprint"],
)

second = client.invoke_capability(
    "scenario-id",
    "function",
    "capability-key",
    release_id="release-id",
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
    "scenario-id", "action", "capability-key", release_id="release-id"
)
preview = client.invoke_capability(
    "scenario-id", "action", "capability-key",
    release_id="release-id", mode="preview", inputs={"request_id": "R-1001"},
    expected_definition_hash=action_contract["definition_hash"],
    expected_deployment_fingerprint=action_contract["deployment_fingerprint"],
)
confirmed = client.invoke_capability(
    "scenario-id", "action", "capability-key",
    release_id="release-id", mode="confirm", inputs={"request_id": "R-1001"},
    confirmation=preview["confirmation"],
    idempotency_key="enterprise-agent:R-1001",
    expected_definition_hash=action_contract["definition_hash"],
    expected_deployment_fingerprint=action_contract["deployment_fingerprint"],
)
```

The client sends credentials only in `X-API-Key`; platform RBAC and ACL remain
active on every request.

Authored rule constraints, workflow ontology bindings and semantic query roles
are evaluated on the server through the same capability contract. The SDK does
not recreate their validation. See the [ontology business contract API](../../docs/ontology-business-contract-api.md)
for authoring fields, legacy-release compatibility and result validation.

HTTPS is required by default.  A local mock can opt in with
`allow_insecure_http=True`, but only for `localhost` or a numeric loopback
address; the SDK never follows HTTP redirects while an API key is attached.

`OntologyPlatformClient` remains available only for existing read-only
`/api/external/v1` integrations.
