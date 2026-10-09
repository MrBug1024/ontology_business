"""Versioned host selection without changing published Claude profile bytes."""
from __future__ import annotations

from copy import deepcopy

from .plugin_delivery_profile import delivery_profile as claude_profile
from .plugin_delivery_profile import require_delivery_profile as require_claude_profile

HOST_LABELS = {'claude_code': 'Claude Code', 'codex': 'Codex'}
CODEX_PROFILE_VERSION = 'scenario-plugin-delivery-profile.v2'
CODEX_PLUGIN_STANDARD = 'https://developers.openai.com/plugins/build/plugins'


def require_host(host: str) -> str:
    if host not in HOST_LABELS:
        raise ValueError('插件宿主不可用，请选择受支持的宿主')
    return host


def package_name(release_id: str, host: str) -> str:
    require_host(host)
    # Keep Claude identifiers unchanged; distinct Codex identities prevent a
    # reviewed version on one host from replacing the same version on another.
    return f'scenario-{release_id}' + ('-codex' if host == 'codex' else '')


def delivery_profile(host: str = 'claude_code') -> dict:
    require_host(host)
    profile = claude_profile()
    if host == 'claude_code':
        return profile
    profile['version'] = CODEX_PROFILE_VERSION
    profile['host'] = {'key': 'codex', 'label': 'Codex', 'scope': 'host_specific'}
    profile['standards'][1] = {
        'key': 'openai_plugins', 'label': 'OpenAI / Codex 插件', 'url': CODEX_PLUGIN_STANDARD,
        'purpose': '使用官方支持的 Codex 兼容清单、技能与 MCP 配置，交付本地插件源；不代表已提交官方目录或支持 ChatGPT 网页运行。',
    }
    profile['components'][-2]['purpose'] = '宿主可支持 hooks；本平台交付范围禁用，候选文本不能成为任意宿主执行入口。'
    profile['platform_rules'].append({
        'key': 'local_host_registration', 'label': '本地源与实际安装',
        'purpose': '安装器分别注册本地源并调用受支持的 Codex CLI 安装；用户在新聊天中检查并启用，注册不证明已安装，安装不证明已授权或业务完成。',
    })
    return profile


def require_delivery_profile(value: object) -> dict | None:
    if isinstance(value, dict) and value.get('version') == CODEX_PROFILE_VERSION:
        expected = delivery_profile('codex')
        if value != expected:
            raise ValueError('Codex 插件交付规范身份不匹配，不能替换已有产物')
        return deepcopy(expected)
    return require_claude_profile(value)


def require_manifest_host(manifest: dict) -> str:
    host = require_host(manifest.get('host', 'claude_code'))
    if 'delivery_profile' in manifest and manifest['delivery_profile'] is None:
        raise ValueError('插件交付规范身份缺失，不能构建')
    profile = require_delivery_profile(manifest.get('delivery_profile'))
    if (host == 'codex' and profile is None) or (profile is not None and profile['host']['key'] != host):
        raise ValueError('插件宿主与交付规范身份不一致，不能构建')
    return host
