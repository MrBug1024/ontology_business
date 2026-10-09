"""Produce host-specific catalogs from the exact reviewed plugin archive."""
from __future__ import annotations

import io
import json
import zipfile

from .plugin_coding_distribution import marketplace_artifact as claude_marketplace
from .plugin_host_profile import require_host


def marketplace_artifact(plugin: bytes, name: str, version: str, *, host: str = 'claude_code') -> bytes:
    require_host(host)
    if host == 'claude_code':
        return claude_marketplace(plugin, name, version)
    marketplace = f'{name}-marketplace'
    index = {'name': marketplace, 'interface': {'displayName': 'Scenario capability plugins'},
        'plugins': [{'name': name, 'source': {'source': 'local', 'path': f'./plugins/{name}'},
            'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_INSTALL'}, 'category': 'Productivity'}]}
    guide = (
        f'# Codex 本地插件源\n\n插件：{name}；版本：{version}。\n\n'
        f'1. 解压并注册本地源：`codex plugin marketplace add ./{marketplace}`。\n'
        f'2. 安装：`codex plugin add {name}@{marketplace}`；也可在桌面插件目录中手动安装。在新聊天中检查并启用。\n'
        '3. 注册源不等于安装成功；插件安装不等于业务执行成功。业务凭据需由使用者单独配置。\n'
        '4. Git 自托管时发布本目录到有权使用的仓库，使用官方 marketplace add 命令注册该仓库。\n'
        '5. 本包未提交 OpenAI 官方公开目录，不承诺 ChatGPT 网页支持；更新需新的人工审阅版本。\n'
    )
    entries = {f'{marketplace}/.agents/plugins/marketplace.json': json.dumps(index, ensure_ascii=False, indent=2),
               f'{marketplace}/DISTRIBUTION.md': guide}
    with zipfile.ZipFile(io.BytesIO(plugin)) as original:
        for path in original.namelist():
            entries[f'{marketplace}/plugins/{path}'] = original.read(path)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path, value in sorted(entries.items()):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, value.encode('utf-8') if isinstance(value, str) else value)
    return output.getvalue()
