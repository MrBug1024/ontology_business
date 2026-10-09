"""Pin trusted build semantics and reserve immutable package versions."""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from .capability_contracts import canonical_hash
from .scenario_package_artifact import TEMPLATE_ROOT
from .plugin_delivery_profile import require_delivery_profile
from .plugin_host_profile import CODEX_PROFILE_VERSION, require_manifest_host


def adapter_hash(profile: dict | None = None) -> str:
    if isinstance(profile, dict) and profile.get('version') == CODEX_PROFILE_VERSION:
        from .plugin_host_profile import require_delivery_profile as require_host_profile
        require_host_profile(profile)
        values = {'server': (TEMPLATE_ROOT / 'server.py').read_text(encoding='utf-8')}
        for name in ('plugin_host_artifact.py', 'plugin_host_profile.py', 'plugin_delivery_reference.py'):
            values[name] = Path(__file__).with_name(name).read_text(encoding='utf-8')
        return canonical_hash(values, domain='scenario-plugin-trusted-adapter-codex-v2')
    values = {
        'server': (TEMPLATE_ROOT / 'server.py').read_text(encoding='utf-8'),
        'builder': Path(__file__).with_name('scenario_package_artifact.py').read_text(encoding='utf-8'),
    }
    if require_delivery_profile(profile) is not None:
        values.update(
            delivery_builder=Path(__file__).with_name('plugin_delivery_artifact.py').read_text(encoding='utf-8'),
            delivery_profile=Path(__file__).with_name('plugin_delivery_profile.py').read_text(encoding='utf-8'),
            delivery_reference=Path(__file__).with_name('plugin_delivery_reference.py').read_text(encoding='utf-8'),
        )
    return canonical_hash(values, domain='scenario-plugin-trusted-adapter-v1')


def assert_adapter_identity(document: dict) -> None:
    manifest = document.get('manifest', {})
    try:
        if not isinstance(manifest, dict) or 'delivery_profile' in manifest and manifest['delivery_profile'] is None:
            raise ValueError('插件规范身份缺失')
        require_manifest_host(manifest)
        expected = adapter_hash(manifest.get('delivery_profile'))
    except ValueError:
        raise HTTPException(409, '插件交付规范不可用，请创建受支持版本；不会替换原交付文件') from None
    if document.get('adapter_hash') != expected:
        raise HTTPException(409, '受信适配器已更新，请创建新版本工作台并重新审阅，不会替换原交付文件')


def snapshot_id(tenant_id: str, name: str, version: str) -> str:
    return canonical_hash({'tenant': tenant_id, 'name': name, 'version': version},
                          domain='scenario-plugin-version-v1')[:32]


def assert_same_version(existing, snapshot: dict) -> None:
    if existing and (existing.proposal or {}).get('artifact_hash') != snapshot['artifact_hash']:
        raise HTTPException(409, '该插件版本已交付其他内容，请设置新版本并重新审阅')
