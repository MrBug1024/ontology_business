"""Validate a synthetic plugin's installation files and actual stdio handshake.

This never invokes a business capability and requires no credential or database.
Run Claude Code's own `plugin validate` against the emitted directory as well.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.services.scenario_package_artifact import build_artifact
from app.services.plugin_coding_distribution import marketplace_artifact


EXPECTED_TOOLS = {
    'list_scenario_capabilities', 'invoke_scenario_capability',
    'get_scenario_receipt', 'read_scenario_approval', 'reply_scenario_interaction',
}


async def verify_stdio(directory: Path) -> list[str]:
    parameters = StdioServerParameters(command=sys.executable,
        args=[str(directory / 'server.py')], env={'PYTHONDONTWRITEBYTECODE': '1'})
    async with asyncio.timeout(30):
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                tools = await client.list_tools()
    names = sorted(tool.name for tool in tools.tools)
    if set(names) != EXPECTED_TOOLS:
        raise ValueError('Generated plugin does not expose its expected protocol tools')
    return names


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New, empty output directory')
    parser.add_argument('--reviewed-coding', action='store_true', help='Use explicit synthetic reviewed source files and emit a Marketplace')
    arguments = parser.parse_args()
    destination = arguments.output.resolve()
    if destination.exists():
        raise ValueError('Use a new output directory; previous evidence is never overwritten')
    manifest = {'package_version': 'scenario-plugin.v1', 'package_name': 'scenario-synthetic',
        'purpose': 'installation_protocol_smoke_only',
        'deployment': {'release_id': 'a' * 32, 'definition_hash': 'c' * 64},
        'scenario': {'id': 'b' * 32, 'name': 'Synthetic protocol fixture'},
        'capabilities': [{'kind': 'function', 'key': 'fixture_check', 'name': 'Protocol fixture'}]}
    sources = None
    if arguments.reviewed_coding:
        sources = {'skills/run-scenario/SKILL.md': '---\nname: run-scenario\ndescription: Invoke the synthetic verified scenario\n---\nUse invoke_scenario_capability, get_scenario_receipt, preview and human confirmation.\n',
            'README.md': '# Synthetic reviewed plugin\nConfigure SCENARIO_API_KEY externally and SCENARIO_MCP_URL for the trusted platform.\nInstall requirements.txt. Run python -m examples.invoke from the package root.\n',
            'examples/invoke.py': 'import asyncio\nfrom server import invoke_scenario_capability\n\nasync def main():\n    result = await invoke_scenario_capability("function", "fixture_check", {"synthetic": True})\n    print(result)\n\nif __name__ == "__main__":\n    asyncio.run(main())\n'}
    artifact = build_artifact(manifest, files=sources, plugin_version='1.2.3' if sources else '1.0.0')
    if artifact != build_artifact(manifest, files=sources, plugin_version='1.2.3' if sources else '1.0.0'):
        raise ValueError('Identical manifests generated different artifacts')
    destination.mkdir(parents=True)
    (destination / 'scenario-synthetic.zip').write_bytes(artifact)
    with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
        for name in archive.namelist():
            target = (destination / name).resolve()
            if not target.is_relative_to(destination):
                raise ValueError('Generated artifact escaped the output directory')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
    tools = asyncio.run(verify_stdio(destination / 'scenario-synthetic'))
    if sources:
        marketplace = marketplace_artifact(artifact, 'scenario-synthetic', '1.2.3')
        (destination / 'scenario-synthetic-marketplace.zip').write_bytes(marketplace)
        with zipfile.ZipFile(io.BytesIO(marketplace)) as archive:
            for name in archive.namelist():
                target = (destination / name).resolve()
                if not target.is_relative_to(destination):
                    raise ValueError('Marketplace escaped the output directory')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
    report = {'purpose': manifest['purpose'], 'business_execution_tested': False,
        'artifact_sha256': hashlib.sha256(artifact).hexdigest(), 'reproducible': True,
        'stdio_initialize': 'passed', 'tools': tools}
    if sources:
        report.update(candidate_origin='synthetic', reviewed_source_packaged=True, marketplace_emitted=True)
    (destination / 'validation.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
