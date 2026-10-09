"""One read projection of trusted plugin files plus current candidate overlays."""
from __future__ import annotations

import io
import zipfile

from .plugin_source_policy import editable_path
from .plugin_host_artifact import build_artifact


def project_files(document: dict) -> list[dict]:
    base = build_artifact(document['manifest'], plugin_version=document['plugin_version'])
    files = []
    with zipfile.ZipFile(io.BytesIO(base)) as archive:
        for full_path in archive.namelist():
            path = full_path.split('/', 1)[1]
            if path == 'checksums.json':
                continue
            content = archive.read(full_path).decode('utf-8')
            files.append({'path': path, 'content': document['files'].get(path, content) if editable_path(path) else content,
                          'previous': document.get('previous', {}).get(path, ''), 'editable': editable_path(path)})
    existing_paths = {item['path'] for item in files}
    files.extend({'path': path, 'content': content, 'previous': document.get('previous', {}).get(path, ''),
                  'editable': editable_path(path)} for path, content in sorted(document['files'].items())
                 if path not in existing_paths)
    return files
