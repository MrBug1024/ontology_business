"""Trusted standalone installer; no business execution or credential collection."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from time import monotonic
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4
import venv
import zipfile

MAX_ARCHIVE_BYTES = 4 * 1024 * 1024
MAX_EXTRACTED_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 256
TOKEN_PATTERN = re.compile(r'[a-z0-9][a-z0-9-]{0,179}')
HASH_PATTERN = re.compile(r'[a-f0-9]{64}')
PROCESS_ENVIRONMENT = frozenset({
    'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP',
    'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'LANG', 'LC_ALL',
    'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE', 'HTTPS_PROXY',
    'HTTP_PROXY', 'NO_PROXY', 'PIP_CONFIG_FILE', 'PIP_INDEX_URL',
    'PIP_EXTRA_INDEX_URL', 'CLAUDE_CONFIG_DIR',
})


class InstallationError(Exception):
    pass


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def checked_url(value: str, allow_local_http: bool) -> str:
    url = urlsplit(value)
    try:
        local = url.hostname == 'localhost' or bool(url.hostname and ipaddress.ip_address(url.hostname).is_loopback)
    except ValueError:
        local = False
    if (not url.hostname or url.username or url.password or url.query or url.fragment
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise InstallationError('安装源地址无效')
    if url.scheme != 'https' and not (url.scheme == 'http' and local and allow_local_http):
        raise InstallationError('安装源必须使用 HTTPS；HTTP 仅允许明确的本机试装')
    return value


def download_archive(url: str, expected_hash: str, target: Path, allow_local_http: bool) -> None:
    if not HASH_PATTERN.fullmatch(expected_hash):
        raise InstallationError('安装包 SHA-256 无效')
    request = Request(checked_url(url, allow_local_http), headers={'Accept': 'application/zip'})
    digest = hashlib.sha256()
    count = 0
    started = monotonic()
    with build_opener(NoRedirects()).open(request, timeout=60) as response, target.open('wb') as output:
        while chunk := response.read(64 * 1024):
            if monotonic() - started > 60:
                raise InstallationError('安装包下载超时')
            count += len(chunk)
            if count > MAX_ARCHIVE_BYTES:
                raise InstallationError('安装包超过大小上限')
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != expected_hash:
        raise InstallationError('安装包 SHA-256 不匹配，已拒绝安装')


def extract_archive(archive_path: Path, destination: Path, marketplace_name: str) -> Path:
    total = 0
    seen: set[str] = set()
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise InstallationError('安装包文件数超过上限')
        for info in entries:
            path = PurePosixPath(info.filename)
            raw_name = info.orig_filename
            if (path.is_absolute() or not path.parts or path.parts[0] != marketplace_name
                    or '..' in path.parts or '\\' in raw_name or ':' in raw_name or '\x00' in raw_name
                    or info.filename in seen or stat.S_ISLNK(info.external_attr >> 16)):
                raise InstallationError('安装包包含不安全路径')
            seen.add(info.filename)
            total += info.file_size
            if total > MAX_EXTRACTED_BYTES:
                raise InstallationError('安装包解压大小超过上限')
            output = destination.joinpath(*path.parts)
            if not output.resolve().is_relative_to(destination.resolve()):
                raise InstallationError('安装包路径超出安装目录')
            if info.is_dir():
                output.mkdir(parents=True, exist_ok=True)
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, output.open('xb') as target:
                remaining = info.file_size
                while chunk := source.read(min(64 * 1024, remaining + 1)):
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise InstallationError('安装包文件大小与声明不符')
                    target.write(chunk)
                if remaining:
                    raise InstallationError('安装包文件不完整')
    return destination / marketplace_name


def check_marketplace(root: Path, package_name: str, marketplace_name: str) -> Path:
    index = json.loads((root / '.claude-plugin' / 'marketplace.json').read_text(encoding='utf-8'))
    if (index.get('name') != marketplace_name or len(index.get('plugins', [])) != 1
            or index['plugins'][0].get('name') != package_name
            or index['plugins'][0].get('source') != f'./plugins/{package_name}'):
        raise InstallationError('市场目录没有指向所选插件')
    plugin = root / 'plugins' / package_name
    metadata = json.loads((plugin / '.claude-plugin' / 'plugin.json').read_text(encoding='utf-8'))
    if metadata.get('name') != package_name:
        raise InstallationError('插件身份与市场条目不符')
    checksums = json.loads((plugin / 'checksums.json').read_text(encoding='utf-8'))
    files = {path.relative_to(plugin).as_posix(): path for path in plugin.rglob('*') if path.is_file()}
    if set(checksums) != set(files) - {'checksums.json'}:
        raise InstallationError('插件内容清单不完整')
    for name, checksum in checksums.items():
        if not isinstance(checksum, str) or not HASH_PATTERN.fullmatch(checksum):
            raise InstallationError('插件校验值无效')
        if hashlib.sha256(files[name].read_bytes()).hexdigest() != checksum:
            raise InstallationError('插件文件校验失败')
    config = json.loads((plugin / '.mcp.json').read_text(encoding='utf-8'))
    expected = {'command': 'python', 'args': ['${CLAUDE_PLUGIN_ROOT}/server.py'], 'env': {
        'SCENARIO_MCP_URL': '${SCENARIO_MCP_URL}', 'SCENARIO_API_KEY': '${SCENARIO_API_KEY}'}}
    if config != {'mcpServers': {'scenario': expected}}:
        raise InstallationError('插件受信 MCP 运行配置不匹配')
    return plugin


def default_install_root() -> Path:
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local')))
    else:
        base = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local' / 'share')))
    return base / 'scenario-plugins'


def prepare_local_copy(source: Path, target: Path, interpreter: Path, package_name: str) -> Path:
    local = target / 'marketplace'
    target.mkdir(parents=True, exist_ok=True)
    if local.is_symlink() or not local.resolve().is_relative_to(target.resolve()):
        raise InstallationError('本地市场目录超出专属安装目录')
    # Every retry starts with this round's hash-verified source. A marker alone
    # cannot establish that a previous local server or Skill is still trusted.
    with tempfile.TemporaryDirectory(prefix='.marketplace-stage-', dir=target) as temporary:
        fresh = Path(temporary) / 'marketplace'
        shutil.copytree(source, fresh)
        plugin = fresh / 'plugins' / package_name
        config_path = plugin / '.mcp.json'
        config = json.loads(config_path.read_text(encoding='utf-8'))
        config['mcpServers']['scenario']['command'] = str(interpreter.resolve())
        config_path.write_text(json.dumps(config, indent=2), encoding='utf-8')
        checksums_path = plugin / 'checksums.json'
        checksums = json.loads(checksums_path.read_text(encoding='utf-8'))
        checksums['.mcp.json'] = hashlib.sha256(config_path.read_bytes()).hexdigest()
        checksums_path.write_text(json.dumps(checksums, sort_keys=True, indent=2), encoding='utf-8')
        previous = target / f'.marketplace-previous-{uuid4().hex}'
        if local.exists():
            local.rename(previous)
        try:
            fresh.rename(local)
        except OSError:
            if previous.exists():
                previous.rename(local)
            raise
        if previous.exists():
            if previous.is_symlink() or not previous.resolve().is_relative_to(target.resolve()):
                raise InstallationError('旧本地市场目录超出专属安装目录')
            shutil.rmtree(previous)
    return local


def run_checked(arguments: list[str], *, env: dict[str, str]) -> None:
    subprocess.run(arguments, check=True, env=env, timeout=300)


def install(args) -> Path:
    if sys.version_info[:2] != (3, 12):
        raise InstallationError('请在 Python 3.12 环境中执行安装命令')
    if not TOKEN_PATTERN.fullmatch(args.package_name) or not TOKEN_PATTERN.fullmatch(args.marketplace_name):
        raise InstallationError('插件或市场标识无效')
    if not HASH_PATTERN.fullmatch(args.sha256):
        raise InstallationError('安装包 SHA-256 无效')
    claude = shutil.which('claude')
    if not claude:
        raise InstallationError('请先安装 Claude Code 并确保 claude 在 PATH 中')
    base = Path(args.install_root or os.environ.get('SCENARIO_PLUGIN_INSTALL_ROOT') or default_install_root()).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    target = base / f'{args.package_name}-{args.sha256[:16]}'
    if target.is_symlink() or target.is_junction() or not target.resolve().is_relative_to(base):
        raise InstallationError('专属安装目录超出指定安装根或包含链接')
    lock = base / f'{target.name}.lock'
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise InstallationError('此插件正在安装；若上次异常退出，请检查并移除该安装目录旁的 .lock 文件') from None
    os.close(descriptor)
    try:
        return install_locked(args, target, base, claude)
    finally:
        lock.unlink(missing_ok=True)


def install_locked(args, target: Path, base: Path, claude: str) -> Path:
    with tempfile.TemporaryDirectory(prefix='scenario-install-', dir=base) as temporary:
        stage = Path(temporary)
        archive = stage / 'marketplace.zip'
        download_archive(args.marketplace_url, args.sha256, archive, args.allow_local_http)
        source = extract_archive(archive, stage / 'source', args.marketplace_name)
        plugin = check_marketplace(source, args.package_name, args.marketplace_name)
        marker = target / '.package-sha256'
        if target.exists():
            if not marker.is_file() or marker.read_text(encoding='ascii') != args.sha256:
                raise InstallationError('现有安装目录身份不符；请使用新的 --install-root 目录')
        else:
            target.mkdir()
            shutil.copytree(source, target / 'source')
            marker.write_text(args.sha256, encoding='ascii')
        environment = {key: value for key, value in os.environ.items() if key.upper() in PROCESS_ENVIRONMENT}
        environment['PYTHONIOENCODING'] = 'utf-8'
        environment['PYTHONNOUSERSITE'] = '1'
        virtual = target / 'venv'
        if virtual.is_symlink() or virtual.is_junction() or not virtual.resolve().is_relative_to(target.resolve()):
            raise InstallationError('Python 虚拟环境目录超出专属安装目录或包含链接')
        interpreter = virtual / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if not interpreter.is_file():
            venv.EnvBuilder(with_pip=True).create(virtual)
        run_checked([str(interpreter), '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
                     '-r', str(plugin / 'requirements.txt')], env=environment)
        local = prepare_local_copy(source, target, interpreter, args.package_name)
        run_checked([claude, 'plugin', 'validate', str(local / 'plugins' / args.package_name)], env=environment)
        run_checked([claude, 'plugin', 'validate', str(local)], env=environment)
        run_checked([claude, 'plugin', 'marketplace', 'add', str(local), '--scope', 'user'], env=environment)
        run_checked([claude, 'plugin', 'install', f'{args.package_name}@{args.marketplace_name}', '--scope', 'user'],
                    env=environment)
        (target / 'installation.json').write_text(json.dumps({
            'marketplace_sha256': args.sha256, 'original_source': 'source',
            'local_mcp_interpreter': str(interpreter.resolve()), 'credentials_included': False,
        }, indent=2), encoding='utf-8')
        print(f'安装完成。在 Claude Code 中使用 /{args.package_name}:run-scenario。')
        print('业务调用前请另外配置 SCENARIO_MCP_URL 与场景专属 SCENARIO_API_KEY；安装不执行业务。')
        return target


def main() -> int:
    parser = argparse.ArgumentParser(description='Install a published scenario plugin into Claude Code')
    parser.add_argument('--marketplace-url', required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--package-name', required=True)
    parser.add_argument('--marketplace-name', required=True)
    parser.add_argument('--install-root')
    parser.add_argument('--allow-local-http', action='store_true')
    try:
        install(parser.parse_args())
        return 0
    except InstallationError as error:
        print(f'安装未完成：{error}', file=sys.stderr)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, zipfile.BadZipFile):
        print('安装未完成：下载、依赖或宿主校验失败；未执行任何业务调用。', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
