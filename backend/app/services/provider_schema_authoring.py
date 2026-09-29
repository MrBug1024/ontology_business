"""Resolve explicit authoring references to immutable trusted Provider schemas."""
from __future__ import annotations

import copy
from typing import Any

from . import provider_definition_service


def materialize_schemas(declaration: dict[str, Any]) -> dict[str, Any]:
    """Resolve only an explicit exact-version reference; never repair bad schemas."""
    result = copy.deepcopy(declaration)
    if 'schema_source' not in result:
        return result
    source = result.pop('schema_source')
    if source != 'provider_manifest' or result.get('runtime_kind') != 'provider':
        raise ValueError('schema_source 仅支持 Provider 的 provider_manifest 声明')
    config = result.get('runtime_config')
    if not isinstance(config, dict):
        raise ValueError('Provider 运行配置必须是对象')
    matches = [manifest for manifest in provider_definition_service.list_function_provider_manifests()
               if manifest['provider_key'] == config.get('provider_key')
               and manifest['provider_version'] == config.get('provider_version')]
    if len(matches) != 1:
        raise ValueError('找不到精确版本的受信 Provider Schema')
    manifest = matches[0]
    for field in ('input_schema', 'output_schema'):
        if manifest[field + '_mode'] != 'fixed':
            raise ValueError('provider_manifest 声明仅适用于固定输入输出 Schema')
        if field in result and result[field] != manifest[field]:
            raise ValueError(f'{field} 与指定版本的固定 Provider Schema 冲突')
        result[field] = copy.deepcopy(manifest[field])
    return result
