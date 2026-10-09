"""Trusted Codex overlay, assembled with the shared standalone installer."""

PROCESS_ENVIRONMENT = PROCESS_ENVIRONMENT | {'CODEX_HOME'}


def check_marketplace(root: Path, package_name: str, marketplace_name: str) -> Path:
    index = json.loads((root / '.agents/plugins/marketplace.json').read_text(encoding='utf-8'))
    expected_entry = {'name': package_name, 'source': {'source': 'local', 'path': f'./plugins/{package_name}'},
        'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_INSTALL'}, 'category': 'Productivity'}
    if index.get('name') != marketplace_name or index.get('plugins') != [expected_entry]:
        raise InstallationError('市场目录没有指向所选 Codex 插件')
    plugin = root / 'plugins' / package_name
    metadata = json.loads((plugin / '.codex-plugin/plugin.json').read_text(encoding='utf-8'))
    if (metadata.get('name') != package_name or metadata.get('skills') != './skills/'
            or metadata.get('mcpServers') != './.mcp.json' or not isinstance(metadata.get('interface'), dict)):
        raise InstallationError('Codex 插件身份与安装清单不符')
    reference = json.loads((plugin / 'references/scenario.json').read_text(encoding='utf-8'))
    if reference.get('host') != 'codex' or reference.get('package_name') != package_name:
        raise InstallationError('插件场景契约的宿主或身份不符')
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
    if config != {'mcpServers': {'scenario': {'command': 'python', 'args': ['server.py'], 'cwd': './', 'env': {},
            'env_vars': ['SCENARIO_MCP_URL', 'SCENARIO_API_KEY']}}}:
        raise InstallationError('Codex 插件受信 MCP 运行配置不匹配')
    return plugin


def install(args) -> Path:
    if sys.version_info[:2] != (3, 12):
        raise InstallationError('请在 Python 3.12 环境中执行安装命令')
    if not TOKEN_PATTERN.fullmatch(args.package_name) or not TOKEN_PATTERN.fullmatch(args.marketplace_name):
        raise InstallationError('插件或市场标识无效')
    if not HASH_PATTERN.fullmatch(args.sha256):
        raise InstallationError('安装包 SHA-256 无效')
    codex = shutil.which('codex')
    if not codex:
        raise InstallationError('请先安装支持插件的 Codex CLI 并确保 codex 在 PATH 中')
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
        return install_locked(args, target, base, codex)
    finally:
        lock.unlink(missing_ok=True)


def install_locked(args, target: Path, base: Path, codex: str) -> Path:
    with tempfile.TemporaryDirectory(prefix='scenario-codex-install-', dir=base) as temporary:
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
        run_checked([codex, 'plugin', 'marketplace', 'add', str(local), '--json'], env=environment)
        # These are the CLI's advertised plugin commands. The host validates
        # its manifest while installing; no undocumented validate command runs.
        run_checked([codex, 'plugin', 'add', f'{args.package_name}@{args.marketplace_name}', '--json'], env=environment)
        (target / 'installation.json').write_text(json.dumps({
            'status': 'installed', 'plugin_installed': True, 'marketplace_sha256': args.sha256,
            'original_source': 'source', 'local_mcp_interpreter': str(interpreter.resolve()),
            'credentials_included': False, 'business_execution_verified': False,
        }, indent=2), encoding='utf-8')
        print('Codex CLI 已完成本地源注册与插件安装。请在新聊天中检查并启用此插件。')
        print('业务调用前请单独配置 SCENARIO_MCP_URL 与场景专属 SCENARIO_API_KEY；安装不证明业务验收。')
        return target


def main() -> int:
    parser = argparse.ArgumentParser(description='Install a published scenario plugin into Codex')
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
        print('安装未完成：下载、依赖或宿主安装失败；未执行任何业务调用。', file=sys.stderr)
    return 1
