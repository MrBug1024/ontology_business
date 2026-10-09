"""Validate candidate source as data; never execute model-authored code."""
from __future__ import annotations

import ast
import re
import json
from jsonschema import Draft202012Validator
from .capability_contracts import canonical_hash, canonical_json
from .plugin_source_policy import MAX_PROJECT_FILES, check_client_script, check_reference, check_skill, editable_path
from .plugin_client_contract import check_invocation_options, client_contract
from .plugin_host_profile import require_manifest_host

EDITABLE_PATHS = frozenset({'skills/run-scenario/SKILL.md', 'README.md', 'examples/invoke.py'})
MAX_SOURCE_BYTES = 128 * 1024
_SECRET = re.compile(r'\bsk-[A-Za-z0-9_-]{8,}|(?i:bearer\s+[A-Za-z0-9_.-]{12,})|(?i:(?:api_key|password|secret)\s*[=:]\s*["\'])[A-Za-z0-9_-]{12,}')


def safe_text(text: str) -> None:
    if '\x00' in text or _SECRET.search(text):
        raise ValueError('内容含不可交付的密钥格式或无效字符，请移除后重试')


def files_hash(files: dict[str, str]) -> str:
    return canonical_hash(files, domain='scenario-plugin-source-v1')


def _example_inputs(call: ast.Call, capability: dict) -> list[str]:
    if not isinstance(call.args[2], ast.Dict):
        return ['示例必须直接提供明确标记为合成值的 typed 输入字典']
    try:
        values = json.loads(canonical_json(ast.literal_eval(call.args[2])))
    except (ValueError, TypeError, SyntaxError, RecursionError):
        return ['示例输入必须是可验证的 JSON 合成值']
    schema = capability.get('input_schema')
    if schema is not None:
        error = next(Draft202012Validator(schema).iter_errors(values), None)
        if error:
            if error.validator == 'required' and isinstance(error.instance, dict):
                missing = [key for key in error.validator_value if key not in error.instance]
                return ['示例输入缺少必填字段：' + '、'.join(str(key)[:80] for key in missing[:8])]
            return [f'示例输入不符合已发布输入契约（校验规则：{error.validator}）']
    return []


def check_example(source: str, manifest: dict) -> list[str]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return ['Python 接入示例语法不成立']
    allowed = {(item['kind'], item['key']): item for item in manifest['capabilities']}
    parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    calls = []
    forbidden = (ast.ClassDef, ast.Lambda, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor,
                 ast.While, ast.Try, ast.Delete, ast.Global, ast.Nonlocal, ast.Raise)
    for node in ast.walk(tree):
        if isinstance(node, forbidden):
            return ['接入示例只允许直接的异步调用，不支持任意执行逻辑']
        if isinstance(node, ast.Import) and any(item.name not in {'asyncio', 'json'} or item.asname for item in node.names):
            return ['接入示例仅允许 asyncio/json 和受信场景客户端']
        if isinstance(node, ast.ImportFrom) and (node.module != 'server' or node.level
                or any(item.name != 'invoke_scenario_capability' or item.asname for item in node.names)):
            return ['接入示例只能从 server 导入 invoke_scenario_capability']
        if isinstance(node, ast.Attribute) and (not isinstance(node.value, ast.Name)
                or (node.value.id, node.attr) not in {('asyncio', 'run'), ('json', 'dumps')}):
            return ['接入示例含非受支持的属性访问']
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (node.decorator_list or node.name != 'main'):
            return ['接入示例只能定义无装饰器的 main']
        if isinstance(node, ast.Name) and node.id.startswith('__') and node.id != '__name__':
            return ['接入示例禁止访问运行环境内部对象']
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else (
                f'{node.func.value.id}.{node.func.attr}' if isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) else '')
            if name not in {'main', 'print', 'asyncio.run', 'json.dumps', 'invoke_scenario_capability'}:
                return ['接入示例含非受支持的调用']
            if name == 'invoke_scenario_capability':
                if len(node.args) < 3 or not all(isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in node.args[:2]):
                    return ['示例调用必须明确能力类型、稳定标识和本次 typed 输入']
                if tuple(arg.value for arg in node.args[:2]) not in allowed:
                    return ['示例调用了未纳入本插件的能力']
                options = check_invocation_options(node, allowed[tuple(arg.value for arg in node.args[:2])], manifest.get('client_contract'))
                if options:
                    return options
                if not isinstance(parents.get(id(node)), ast.Await):
                    return ['异步场景客户端必须 await，否则示例不会实际调用能力']
                issues = _example_inputs(node, allowed[tuple(arg.value for arg in node.args[:2])])
                if issues:
                    return issues
                calls.append(node)
    return [] if calls else ['Python 接入示例缺少实际场景客户端调用']


def validate_files(files: dict[str, str], manifest: dict) -> list[str]:
    issues = []
    if 'delivery_profile' in manifest:
        try:
            if manifest['delivery_profile'] is None:
                raise ValueError('插件交付规范身份缺失')
            require_manifest_host(manifest)
        except ValueError as exc:
            issues.append(str(exc))
    if not EDITABLE_PATHS.issubset(files):
        return ['缺少入口 Skill、README 或可验证的 Python 接入示例']
    if len(files) > MAX_PROJECT_FILES or any(not editable_path(path, legacy_blueprint='delivery_profile' not in manifest) for path in files):
        return ['文件路径越界或超过项目文件数量上限']
    if sum(len(value.encode('utf-8')) for value in files.values()) > MAX_SOURCE_BYTES:
        return ['源文件总字节超过构建上限']
    for path, content in files.items():
        try:
            safe_text(content)
        except ValueError as exc:
            issues.append(f'{path}：{exc}')
        if not content.strip():
            issues.append(f'{path} 为空')
        if len(content) > 32768:
            issues.append(f'{path} 超过单文件上限')
        if manifest.get('host') == 'codex' and any(marker in content for marker in (
                '${CLAUDE_PLUGIN_ROOT}', 'claude --plugin-dir', 'claude plugin ', 'codex plugin install', 'codex plugin validate')):
            issues.append(f'{path}：Codex 插件不能使用其他宿主变量或不支持的安装命令，请依据当前宿主契约修正')
        if path.endswith('/SKILL.md'):
            issues.extend(check_skill(path, content))
        elif path.startswith('scripts/'):
            issues.extend(f'{path}：{issue}' for issue in check_client_script(content, manifest))
        elif path.startswith('examples/') and path != 'examples/invoke.py':
            issues.extend(f'{path}：{issue}' for issue in check_example(content, manifest))
        elif path.endswith('.json'):
            issues.extend(check_reference(path, content))
        client = manifest.get('client_contract')
        if client and path.endswith('.md'):
            environment = client.get('environment') or client_contract()['environment']
            unsupported = set(re.findall(r'SCENARIO_[A-Z0-9_]+', content)) - set(environment)
            if unsupported:
                issues.append(f'{path}：引用了客户端未支持的环境变量：' + '、'.join(sorted(unsupported)))
    skill = files['skills/run-scenario/SKILL.md']
    for marker in ('invoke_scenario_capability', 'get_scenario_receipt', 'SCENARIO_API_KEY'):
        if marker not in skill + files['README.md']:
            issues.append(f'使用合同缺少必要说明：{marker}')
    issues.extend(check_example(files['examples/invoke.py'], manifest))
    return issues
