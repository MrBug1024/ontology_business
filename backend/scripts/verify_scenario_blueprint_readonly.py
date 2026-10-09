"""Check exact released semantic contexts with an explicitly selected account.

Uses the ordinary permission and release services in a read-only PostgreSQL
transaction. It does not create workspaces, model candidates or business data.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
from pathlib import Path
import re
import sys
from time import perf_counter
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)


def resource_id(value: str) -> str:
    if re.fullmatch(r'[a-f0-9]{32}', value) is None:
        raise argparse.ArgumentTypeError('A complete resource ID is required')
    return value


def _public_context(db, release_id: str, workspace_id: str | None) -> dict:
    from app.plugin_coding_schemas import CodingContextOut
    from app.services import plugin_authoring_context, plugin_coding_contract, plugin_coding_workspace

    if workspace_id:
        row = plugin_coding_workspace.owned_root(db, workspace_id)
        contract = plugin_coding_contract.authoring_contract(db, row.proposal['manifest'])
        value = {key: contract[key] for key in CodingContextOut.model_fields}
    else:
        value = plugin_authoring_context.discover_context(db, release_id)
    return CodingContextOut.model_validate(value).model_dump(mode='json')


def _candidate_artifact(db, release, workspace_id: str, context: dict) -> dict:
    from app.scenario_package_schemas import ScenarioPackageBuild
    from app.services import plugin_coding_workspace, scenario_package_service

    row = plugin_coding_workspace.owned_root(db, workspace_id)
    document = row.proposal
    if document['phase'] != 'released':
        raise ValueError('Only previously human-reviewed business evidence is supported')
    request = ScenarioPackageBuild.model_validate(document['acceptance_request'])
    if not request.confirmed_business_acceptance:
        raise ValueError('The stored business evidence has not been confirmed')
    name, artifact = scenario_package_service.build_package(db, release.id, request)
    with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
        reference = json.loads(archive.read(f'{name}/references/scenario-blueprint.json'))
        contract = json.loads(archive.read(f'{name}/references/scenario.json'))
        checksums = json.loads(archive.read(f'{name}/checksums.json'))
        assert reference == context['scenario_blueprint']
        assert contract['scenario_blueprint'] == reference
        assert contract['delivery_profile'] == context['delivery_profile']
        for path, checksum in checksums.items():
            assert hashlib.sha256(archive.read(f'{name}/{path}')).hexdigest() == checksum
        assert 'references/scenario-blueprint.json' in checksums
        skill = archive.read(f'{name}/skills/run-scenario/SKILL.md').decode('utf-8')
        assert 'client_contract.workflow_completion' in skill
        assert 'semantic.input_bindings' in skill and 'semantic.output_node_keys' in skill
        readme = archive.read(f'{name}/README.md').decode('utf-8')
        assert 'capability:read' in readme and 'capability:invoke' in readme
        assert 'capabilities:read' not in readme and 'capabilities:invoke' not in readme
    return {'profile_version': contract['delivery_profile']['version'],
            'artifact_sha256': hashlib.sha256(artifact).hexdigest(), 'bytes': len(artifact),
            'protected_reference_matches': True, 'checksums_verified': len(checksums),
            'published': False, 'installed': False}


def verify(user_identity: str, release_ids: list[str], workspace_ids: list[str], *, verify_build: bool = False) -> dict:
    from sqlalchemy import event, or_, select, text

    from app.database import SessionLocal
    from app.models import AssistantThread, OntologyRelease, User
    from app.services import permission_service

    results = []
    with SessionLocal() as db:
        db.execute(text('SET TRANSACTION READ ONLY'))
        users = db.scalars(select(User).where(
            or_(User.display_name == user_identity, User.email == user_identity))).all()
        if len(users) != 1:
            raise ValueError('The explicitly selected account must resolve uniquely')
        targets = [(release_id, None) for release_id in release_ids]
        for workspace_id in workspace_ids:
            thread = db.get(AssistantThread, workspace_id)
            if (thread is None or thread.created_by_user_id != users[0].id
                    or not thread.scope_key.startswith('plugin-coding:')):
                raise ValueError('The explicitly selected workspace is unavailable to this account')
            targets.append((thread.scope_key.removeprefix('plugin-coding:'), workspace_id))
        for release_id, workspace_id in targets:
            release = db.get(OntologyRelease, release_id)
            if release is None:
                raise ValueError('The explicitly selected release is unavailable')
            db.info.clear()
            db.info.update(user_id=users[0].id, tenant_id=release.tenant_id)
            permission_service.require_principal(db)
            query_count = []

            def record_query(*args):
                query_count.append(1)

            connection = db.connection()
            event.listen(connection, 'before_cursor_execute', record_query)
            started = perf_counter()
            try:
                payload = _public_context(db, release_id, workspace_id)
            finally:
                event.remove(connection, 'before_cursor_execute', record_query)
            blueprint = payload['scenario_blueprint']
            assert blueprint is not None
            assert blueprint['deployment'] == payload['deployment']
            assert blueprint['deployment']['release_id'] == release_id
            assert blueprint['deployment']['definition_source'] == 'release'
            assert blueprint['completeness'] == 'complete_authorized_projection'
            assert payload['delivery_profile']['host']['key'] == 'claude_code'
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
            assert len(encoded) <= 128 * 1024
            result = {
                'scenario_name': payload['scenario']['name'],
                'release_id': release_id,
                'scope': 'workspace_selected' if workspace_id else 'release_discovery',
                'blueprint_version': blueprint['version'],
                'object_count': len(blueprint['ontology']['objects']),
                'relation_count': len(blueprint['ontology']['relations']),
                'capability_count': len(payload['capabilities']),
                'dependency_count': len(blueprint['coverage']['dependencies']),
                'context_bytes': len(encoded),
                'query_count': len(query_count),
                'elapsed_ms': round((perf_counter() - started) * 1000, 3),
            }
            if verify_build and workspace_id:
                result['candidate_build'] = _candidate_artifact(db, release, workspace_id, payload)
            results.append(result)
    return {'read_only': True, 'explicit_account_verified': True, 'releases': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', required=True)
    parser.add_argument('--release-id', action='append', default=[], type=resource_id)
    parser.add_argument('--workspace-id', action='append', default=[], type=resource_id)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--verify-build', action='store_true',
                        help='Build in-memory candidates from existing reviewed business evidence; never publish')
    arguments = parser.parse_args()
    if not arguments.release_id and not arguments.workspace_id:
        parser.error('Provide an explicit release or workspace')
    try:
        result = verify(arguments.user, arguments.release_id, arguments.workspace_id, verify_build=arguments.verify_build)
    except Exception as exc:
        print(f'Blueprint verification failed: {type(exc).__name__}', file=sys.stderr)
        raise SystemExit(1) from None
    arguments.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
