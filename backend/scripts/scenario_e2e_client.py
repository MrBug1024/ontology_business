"""Execute an explicitly reviewed, installed client against real scoped receipts.

This is test orchestration, not a production runtime for tenant code. The module
and entry point must be supplied from the already reviewed immutable artifact.
Credentials are passed only to the isolated process environment and never saved.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

from sqlalchemy import select

from app.models import CapabilityInvocation

RUNNER = '''
import asyncio
import importlib
import json
import logging
import sys
logging.disable(logging.CRITICAL)
try:
    request = json.load(sys.stdin)
    module = importlib.import_module(request['module'])
    result = asyncio.run(getattr(module, request['function'])(*request['arguments']))
    print(json.dumps({'result': result}, allow_nan=False))
except Exception as error:
    print(json.dumps({'failure_type': type(error).__name__}))
    raise SystemExit(1) from None
'''


def receipts(value) -> list[dict]:
    if isinstance(value, dict):
        if isinstance(value.get('invocation_id'), str) and 'status' in value:
            return [value]
        return [receipt for item in value.values() for receipt in receipts(item)]
    if isinstance(value, list):
        return [receipt for item in value for receipt in receipts(item)]
    return []


def verify_client(*, specification: dict, installed: Path, interpreter: Path,
                  environment: dict, client, actor_session, scene_id: str,
                  username: str, key_id: str, definition_hash: str) -> dict:
    module, function = specification['module'], specification['function']
    assert re.fullmatch(r'scripts\.[a-z][a-z0-9_]*', module)
    assert re.fullmatch(r'[a-z][a-z0-9_]*', function)
    source = installed.joinpath(*module.split('.')).with_suffix('.py')
    assert source.is_file() and source.resolve().is_relative_to(installed.resolve())
    outcomes = []
    for case in specification['cases']:
        with actor_session(scene_id, username) as db:
            before = set(db.scalars(select(CapabilityInvocation.id).where(
                CapabilityInvocation.scenario_id == scene_id,
                CapabilityInvocation.tenant_id == db.info['tenant_id'],
                CapabilityInvocation.principal_id == key_id)).all())
        request = {'module': module, 'function': function, 'arguments': case['arguments']}
        completed = subprocess.run([str(interpreter), '-c', RUNNER],
            input=json.dumps(request, allow_nan=False).encode(), env=environment,
            cwd=str(installed), capture_output=True, timeout=150)
        assert completed.returncode == 0, 'Reviewed installed client did not complete'
        assert len(completed.stdout) < 256 * 1024
        value = json.loads(completed.stdout)
        returned = receipts(value['result'])
        expected = case['expected_receipts']
        assert len(returned) == len(expected), 'Client returned unexpected execution count'
        with actor_session(scene_id, username) as db:
            after = set(db.scalars(select(CapabilityInvocation.id).where(
                CapabilityInvocation.scenario_id == scene_id,
                CapabilityInvocation.tenant_id == db.info['tenant_id'],
                CapabilityInvocation.principal_id == key_id)).all())
        returned_ids = {item['invocation_id'] for item in returned}
        assert after - before == returned_ids, 'Client executed an unreported capability'
        verified = []
        for receipt, assertion in zip(returned, expected):
            response = client.get('/api/external/v2/invocations/' + receipt['invocation_id'])
            assert response.status_code == 200
            actual = response.json()
            assert actual['definition_hash'] == definition_hash
            assert actual['capability']['kind'] == assertion['kind']
            assert actual['capability']['key'] == assertion['key']
            assert actual['status'] == assertion['status']
            assert actual['output'] == assertion['output']
            assert receipt['status'] == actual['status'] and receipt['output'] == actual['output']
            verified.append({'invocation_id': actual['invocation_id'],
                'kind': actual['capability']['kind'], 'status': actual['status'],
                'output': actual['output']})
        outcomes.append({'role': case['role'], 'receipts': verified,
                         'no_unreported_invocations': True})
    return {'module': module, 'function': function, 'cases': outcomes,
            'actual_installed_source_executed': True}


def client_environment(credential: str, origin: str, ca: Path) -> dict:
    value = {key: item for key, item in os.environ.items()
             if key.upper() in {'PATH', 'SYSTEMROOT', 'TEMP', 'TMP'}}
    value.update(SCENARIO_MCP_URL=origin + '/mcp', SCENARIO_API_KEY=credential,
        SSL_CERT_FILE=str(ca), PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
    return value
