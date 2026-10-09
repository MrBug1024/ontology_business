"""Install reviewed synthetic plugins and exercise their authenticated protocols.

The credential is issued through the normal management application, stays in
memory, and is revoked even when an assertion fails. The input reports identify
only the newly authorized scenarios; no old scenario is modified.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import traceback
from time import monotonic, sleep
from uuid import uuid4

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import or_, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal
from app.external_api_schemas import ExternalApiKeyCreateIn
from app.models import BusinessScenario, User
from app.routers import external_api
from app.services import permission_service

API = '/api/external/v2'
TERMINAL = {'succeeded', 'failed', 'rejected', 'cancelled', 'timed_out'}


@contextmanager
def actor_session(scenario_id: str, username: str):
    with SessionLocal() as db:
        users = db.scalars(select(User).where(or_(User.display_name == username, User.email == username))).all()
        user = users[0] if len(users) == 1 else None
        scene = db.get(BusinessScenario, scenario_id)
        if user is None or scene is None:
            raise ValueError('Explicit acceptance identity is unavailable')
        db.info.update(user_id=user.id, tenant_id=scene.tenant_id)
        permission_service.require_principal(db)
        permission_service.require_tenant_permission(db, 'manage')
        permission_service.require_scenario_permission(db, scene, 'manage')
        yield db


def safe_receipt(receipt: dict) -> dict:
    value = {key: receipt.get(key) for key in ('invocation_id', 'status',
        'definition_hash', 'data_context_fingerprint', 'output')}
    value['error_code'] = (receipt.get('error') or {}).get('code')
    return value


def normalize_report(report: dict) -> dict:
    value = dict(report)
    value['scene_id'] = report.get('scene_id', report.get('scenario_id'))
    release = report.get('release', {})
    value['release_id'] = report.get('release_id', release.get('id'))
    identities = {(item['kind'], item['key']) for item in report['capabilities']}
    catalog = report.get('selected_capabilities', report.get('capability_catalog', []))
    value['selected_capabilities'] = [item for item in catalog if (item['kind'], item['key']) in identities]
    assert value['selected_capabilities'], 'No actual selected capability contract'
    value['definition_hash'] = report.get('definition_hash') or next(
        item['definition_hash'] for item in report.get('capability_catalog', [])
        if (item['kind'], item['key']) in identities)
    raw_cases = report.get('cases', report.get('business_cases', []))
    cases = []
    for item in raw_cases:
        case = dict(item)
        if 'kind' not in case or 'key' not in case:
            assert len(identities) == 1
            case['kind'], case['key'] = next(iter(identities))
        output = case.get('actual_output', {'result': case['result']} if 'result' in case else {})
        case.setdefault('output', output)
        case.setdefault('expected_status', case.get('expected_assertion', {}).get('status',
            case.get('stored_invocation_status', case.get('status'))))
        cases.append(case)
    assert cases and all(item.get('expected_status') for item in cases)
    value['business_acceptance'] = {'cases': cases}
    return value


def business_value(kind: str, output: dict):
    if kind != 'workflow':
        return output
    result = output.get('result', {})
    return [step.get('result') for step in result.get('steps', []) if step.get('type') == 'end']


def wait_rest(client: httpx.Client, receipt: dict) -> dict:
    deadline = monotonic() + 120
    current = receipt
    while current['status'] not in TERMINAL and current['status'] != 'awaiting_approval':
        if monotonic() >= deadline:
            raise TimeoutError('Receipt did not settle')
        sleep(0.5)
        response = client.get(API + '/invocations/' + current['invocation_id'])
        assert response.status_code == 200, 'Receipt read failed'
        current = response.json()
    return current


def document(result):
    assert not result.isError, 'Installed plugin rejected the scoped request'
    value = result.structuredContent
    if value is None:
        assert len(result.content) == 1 and result.content[0].type == 'text'
        value = json.loads(result.content[0].text)
    return value['result'] if isinstance(value, dict) and set(value) == {'result'} else value


def install_plugin(installation: dict, directory: Path, tls_context: ssl.SSLContext, ca: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=False)
    with httpx.Client(verify=tls_context, trust_env=False, timeout=60, follow_redirects=False) as client:
        response = client.get(installation['installer_url'])
        assert response.status_code == 200, 'Public installer unavailable'
        source = response.content
        assert hashlib.sha256(source).hexdigest() == installation['installer_sha256']
        installer = directory / 'installer.py'
        installer.write_bytes(source)
    environment = {key: value for key, value in os.environ.items() if key.upper() in {
        'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP',
        'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'PIP_INDEX_URL', 'PIP_EXTRA_INDEX_URL'}}
    config = directory / 'claude-config'
    config.mkdir()
    environment.update(CLAUDE_CONFIG_DIR=str(config), SSL_CERT_FILE=str(ca), PYTHONIOENCODING='utf-8')
    root = directory / 'installed'
    command = [sys.executable, str(installer), '--marketplace-url', installation['marketplace_url'],
        '--sha256', installation['marketplace_sha256'], '--package-name', installation['package_name'],
        '--marketplace-name', installation['marketplace_name'], '--install-root', str(root)]
    if installation['marketplace_url'].startswith('http://'):
        command.append('--allow-local-http')
    completed = subprocess.run(command, env=environment, capture_output=True, timeout=900)
    assert completed.returncode == 0, 'Trusted installer did not complete'
    target = root / (installation['package_name'] + '-' + installation['marketplace_sha256'][:16])
    local = json.loads((target / 'installation.json').read_text(encoding='utf-8'))
    assert local['marketplace_sha256'] == installation['marketplace_sha256']
    installed = config / 'plugins' / 'cache' / installation['marketplace_name'] / installation['package_name'] / installation['plugin_version']
    assert (installed / 'server.py').is_file(), 'Host did not install plugin'
    assert local['credentials_included'] is False
    print(json.dumps({'stage': 'installed', 'package_name': installation['package_name']}), flush=True)
    return installed, Path(local['local_mcp_interpreter'])


async def plugin_calls(installed: Path, interpreter: Path, credential: str,
        origin: str, ca: Path, selected: list[dict], replay: dict, cases: list[dict]) -> dict:
    environment = {key: value for key, value in os.environ.items()
        if key.upper() in {'PATH', 'SYSTEMROOT', 'TEMP', 'TMP'}}
    environment.update(SCENARIO_MCP_URL=origin + '/mcp', SCENARIO_API_KEY=credential,
        SSL_CERT_FILE=str(ca), PYTHONIOENCODING='utf-8')
    params = StdioServerParameters(command=str(interpreter), args=[str(installed / 'server.py')],
        env=environment, cwd=str(installed))
    result = {'cases': []}
    with open(os.devnull, 'w') as error_sink:
        async with asyncio.timeout(240):
            async with stdio_client(params, errlog=error_sink) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert {tool.name for tool in tools.tools} == {'list_scenario_capabilities',
                        'invoke_scenario_capability', 'get_scenario_receipt',
                        'read_scenario_approval', 'reply_scenario_interaction'}
                    found = document(await session.call_tool('list_scenario_capabilities', {}))
                    assert {(item['kind'], item['key']) for item in found} == {
                        (item['kind'], item['key']) for item in selected}
                    repeat = document(await session.call_tool('invoke_scenario_capability', replay['arguments']))
                    assert repeat['invocation_id'] == replay['invocation_id'], 'REST/MCP replay duplicated execution'
                    result['rest_to_mcp_same_invocation'] = True
                    for case in cases:
                        arguments = {'kind': case['kind'], 'key': case['key'], 'inputs': case['inputs'],
                            'idempotency_key': 'mcp-e2e-' + uuid4().hex}
                        current = document(await session.call_tool('invoke_scenario_capability', arguments))
                        deadline = monotonic() + 120
                        replied = False
                        while current['status'] not in TERMINAL:
                            if monotonic() >= deadline:
                                raise TimeoutError('Installed workflow did not settle')
                            if current['status'] == 'awaiting_approval':
                                assert not replied and case.get('decision') in {'approve', 'reject'}
                                advertised = current['delivery']['interactions']
                                assert len(advertised) == 1 and advertised[0]['kind'] == 'workflow_approval'
                                approval = advertised[0]
                                read = document(await session.call_tool('read_scenario_approval', {
                                    'invocation_id': current['invocation_id'], 'interaction_id': approval['id']}))
                                assert read, 'Approval read returned no business context'
                                verb = '同意' if case['decision'] == 'approve' else '驳回'
                                document(await session.call_tool('reply_scenario_interaction', {
                                    'invocation_id': current['invocation_id'], 'kind': 'approval',
                                    'interaction_id': approval['id'], 'text': verb + ' ' + approval['code'],
                                    'message_id': 'synthetic-e2e-' + uuid4().hex,
                                    'expected_revision': approval['revision']}))
                                replied = True
                            await asyncio.sleep(0.4)
                            current = document(await session.call_tool('get_scenario_receipt', {
                                'invocation_id': current['invocation_id']}))
                        expected_status = case.get('workflow_status', case['expected_status'])
                        assert current['status'] == expected_status, 'Installed business status differs'
                        expected = case.get('business_output', business_value(case['kind'], case['output']))
                        assert business_value(case['kind'], current['output']) == expected, 'Installed business result differs'
                        result['cases'].append({'role': case['role'], 'kind': case['kind'], **safe_receipt(current),
                            'approval_replied': replied})
                        if replied:
                            stale_reply = await session.call_tool('reply_scenario_interaction', {
                                'invocation_id': current['invocation_id'], 'kind': 'approval',
                                'interaction_id': approval['id'], 'text': verb + ' ' + approval['code'],
                                'message_id': 'stale-synthetic-' + uuid4().hex,
                                'expected_revision': approval['revision']})
                            assert stale_reply.isError, 'Closed approval accepted a stale reply'
                            result['cases'][-1]['closed_approval_reply_rejected'] = True
                    denied = await session.call_tool('invoke_scenario_capability', {
                        'kind': 'rule', 'key': '0' * 32, 'inputs': {}})
                    assert denied.isError
                    result.update(discovery=True, unpackaged_capability_rejected=True)
    return result


def verify_report(report: dict, args, other_scene: str) -> dict:
    scene_id = report.get('scenario_id', report.get('scene_id'))
    assert report['artifact_id'] == report['artifact']['id'] == report['publication']['artifact_id']
    assert report['artifact_hash'] == report['artifact']['artifact_hash']
    assert report['source_review']['files_hash'] == report['workspace']['files_hash']
    assert report['publication']['status'] == 'published' and report['publication']['available']
    installation = report['publication']['installation']
    selected = report['selected_capabilities']
    cases = report['business_acceptance']['cases']
    certificate = args.ca.resolve()
    context = ssl.create_default_context(cafile=str(certificate))
    installed, interpreter = install_plugin(installation, args.install_root / scene_id[:8], context, certificate)
    output = {'scenario_id': scene_id, 'release_id': report['release_id'],
        'artifact_id': report['artifact_id'], 'installation': 'actual_isolated_host_installation',
        'verified_tls': True, 'rest': [], 'credential_revoked': False}
    with actor_session(scene_id, args.username) as db:
        key = external_api.create_api_key(ExternalApiKeyCreateIn(scenario_id=scene_id,
            name='Synthetic multi-scenario protocol acceptance',
            scopes=['capabilities:read', 'capabilities:invoke'], expires_in_days=1), db=db)
    key_id, credential = key.id, key.token
    try:
        with httpx.Client(base_url=args.origin, headers={'X-API-Key': credential},
                verify=context, timeout=30, trust_env=False, follow_redirects=False) as client:
            catalog = client.get(API + '/scenarios/' + scene_id + '/capabilities',
                params={'release_id': report['release_id']})
            assert catalog.status_code == 200, 'Scoped discovery failed'
            assert client.get(API + '/scenarios/' + other_scene + '/capabilities').status_code == 404
            output['cross_scenario_rejected'] = True
            replay = None
            for case in cases:
                if case['kind'] == 'workflow' and case.get('decision'):
                    continue
                payload = {'release_id': report['release_id'], 'inputs': case['inputs'],
                    'expected_definition_hash': report['definition_hash'],
                    'idempotency_key': 'rest-e2e-' + uuid4().hex}
                path = API + '/scenarios/' + scene_id + '/capabilities/' + case['kind'] + '/' + case['key'] + '/invoke'
                response = client.post(path, json=payload)
                assert response.status_code == 200, 'Scoped invocation failed'
                current = wait_rest(client, response.json())
                expected_status = case.get('workflow_status', case['expected_status'])
                assert current['status'] == expected_status
                expected = case.get('business_output', business_value(case['kind'], case['output']))
                assert business_value(case['kind'], current['output']) == expected
                assert current['definition_hash'] == report['definition_hash']
                output['rest'].append({'role': case['role'], 'kind': case['kind'], **safe_receipt(current)})
                repeated = client.post(path, json=payload)
                assert repeated.status_code == 200 and repeated.json()['invocation_id'] == current['invocation_id']
                if replay is None and case['role'] == 'success':
                    replay = {'invocation_id': current['invocation_id'], 'arguments': {
                        'kind': case['kind'], 'key': case['key'], 'inputs': case['inputs'],
                        'idempotency_key': payload['idempotency_key']}}
                    different = next(item['inputs'] for item in cases if item['kind'] == case['kind']
                        and item['key'] == case['key'] and item['inputs'] != case['inputs'])
                    changed = {**payload, 'inputs': different}
                    conflict = client.post(path, json=changed)
                    assert conflict.status_code == 409
                    output['changed_input_rejected'] = conflict.status_code
                    malformed = client.post(path, json={**payload, 'inputs': {},
                        'idempotency_key': 'invalid-' + uuid4().hex})
                    assert malformed.status_code in {400, 422}
                    output['invalid_schema_rejected'] = malformed.status_code
                    stale = client.post(path, json={**payload,
                        'expected_definition_hash': '0' * 64, 'idempotency_key': 'stale-' + uuid4().hex})
                    assert stale.status_code == 409
                    output['stale_definition_rejected'] = True
            assert replay is not None
            output['plugin'] = asyncio.run(plugin_calls(installed, interpreter, credential,
                args.origin, certificate, selected, replay, cases))
            if args.client_spec:
                from scenario_e2e_client import client_environment, verify_client
                specification = json.loads(args.client_spec.read_text(encoding='utf-8'))
                assert specification['scenario_id'] == scene_id
                output['installed_client'] = verify_client(specification=specification,
                    installed=installed, interpreter=interpreter,
                    environment=client_environment(credential, args.origin, certificate),
                    client=client, actor_session=actor_session, scene_id=scene_id,
                    username=args.username, key_id=key_id, definition_hash=report['definition_hash'])
    finally:
        with actor_session(scene_id, args.username) as db:
            external_api.revoke_api_key(key_id, db=db)
        with httpx.Client(base_url=args.origin, headers={'X-API-Key': credential},
                verify=context, timeout=10, trust_env=False) as client:
            assert client.get(API + '/scenarios/' + scene_id + '/capabilities').status_code == 401
        output['credential_revoked'] = True
        credential = ''
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reports', nargs='+', required=True, type=Path)
    parser.add_argument('--username', required=True)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--ca', required=True, type=Path)
    parser.add_argument('--install-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--other-scene-id', help='Explicit other new scene when verifying one report')
    parser.add_argument('--client-spec', type=Path, help='Explicit reviewed installed-client acceptance fixture')
    args = parser.parse_args()
    args.install_root = args.install_root.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    values = [normalize_report(json.loads(path.read_text(encoding='utf-8'))) for path in args.reports]
    assert not args.client_spec or len(values) == 1
    result = {'protocol': 'real_rest_and_installed_stdio_mcp', 'scenarios': []}
    try:
        for index, report in enumerate(values):
            other = values[(index + 1) % len(values)]
            other_id = other.get('scene_id') if len(values) > 1 else args.other_scene_id
            assert other_id and other_id != report['scene_id'], 'A distinct explicit scenario is required'
            outcome = verify_report(report, args, other_id)
            result['scenarios'].append(outcome)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            print(json.dumps({'scenario_id': outcome['scenario_id'], 'protocol_verified': True,
                'credential_revoked': outcome['credential_revoked']}), flush=True)
    except BaseException as error:
        result['failure_type'] = type(error).__name__
        result['failure_frames'] = [{'file': Path(frame.filename).name, 'line': frame.lineno,
            'function': frame.name} for frame in traceback.extract_tb(error.__traceback__)[-6:]]
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'verification_failed': True, 'failure_type': type(error).__name__}), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
