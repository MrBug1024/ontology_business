"""Validate portable Skill metadata and the generated client's execution boundary."""
from __future__ import annotations

import re
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from .plugin_delivery_profile import READ_ONLY_SKILL_TOOLS


_FRONTMATTER = re.compile(r'^---\r?\n(.*?)\r?\n---(?:\r?\n|$)', re.DOTALL)
_SKILL_NAME = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
_READ_ONLY_SCENARIO_TOOL = re.compile(
    r'^(?:mcp__scenario__|mcp__plugin_[a-z][a-z0-9-]{0,63}_scenario__)?'
    r'(?:' + '|'.join(re.escape(name) for name in READ_ONLY_SKILL_TOOLS) + r')$'
)


class _UniqueSkillLoader(yaml.SafeLoader):
    """Duplicate YAML fields cannot silently change the reviewed metadata."""


def _unique_mapping(loader: _UniqueSkillLoader, node: MappingNode, deep: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ConstructorError(None, None, 'Skill metadata keys must be unique strings', key_node.start_mark)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueSkillLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _text_field(metadata: dict, key: str, maximum: int | None = None) -> bool:
    value = metadata.get(key)
    return isinstance(value, str) and bool(value.strip()) and (maximum is None or len(value) <= maximum)


def _safe_tool_permissions(value: object) -> bool:
    if isinstance(value, str):
        tools = [item for item in re.split(r'[\s,]+', value.strip()) if item]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        tools = value
    else:
        return False
    return all(_READ_ONLY_SCENARIO_TOOL.fullmatch(tool) for tool in tools)


def load_frontmatter(source: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse a closed YAML frontmatter block shared by host-standard files.

    Returns (metadata, error). (None, None) means the source has no frontmatter
    block at all; (None, message) reports an invalid one.
    """
    header = _FRONTMATTER.match(source)
    if header is None:
        return None, None
    try:
        metadata = yaml.load(header.group(1), Loader=_UniqueSkillLoader)
    except (yaml.YAMLError, ValueError, RecursionError):
        return None, 'frontmatter 须为安全 YAML，字段不能重复'
    if not isinstance(metadata, dict):
        return None, 'frontmatter 必须为字段映射'
    return metadata, None


def validate_skill(path: str, source: str) -> list[str]:
    metadata, error = load_frontmatter(source)
    if error is not None:
        return [f'{path}：Skill {error}']
    if metadata is None:
        return [f'{path}：Skill 必须提供闭合的 YAML frontmatter']
    name = metadata.get('name')
    if (not isinstance(name, str) or not 1 <= len(name) <= 64
            or not _SKILL_NAME.fullmatch(name) or name != path.split('/')[1]):
        return [f'{path}：Skill 名称须匹配目录，使用 1–64 个小写字母、数字或单个短横线']
    issues = []
    if not _text_field(metadata, 'description', 1024):
        issues.append(f'{path}：Skill description 必须为 1–1024 字符的说明和触发条件')
    if 'compatibility' in metadata and not _text_field(metadata, 'compatibility', 500):
        issues.append(f'{path}：Skill compatibility 必须为 1–500 字符的环境要求')
    if 'license' in metadata and not _text_field(metadata, 'license'):
        issues.append(f'{path}：Skill license 必须为许可证名称或引用文本')
    if 'metadata' in metadata:
        extra = metadata['metadata']
        if not isinstance(extra, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                              for key, value in extra.items()):
            issues.append(f'{path}：Skill metadata 必须为字符串键和值的映射')
    if 'allowed-tools' in metadata and not _safe_tool_permissions(metadata['allowed-tools']):
        # These fields grant host permissions; generated prose must not add a
        # shell executor or silently preapprove a business effect.
        issues.append(f'{path}：Skill allowed-tools 只能精确授权场景发现、回执和审批读取工具')
    if 'hooks' in metadata:
        issues.append(f'{path}：Skill 不能定义自动运行的宿主 hooks，执行入口必须保留受信客户端边界')
    if '!`' in source:
        issues.append(f'{path}：Skill 不支持自动执行 shell 的动态上下文；运行调用必须经受信场景客户端')
    return issues
