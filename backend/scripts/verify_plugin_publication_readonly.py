"""Read-only verification of real reviewed artifacts using their owning session."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def verify(workspace_id: str, package_directory: Path | None = None) -> dict:
    from fastapi import HTTPException
    from sqlalchemy import event, text
    from app.database import SessionLocal
    from app.models import AssistantThread
    from app.plugin_coding_schemas import PluginArtifactDownload
    from app.services import plugin_artifact_catalog as catalog, permission_service

    with SessionLocal() as db:
        db.execute(text('SET TRANSACTION READ ONLY'))
        thread = db.get(AssistantThread, workspace_id)
        if thread is None or not thread.scope_key.startswith('plugin-coding:'):
            raise ValueError('Must select an existing plugin coding workspace')
        db.info.update(tenant_id=thread.tenant_id, user_id=thread.created_by_user_id)
        permission_service.require_principal(db)
        count = []
        def query_count(*args):
            count.append(1)
        event.listen(db.get_bind(), 'before_cursor_execute', query_count)
        started = perf_counter()
        try:
            page = catalog.list_artifacts(db, scenario_id=thread.scenario_id, offset=0, limit=50)
        finally:
            event.remove(db.get_bind(), 'before_cursor_execute', query_count)
        metrics = {'query_count': len(count), 'elapsed_ms': round((perf_counter() - started) * 1000, 3)}
        values = [item for item in page.items if item.workspace_id == workspace_id]
        if not values:
            raise ValueError('Workspace has no reviewed version')
        if package_directory is not None:
            package_directory.mkdir(parents=True, exist_ok=False)
        results = []
        for value in values:
            request = PluginArtifactDownload(artifact_hash=value.artifact_hash)
            name, artifact = catalog.download_artifact(db, value.id, request)
            assert hashlib.sha256(artifact).hexdigest() == value.artifact_hash
            market_name, market = catalog.download_artifact(db, value.id, request.model_copy(update={'format': 'marketplace'}))
            assert market_name.endswith('-marketplace') and market != artifact
            if package_directory is not None:
                (package_directory / f'plugin-{value.plugin_version}.zip').write_bytes(artifact)
                (package_directory / f'marketplace-{value.plugin_version}.zip').write_bytes(market)
            try:
                catalog.download_artifact(db, value.id, request.model_copy(update={'artifact_hash': '0' * 64}))
            except HTTPException as exc:
                assert exc.status_code == 409
            else:
                raise AssertionError('Mismatched identity was accepted')
            results.append({'version': value.plugin_version, 'artifact_sha256': value.artifact_hash,
                            'bytes': len(artifact), 'marketplace_bytes': len(market)})
        identity = values[0].id
        db.info.pop('permission_cache', None)
        db.info.update(tenant_id=uuid4().hex)
        try:
            catalog.get_artifact(db, identity)
        except HTTPException as exc:
            assert exc.status_code in {403, 404}
        else:
            raise AssertionError('Foreign tenant read was accepted')
        db.info.clear()
        try:
            catalog.get_artifact(db, identity)
        except HTTPException as exc:
            assert exc.status_code == 401
        else:
            raise AssertionError('Anonymous read was accepted')
        return {'read_only': True, 'reviewed_versions': results, 'catalog': metrics,
                'identity_conflict_rejected': True, 'foreign_tenant_rejected': True, 'anonymous_rejected': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace-id', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--package-directory', type=Path, help='New local directory for verified installation packages')
    args = parser.parse_args()
    try:
        result = verify(args.workspace_id, args.package_directory)
    except Exception as exc:
        print(f'Plugin publication verification failed: {type(exc).__name__}', file=sys.stderr)
        raise SystemExit(1) from None
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
