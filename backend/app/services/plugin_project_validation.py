"""Task completion requires authored deliverables, not untouched templates."""
from __future__ import annotations

import re

from .plugin_coding_validation import EDITABLE_PATHS, validate_files
from .plugin_source_policy import editable_path


def required_paths(instruction: str) -> list[str]:
    mentioned = re.findall(r'(?:skills|scripts|examples|references|agents|commands|hooks|output-styles|lsp)/[a-zA-Z0-9_./-]+', instruction)
    return sorted(EDITABLE_PATHS | {path for path in mentioned if editable_path(path)})


def validate_project(document: dict) -> list[str]:
    contract = document['coding_contract']
    if isinstance(document.get('manifest'), dict) and 'delivery_profile' not in document['manifest']:
        contract = {key: value for key, value in contract.items() if key != 'delivery_profile'}
    issues = validate_files(document['files'], contract)
    authored = set(document.get('authored_paths', []))
    for path in document.get('required_paths', []):
        if path not in document['files'] or path not in authored:
            issues.append(f'任务尚未交付定制文件：{path}')
    return issues
