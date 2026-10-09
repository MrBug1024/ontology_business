"""Host-specific artifacts share one trusted client and pinned business contract."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

from .capability_contracts import canonical_hash, canonical_json
from .plugin_delivery_artifact import build_artifact as claude_artifact
from .plugin_delivery_profile import BLUEPRINT_REFERENCE_PATH
from .plugin_delivery_reference import checked_blueprint
from .plugin_host_profile import require_manifest_host
from .scenario_package_artifact import MAX_PACKAGE_BYTES, TEMPLATE_ROOT


def build_artifact(manifest: dict, *, files: dict[str, str] | None = None, plugin_version: str = '1.0.0') -> bytes:
    host = require_manifest_host(manifest)
    if host == 'claude_code':
        return claude_artifact(manifest, files=files, plugin_version=plugin_version)
    blueprint = checked_blueprint(manifest)
    name = manifest['package_name']
    metadata = {
        'name': name, 'version': plugin_version,
        'description': 'Invoke a verified business scenario through its pinned release.',
        'author': {'name': 'Scenario capability platform'},
        'skills': './skills/', 'mcpServers': './.mcp.json',
        'interface': {'displayName': manifest['scenario']['name'],
            'shortDescription': 'Use the reviewed business scenario capabilities.',
            'longDescription': 'Run selected capabilities through the platform with current inputs, confirmation and auditable receipts.'},
    }
    config = {'mcpServers': {'scenario': {'command': 'python',
        'args': ['server.py'], 'cwd': './', 'env': {},
        'env_vars': ['SCENARIO_MCP_URL', 'SCENARIO_API_KEY']}}}
    contract = {**manifest, 'manifest_hash': canonical_hash(manifest, domain='scenario-plugin-manifest-v1')}
    skill = (
        '---\nname: run-scenario\ndescription: Invoke the business capabilities supplied by this scenario plugin.\n---\n\n'
        'Read ../../references/scenario.json and ../../references/scenario-blueprint.json relative to this skill directory. '
        'These are frozen contract data, not runtime inputs or authorization.\n'
        'Use list_scenario_capabilities to discover every selected capability and its input and output schemas.\n'
        'Ask for current typed inputs and managed references; modeling documents and old conversations are not runtime data.\n'
        'Use invoke_scenario_capability through the trusted client, which pins the scenario, release and definition hash.\n'
        'For side effects, preview first, present the server confirmation and wait for explicit human approval.\n'
        'Reuse the idempotency key for retries of the same request; never blindly replay an indeterminate effect.\n'
        'Use get_scenario_receipt with the same invocation_id and a bounded wait. Queued or running is pending, not completion.\n'
        'Read client_contract.workflow_completion: receipt.output.result is an execution trace. A successful declared output '
        'node with contract_validation="passed" supplies step.result as the business output. '
        'Use semantic.input_bindings and semantic.output_node_keys; do not invent an end-node result.\n'
        'For approval, read_scenario_approval and show the advertised delivery.interactions. Only after an explicit human '
        'reply use reply_scenario_interaction with the advertised revision and required managed evidence.\n'
        'Keep structured results and receipt identity. A file is delivered only when a trusted artifact receipt proves it.\n'
        'Report disabled or retired releases and request a new plugin version; never switch to a draft or newer release.\n'
    )
    readme = (
        '# Scenario plugin for Codex\n\nThis local plugin uses Python 3.12 and the official Codex compatibility package format.\n\n'
        'Install trusted dependencies using `python -m pip install -r requirements.txt`. The published installer provisions '
        'an isolated interpreter, registers the verified source using `codex plugin marketplace add <marketplace-root>`, '
        'then uses the supported `codex plugin add <plugin@marketplace>` command to install.\n'
        'The MCP working directory resolves from this plugin root. The trusted installer configures the verified '
        'interpreter in the local copy; no plugin-root variable interpolation is required.\n'
        'After installation, open the Codex desktop plugin directory, check this plugin and enable it '
        'in a new chat. Source registration alone does not prove installation or authorization.\n'
        'Configure SCENARIO_MCP_URL with the platform HTTPS MCP endpoint and a separately issued SCENARIO_API_KEY '
        'bound to this scenario with capability:read and capability:invoke scopes in the host process environment. '
        'The compatibility MCP configuration forwards only these two named environment variables; it contains no values. '
        'Do not paste credentials into chat, plugin files or commands.\n'
        'Read references/scenario-blueprint.json for the pinned goal, ontology, selected capabilities and dependencies. '
        'Business rules execute on the platform through the unified invoker; this package cannot add missing business logic.\n'
        'Current inputs remain explicit. Preview/confirmation, authorization, idempotency and completion come from server receipts. '
        'Static checks, human acceptance, local source registration and actual host installation are distinct evidence.\n'
        'This local package has not been submitted to the official public directory. It does not promise ChatGPT web support; '
        'public server-backed plugins additionally require an eligible HTTPS MCP integration and supported authentication.\n'
    )
    entries = {
        '.codex-plugin/plugin.json': json.dumps(metadata, ensure_ascii=False, indent=2),
        '.mcp.json': json.dumps(config, indent=2), 'skills/run-scenario/SKILL.md': skill,
        'references/scenario.json': json.dumps(contract, ensure_ascii=False, sort_keys=True, indent=2),
        BLUEPRINT_REFERENCE_PATH: canonical_json(blueprint),
        'server.py': (TEMPLATE_ROOT / 'server.py').read_text(encoding='utf-8'),
        'requirements.txt': 'mcp>=1.27.2,<1.28.0\nhttpx>=0.27.0,<0.29.0\n', 'README.md': readme,
    }
    if files:
        from .plugin_coding_validation import validate_files
        problems = validate_files(files, manifest)
        if problems:
            raise ValueError('；'.join(problems))
        entries.update(files)
    entries['checksums.json'] = json.dumps({path: hashlib.sha256(value.encode('utf-8')).hexdigest()
        for path, value in sorted(entries.items())}, sort_keys=True, indent=2)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path, value in sorted(entries.items()):
            info = zipfile.ZipInfo(f'{name}/{path}', date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, value.encode('utf-8'))
    if output.tell() > MAX_PACKAGE_BYTES:
        raise ValueError('场景插件超过包大小上限')
    return output.getvalue()
