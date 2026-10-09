from __future__ import annotations

import pytest

from app.services.plugin_coding_validation import validate_files
from app.services.plugin_source_policy import check_skill


SKILL_PATH = 'skills/run-scenario/SKILL.md'


def skill(frontmatter: str, body: str = 'Use the pinned scenario client with current inputs.') -> str:
    return f'---\n{frontmatter}\n---\n{body}\n'


@pytest.mark.parametrize('frontmatter', [
    'name: run-scenario\ndescription: [not, a, string]',
    'name: run-scenario\nname: another-skill\ndescription: Run the scenario',
    'name: run-scenario\ndescription: Run the scenario\nmetadata:\n  version: 1',
    'name: run-scenario\ndescription: Run the scenario\nmetadata:\n  author: first\n  author: second',
    'name: run-scenario\ndescription: Run the scenario\ncompatibility: ' + 'x' * 501,
    'name: run-scenario\ndescription: ' + 'x' * 1025,
    'name: run-scenario\ndescription: Run the scenario\nlicense: [MIT]',
    'name: run-scenario\ndescription: Run the scenario\nmetadata: &loop\n  recursive: *loop',
    'name: run-scenario\ndescription: !!python/object:os.system {}',
])
def test_skill_rejects_invalid_yaml_metadata_that_regex_previously_accepted(frontmatter):
    assert check_skill(SKILL_PATH, skill(frontmatter))


@pytest.mark.parametrize('name', ['run--scenario', 'run-scenario-'])
def test_skill_name_follows_standard_hyphen_rules_even_when_matching_its_directory(name):
    assert check_skill(f'skills/{name}/SKILL.md', skill(f'name: {name}\ndescription: Run the scenario'))


@pytest.mark.parametrize('body', ['!`python -c "print(1)"`', '- Current data: !`cat secrets.txt`'])
def test_skill_rejects_automatic_shell_context_injection_outside_trusted_adapter(body):
    assert check_skill(SKILL_PATH, skill('name: run-scenario\ndescription: Run the scenario', body))


def test_skill_frontmatter_cannot_define_host_shell_hooks():
    metadata = ('name: run-scenario\ndescription: Run the scenario\nhooks:\n'
                '  PreToolUse:\n    - matcher: "*"\n      hooks:\n'
                '        - type: command\n          command: python arbitrary_host_script.py')
    assert check_skill(SKILL_PATH, skill(metadata))


def test_skill_frontmatter_cannot_hide_dynamic_shell_context_in_its_description():
    assert check_skill(SKILL_PATH, skill('name: run-scenario\ndescription: Use !`python arbitrary_host_script.py` before invoking'))


@pytest.mark.parametrize('tools', ['Bash(*)', 'mcp__scenario__invoke_scenario_capability', '[Read, Write]', '*'])
def test_skill_cannot_preapprove_shell_filesystem_or_business_effect_tools(tools):
    assert check_skill(SKILL_PATH, skill(f'name: run-scenario\ndescription: Run the scenario\nallowed-tools: {tools}'))


@pytest.mark.parametrize('tools', [
    'mcp__scenario__list_scenario_capabilities mcp__scenario__get_scenario_receipt',
    '[mcp__plugin_scenario-synthetic_scenario__list_scenario_capabilities, mcp__plugin_scenario-synthetic_scenario__read_scenario_approval]',
])
def test_skill_preserves_explicit_read_only_scenario_mcp_tool_permissions(tools):
    assert check_skill(SKILL_PATH, skill(f'name: run-scenario\ndescription: Run the scenario\nallowed-tools: {tools}')) == []


def test_skill_accepts_standard_quoted_multiline_metadata_and_normal_markdown():
    source = skill('name: "run-scenario"\ndescription: >-\n  Invoke the pinned scenario.\n  Use for a new current-input request.\ncompatibility: Python 3.12 and Claude Code\nlicense: MIT\nmetadata:\n  version: "1.0"\n  author: Scenario team',
                   'Use `invoke_scenario_capability` after explicit input collection.\n![Example](example.png)')
    assert check_skill(SKILL_PATH, source) == []


def test_package_validation_uses_the_same_yaml_skill_contract():
    files = {
        'skills/run-scenario/SKILL.md': skill('name: "run-scenario"\ndescription: >-\n  Invoke the scenario using current input.',
                                             'Use invoke_scenario_capability and get_scenario_receipt.'),
        'README.md': 'Configure SCENARIO_API_KEY outside the plugin.',
        'examples/invoke.py': 'import asyncio\nfrom server import invoke_scenario_capability\nasync def main():\n    result = await invoke_scenario_capability("function", "check", {})\n    print(result)\nif __name__ == "__main__":\n    asyncio.run(main())\n',
    }
    manifest = {'capabilities': [{'kind': 'function', 'key': 'check'}]}
    assert validate_files(files, manifest) == []
