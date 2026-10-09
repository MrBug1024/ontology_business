"""Trusted external adapter; all execution and authorization stay on the platform."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP


ROOT = Path(__file__).resolve().parent
server = FastMCP('scenario-plugin')


def manifest() -> dict:
    path = ROOT / 'references' / 'scenario.json'
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('Scenario manifest is too large')
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('package_version') != 'scenario-plugin.v1' or not value.get('deployment', {}).get('release_id'):
        raise ValueError('Scenario release manifest is invalid')
    return value


async def remote(name: str, arguments: dict) -> Any:
    endpoint = os.environ.get('SCENARIO_MCP_URL', '')
    credential = os.environ.get('SCENARIO_API_KEY', '')
    parsed = urlsplit(endpoint)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not credential):
        raise ValueError('Configure the HTTPS MCP endpoint and a scenario credential before use')
    try:
        async with asyncio.timeout(60):
            async with httpx.AsyncClient(headers={'Authorization': f'Bearer {credential}'},
                    timeout=httpx.Timeout(45, connect=10), follow_redirects=False) as client:
                async with streamable_http_client(endpoint, http_client=client) as (reader, writer, _):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        result = await session.call_tool(name, arguments)
        if result.isError:
            raise ValueError('The platform rejected the request; review the scoped credential, release and inputs')
        value = result.structuredContent
        if value is None and len(result.content) == 1 and result.content[0].type == 'text':
            value = json.loads(result.content[0].text)
        return value['result'] if isinstance(value, dict) and set(value) == {'result'} else value
    except Exception as exc:
        # Never expose transport headers, credentials or supplier traceback.
        raise ValueError('Scenario request did not complete; inspect the platform receipt before retrying effects') from None


def selected(document: dict, kind: str, key: str) -> None:
    if not any(item['kind'] == kind and item['key'] == key for item in document['capabilities']):
        raise ValueError('Capability is not included in this plugin')


def checked_receipt(document: dict, result: Any) -> dict:
    if not isinstance(result, dict) or result.get('definition_hash') != document['deployment']['definition_hash']:
        raise ValueError('Receipt does not match the pinned scenario definition')
    capability = result.get('capability', {})
    selected(document, capability.get('kind', ''), capability.get('key', ''))
    return result


@server.tool()
async def list_scenario_capabilities() -> list[dict]:
    """Discover the exact release contracts included in this plugin."""
    document = manifest()
    result = await remote('list_capabilities', {'scenario_id': document['scenario']['id'],
        'release_id': document['deployment']['release_id']})
    if isinstance(result, dict):
        result = result.get('capabilities')
    if not isinstance(result, list):
        raise ValueError('Platform capability discovery is invalid')
    identities = {(item['kind'], item['key']) for item in document['capabilities']}
    filtered = [item for item in result if (item.get('kind'), item.get('key')) in identities]
    if (len(filtered) != len(identities) or any(item.get('definition_hash') != document['deployment']['definition_hash'] for item in filtered)):
        raise ValueError('The pinned release is unavailable or changed')
    return filtered


@server.tool()
async def invoke_scenario_capability(kind: Literal['function', 'action', 'rule', 'workflow'], key: str,
        inputs: dict[str, Any], mode: Literal['execute', 'preview', 'confirm'] = 'execute',
        managed_inputs: list[dict[str, Any]] | None = None, idempotency_key: str | None = None,
        correlation_id: str | None = None, request_id: str | None = None,
        confirmation: dict[str, Any] | None = None) -> dict:
    """Invoke a packaged capability; server preview, confirmation and idempotency remain authoritative."""
    document = manifest()
    selected(document, kind, key)
    arguments = {'scenario_id': document['scenario']['id'], 'release_id': document['deployment']['release_id'],
        'expected_definition_hash': document['deployment']['definition_hash'],
        'capability_kind': kind, 'capability_key': key, 'inputs': inputs, 'mode': mode,
        'managed_inputs': managed_inputs or [], 'idempotency_key': idempotency_key,
        'correlation_id': correlation_id, 'request_id': request_id, 'confirmation': confirmation or {}}
    if len(json.dumps(arguments, allow_nan=False).encode()) > 128 * 1024:
        raise ValueError('Invocation exceeds the request size limit')
    return checked_receipt(document, await remote('invoke_capability', arguments))


@server.tool()
async def get_scenario_receipt(invocation_id: str) -> dict:
    """Read a caller-owned receipt for a capability included in this plugin."""
    if not 1 <= len(invocation_id) <= 32:
        raise ValueError('Invocation identifier is invalid')
    return checked_receipt(manifest(), await remote('get_capability_receipt', {'invocation_id': invocation_id}))


def checked_interaction(receipt: dict, *, kind: str, interaction_id: str, expected_revision: int | None = None) -> None:
    public_kind = 'workflow_approval' if kind == 'approval' else 'capability_confirmation'
    interactions = receipt.get('delivery', {}).get('interactions', [])
    if not any(item.get('kind') == public_kind and item.get('id') == interaction_id
        and (item.get('status') == 'pending' if kind == 'approval' else receipt.get('status') == 'awaiting_confirmation')
        and (expected_revision is None or item.get('revision') == expected_revision)
        for item in interactions):
        raise ValueError('Interaction is not pending on this pinned invocation or its revision changed')


@server.tool()
async def read_scenario_approval(invocation_id: str, interaction_id: str) -> dict:
    """Read an approval actually advertised by a pinned scenario receipt."""
    receipt = await get_scenario_receipt(invocation_id)
    checked_interaction(receipt, kind='approval', interaction_id=interaction_id)
    result = await remote('read_business_approval', {'interaction_id': interaction_id})
    if not isinstance(result, dict):
        raise ValueError('Platform approval projection is invalid')
    return result


@server.tool()
async def reply_scenario_interaction(invocation_id: str, kind: Literal['confirmation', 'approval'],
        interaction_id: str, text: str, message_id: str, expected_revision: int,
        evidence: list[dict[str, Any]] | None = None) -> dict:
    """Send an explicit human reply; platform audience, evidence, revision and confirmation govern effects."""
    if not 1 <= len(text) <= 5000 or not 1 <= len(message_id) <= 160 or expected_revision < 1 or len(evidence or []) > 20:
        raise ValueError('Business reply exceeds contract limits')
    receipt = await get_scenario_receipt(invocation_id)
    checked_interaction(receipt, kind=kind, interaction_id=interaction_id, expected_revision=expected_revision)
    result = await remote('reply_business_interaction', {'kind': kind, 'interaction_id': interaction_id,
        'reply': {'text': text, 'message_id': message_id, 'expected_revision': expected_revision, 'evidence': evidence or []}})
    if not isinstance(result, dict):
        raise ValueError('Platform reply projection is invalid')
    return result


if __name__ == '__main__':
    server.run(transport='stdio')
