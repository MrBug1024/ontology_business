"""Assemble trusted host installers while retaining existing published bytes."""
from __future__ import annotations

from pathlib import Path

from .plugin_host_profile import require_host
from .plugin_installation_commands import MAX_INSTALLER_BYTES
from .plugin_installation_commands import installation_info as claude_info
from .plugin_installation_commands import installer_bytes as claude_installer


def installer_bytes(host: str = 'claude_code') -> bytes:
    require_host(host)
    original = claude_installer()
    if host == 'claude_code':
        return original
    source = original.decode('utf-8')
    guard = "if __name__ == '__main__':"
    if source.count(guard) != 1 or not source.endswith("    raise SystemExit(main())\n"):
        raise ValueError('受信安装器组合入口已变化，不能发布')
    overlay = (Path(__file__).resolve().parents[2] / 'plugin_templates/scenario_codex_install.py').read_text(encoding='utf-8')
    combined = source.rsplit(guard, 1)[0] + '\n' + overlay + "\n\nif __name__ == '__main__':\n    raise SystemExit(main())\n"
    encoded = combined.encode('utf-8')
    if len(encoded) > MAX_INSTALLER_BYTES:
        raise ValueError('受信安装器超过大小上限')
    return encoded


def installation_info(settings, *, identity: str, package_name: str, version: str, marketplace_hash: str,
                      installer_hash: str, host: str = 'claude_code'):
    require_host(host)
    result = claude_info(settings, identity=identity, package_name=package_name, version=version,
                        marketplace_hash=marketplace_hash, installer_hash=installer_hash)
    if host == 'claude_code':
        return result
    return result.model_copy(update={
        'host': 'codex', 'usage_command': f'在 Codex 新聊天中选择 {package_name} 插件，使用其 run-scenario 技能',
        'requirements': ['Python 3.12（python / python3 在 PATH 中）', '支持 plugin add 与 marketplace add 的 Codex CLI',
                         '安装方可访问安装源和 Python 依赖仓库'],
        'configuration_notes': ['安装命令校验下载内容、配置独立 Python 环境、注册本地源并调用 Codex CLI 安装。',
            '如当前 Codex CLI 不支持插件 add，请先更新到受支持版本；也可手动在桌面插件目录安装本地源中的包。',
            '安装后在新聊天中检查并启用；在宿主环境单独配置 SCENARIO_MCP_URL 和场景专属 SCENARIO_API_KEY。仅转发这两个变量名，包和安装命令不包含值。',
            '本包适用于 Codex 本地宿主，未提交官方公开目录；安装成功不证明业务调用验收。'],
    })
