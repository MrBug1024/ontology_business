"""Reproducible artifacts from a trusted adapter template and closed manifest."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import zipfile

from .capability_contracts import canonical_hash


TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / 'plugin_templates' / 'scenario_mcp'
MAX_PACKAGE_BYTES = 2 * 1024 * 1024


def build_artifact(manifest: dict, *, files: dict[str, str] | None = None, plugin_version: str = '1.0.0') -> bytes:
    name = manifest['package_name']
    content = {**manifest, 'manifest_hash': canonical_hash(manifest, domain='scenario-plugin-manifest-v1')}
    plugin = {'name': name, 'version': plugin_version,
        'description': 'Invoke a verified business scenario through its pinned release.',
        'author': {'name': 'Scenario capability platform'}}
    config = {'mcpServers': {'scenario': {'command': 'python',
        'args': ['${CLAUDE_PLUGIN_ROOT}/server.py'],
        'env': {'SCENARIO_MCP_URL': '${SCENARIO_MCP_URL}', 'SCENARIO_API_KEY': '${SCENARIO_API_KEY}'}}}}
    skill = (
        '---\nname: run-scenario\ndescription: Invoke the business capabilities supplied by this scenario plugin.\n---\n\n'
        'Read ${CLAUDE_PLUGIN_ROOT}/references/scenario.json as contract data. '
        'Use list_scenario_capabilities to discover input and output schemas.\n'
        'Ask for required current inputs; modeling documents and old conversations are not runtime data.\n'
        'Use invoke_scenario_capability. The adapter pins the scenario, release and definition hash.\n'
        'For side effects, preview first, present the returned confirmation, and confirm only after explicit user approval.\n'
        'Reuse the idempotency key for retries of the same execution. Never replay an indeterminate effect blindly.\n'
        'Use get_scenario_receipt. A queued workflow is still running; report pending approvals or failures honestly.\n'
        'For a pending approval, read_scenario_approval and present its instructions to the user. '
        'Only after an explicit human reply use reply_scenario_interaction with the advertised revision and required managed evidence.\n'
        'Keep the structured business output and receipt identity. Deliver requested files only after a trusted artifact receipt exists.\n'
        'Report a disabled or retired release and request a new plugin version; do not switch to a draft or newer release.\n'
    )
    readme = (
        '# Scenario plugin\n\nThis package uses Python 3.12 and Claude Code. Unzip into its own directory.\n\n'
        'Install the trusted client dependencies with `python -m pip install -r requirements.txt`.\n'
        'Set SCENARIO_MCP_URL to the platform HTTPS MCP endpoint and SCENARIO_API_KEY to a separately issued '
        'credential bound to this business scenario with capabilities:read and capabilities:invoke scopes.\n'
        'Do not paste credentials into chat or package files. Start `claude --plugin-dir ./<package-directory>` '
        'and use the run-scenario skill. Run `claude plugin validate ./<package-directory>` to check installation structure.\n\n'
        'The bundle includes no customer runtime data or credentials. Business execution remains on the platform '
        'through the unified invoker. This adapter cannot add missing business logic or enable a release.\n'
        'Acceptance in references/scenario.json records manual review of three distinct server receipts per capability; '
        'it is not an automatic proof of every business behavior.\n'
    )
    entries = {
        '.claude-plugin/plugin.json': json.dumps(plugin, ensure_ascii=False, indent=2),
        '.mcp.json': json.dumps(config, indent=2),
        'skills/run-scenario/SKILL.md': skill,
        'references/scenario.json': json.dumps(content, ensure_ascii=False, sort_keys=True, indent=2),
        'server.py': (TEMPLATE_ROOT / 'server.py').read_text(encoding='utf-8'),
        'requirements.txt': 'mcp>=1.27.2,<1.28.0\nhttpx>=0.27.0,<0.29.0\n',
        'README.md': readme,
    }
    if files:
        from .plugin_coding_validation import validate_files
        problems = validate_files(files, manifest)
        if problems:
            raise ValueError('；'.join(problems))
        entries.update(files)
    entries['checksums.json'] = json.dumps({path: hashlib.sha256(value.encode()).hexdigest()
        for path, value in sorted(entries.items())}, sort_keys=True, indent=2)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path, value in sorted(entries.items()):
            info = zipfile.ZipInfo(f'{name}/{path}', date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, value.encode())
    if stream.tell() > MAX_PACKAGE_BYTES:
        raise ValueError('场景插件超过包大小上限')
    return stream.getvalue()
