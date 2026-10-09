"""Bounded plugin projects; generated code is data, never platform executable."""
from __future__ import annotations

import ast
import json
import re
from .plugin_client_contract import check_invocation_options
from .plugin_skill_contract import validate_skill
from .plugin_delivery_profile import BLUEPRINT_REFERENCE_PATH, PROTECTED_REFERENCE_PATHS

MAX_PROJECT_FILES = 32
_PATH = re.compile(r'^(?:README\.md|skills/[a-z][a-z0-9-]{0,63}/(?:SKILL\.md|references/[a-z0-9_-]+\.md)|(?:examples|scripts)/[a-z][a-z0-9_]{0,63}\.py|references/[a-z][a-z0-9_-]{0,63}\.(?:md|json))$')


def editable_path(path: str, *, legacy_blueprint: bool = False) -> bool:
    # Legacy immutable packages could contain a user-authored file at this name.
    # Only restoring their original validation semantics may retain that file.
    protected = path in PROTECTED_REFERENCE_PATHS and not (legacy_blueprint and path == BLUEPRINT_REFERENCE_PATH)
    return not protected and bool(_PATH.fullmatch(path))


def check_skill(path: str, source: str) -> list[str]:
    return validate_skill(path, source)


def check_client_script(source: str, contract: dict) -> list[str]:
    """Validate pure client orchestration, with the trusted adapter as its only I/O."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return ['调用脚本 Python 语法不成立']
    functions = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    tools = {'invoke_scenario_capability', 'get_scenario_receipt'}
    allowed = {(item['kind'], item['key']) for item in contract['capabilities']}
    parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    invocations = 0
    forbidden = (ast.ClassDef, ast.Lambda, ast.With, ast.AsyncWith, ast.While, ast.AsyncFor,
                 ast.Try, ast.Delete, ast.Global, ast.Nonlocal, ast.Raise)
    for node in ast.walk(tree):
        if isinstance(node, forbidden):
            return ['脚本仅支持纯输入处理与受信客户端调用，不支持系统执行或无界重试']
        if isinstance(node, ast.Import) and any(item.name not in {'asyncio', 'json'} or item.asname for item in node.names):
            return ['脚本仅允许 asyncio/json 和受信场景客户端']
        if isinstance(node, ast.ImportFrom) and (node.module != 'server' or node.level
                or any(item.name not in tools or item.asname for item in node.names)):
            return ['脚本只能从 server 导入已注册的场景调用与回执工具']
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.decorator_list:
            return ['脚本不能使用装饰器']
        if isinstance(node, ast.Name) and node.id.startswith('__') and node.id != '__name__':
            return ['脚本不能访问运行环境内部对象']
        if isinstance(node, ast.Attribute):
            if not isinstance(node.value, ast.Name) or (node.value.id, node.attr) not in {('asyncio', 'run'), ('json', 'dumps'), ('json', 'loads')}:
                return ['脚本不能访问环境、网络或任意对象属性']
        if isinstance(node, ast.For):
            if not (isinstance(node.iter, (ast.List, ast.Tuple)) and len(node.iter.elts) <= 100):
                return ['脚本循环必须使用不超过 100 项的明确列表；业务异步等待由宿主处理']
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else (
                f'{node.func.value.id}.{node.func.attr}' if isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) else '')
            if name not in functions | tools | {'print', 'len', 'str', 'int', 'bool', 'dict', 'list', 'asyncio.run', 'json.dumps', 'json.loads'}:
                return ['脚本含非受支持的调用']
            if name in tools and not isinstance(parents.get(id(node)), ast.Await):
                return ['场景客户端调用必须 await']
            if name == 'invoke_scenario_capability':
                if len(node.args) < 3 or not all(isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in node.args[:2]):
                    return ['脚本必须固定能力类型与稳定标识；仅本次 typed 输入可由参数提供']
                if tuple(arg.value for arg in node.args[:2]) not in allowed:
                    return ['脚本调用了未纳入本插件的能力']
                capability = next(item for item in contract['capabilities'] if (item['kind'], item['key']) == tuple(arg.value for arg in node.args[:2]))
                options = check_invocation_options(node, capability, contract.get('client_contract'))
                if options:
                    return options
                if not isinstance(node.args[2], (ast.Dict, ast.Name)):
                    return ['脚本输入必须为 typed 字典或显式参数，由平台再次校验']
                invocations += 1
    return [] if invocations else ['调用脚本缺少实际场景能力调用']


def check_reference(path: str, source: str) -> list[str]:
    if not path.endswith('.json'):
        return []
    try:
        json.loads(source, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError):
        return [f'{path}：JSON 资料格式无效']
    return []
