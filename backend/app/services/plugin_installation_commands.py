"""Installation commands use explicit origins and fixed trusted installer bytes."""
from __future__ import annotations

import ipaddress
from pathlib import Path
import shlex
from urllib.parse import urlsplit

from ..plugin_publication_schemas import PluginInstallationOut

INSTALLER_PATH = Path(__file__).resolve().parents[2] / 'plugin_templates' / 'scenario_install.py'
MAX_INSTALLER_BYTES = 64 * 1024


def installer_bytes() -> bytes:
    value = INSTALLER_PATH.read_bytes()
    if len(value) > MAX_INSTALLER_BYTES:
        raise ValueError('受信安装器超过大小上限')
    return value


def powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def installation_info(settings, *, identity: str, package_name: str, version: str, marketplace_hash: str,
                      installer_hash: str) -> PluginInstallationOut:
    base = settings.plugin_public_base_url.rstrip('/')
    root = f'{base}{settings.api_prefix}/published-plugins/{identity}'
    marketplace_url, installer_url = f'{root}/marketplace.zip', f'{root}/installer.py'
    digest = installer_hash
    marketplace_name = f'{package_name}-marketplace'
    parsed = urlsplit(base)
    try:
        local = parsed.hostname == 'localhost' or bool(parsed.hostname and ipaddress.ip_address(parsed.hostname).is_loopback)
    except ValueError:
        local = False
    insecure_local = parsed.scheme == 'http'
    arguments = ['--marketplace-url', marketplace_url, '--sha256', marketplace_hash,
                 '--package-name', package_name, '--marketplace-name', marketplace_name]
    if insecure_local:
        arguments.append('--allow-local-http')
    ps_args = ' '.join(powershell_quote(value) for value in arguments)
    sh_args = ' '.join(shlex.quote(value) for value in arguments)
    powershell = '\n'.join([
        "$scenarioPluginInstaller = Join-Path ([System.IO.Path]::GetTempPath()) ('scenario-plugin-' + [guid]::NewGuid().ToString('N') + '.py')",
        f'Invoke-WebRequest -Uri {powershell_quote(installer_url)} -OutFile $scenarioPluginInstaller -MaximumRedirection 0 -TimeoutSec 60 -ErrorAction Stop',
        f"if ((Get-FileHash -LiteralPath $scenarioPluginInstaller -Algorithm SHA256).Hash.ToLowerInvariant() -ne '{digest}') {{ throw '插件安装器 SHA-256 不匹配，已停止' }}",
        f'python $scenarioPluginInstaller {ps_args}',
        "if ($LASTEXITCODE -ne 0) { throw '插件安装未完成，请查看上方原因' }",
        'Remove-Item -LiteralPath $scenarioPluginInstaller',
    ])
    protocol = '=http,https' if insecure_local else '=https'
    check = "import hashlib,sys; sys.exit(0 if hashlib.sha256(open(sys.argv[1], 'rb').read()).hexdigest() == sys.argv[2] else 'Plugin installer SHA-256 mismatch')"
    bash = '\n'.join([
        '(', '  set -eu', '  scenario_plugin_installer="$(mktemp)"',
        "  trap 'rm -f \"$scenario_plugin_installer\"' EXIT",
        f'  curl --fail --silent --show-error --proto {shlex.quote(protocol)} --max-time 60 --output "$scenario_plugin_installer" {shlex.quote(installer_url)}',
        f'  python3 -c {shlex.quote(check)} "$scenario_plugin_installer" {digest}',
        f'  python3 "$scenario_plugin_installer" {sh_args}', ')',
    ])
    return PluginInstallationOut(scope='local_test' if local else 'public', package_name=package_name,
        marketplace_name=marketplace_name, plugin_version=version, marketplace_url=marketplace_url,
        marketplace_sha256=marketplace_hash, installer_url=installer_url, installer_sha256=digest,
        powershell_command=powershell, bash_command=bash, usage_command=f'/{package_name}:run-scenario',
        requirements=['Python 3.12（python / python3 在 PATH 中）', 'Claude Code 2.1.208 或兼容版本',
                      '安装方可访问安装源和 Python 依赖仓库'],
        configuration_notes=['安装器会创建此插件独享的 Python 虚拟环境，并校验来源与文件。',
            '安装后另行配置 SCENARIO_MCP_URL（HTTPS）与场景专属 SCENARIO_API_KEY；不把密钥写入安装命令。',
            '需要指定安装目录时，在安装前配置 SCENARIO_PLUGIN_INSTALL_ROOT；默认使用用户的插件专属目录。',
            '本机地址仅能在运行此平台的设备上试装。' if local else '插件安装不授予场景执行权限；调用由平台统一校验。'])
