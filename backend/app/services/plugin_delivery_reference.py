"""Validate the server-owned blueprint against its pinned package identity."""
from __future__ import annotations

from pydantic import ValidationError

from ..scenario_capability_blueprint_schemas import ScenarioCapabilityBlueprintOut
from .capability_contracts import canonical_json
from .plugin_coding_validation import safe_text


MAX_BLUEPRINT_REFERENCE_BYTES = 128 * 1024


def checked_blueprint(manifest: dict) -> dict:
    try:
        value = ScenarioCapabilityBlueprintOut.model_validate(manifest.get('scenario_blueprint'), strict=True)
    except ValidationError:
        raise ValueError('新插件缺少符合封闭契约的固定场景画像，不能构建') from None
    identity = manifest['deployment']
    if value.scenario.id != manifest['scenario']['id'] or any(
        getattr(value.deployment, key) != identity.get(key)
        for key in ('release_id', 'snapshot_id', 'definition_hash')
    ):
        raise ValueError('插件画像与固定场景发布身份不一致，不能构建')
    chosen = {(item['kind'], item['key']) for item in manifest['capabilities']}
    selected = {(item.kind, item.key) for item in value.coverage.selected}
    declared = {(item.kind, item.key) for item in value.capabilities if item.selected}
    available = {(item.kind, item.key) for item in value.coverage.available}
    projected_available = {(item.kind, item.key) for item in value.capabilities if item.available}
    remaining = {(item.kind, item.key) for item in value.coverage.unselected_available}
    if (selected != chosen or declared != chosen or not chosen.issubset(available)
            or available != projected_available or remaining != available - chosen
            or len(selected) != len(value.coverage.selected)
            or len(value.capabilities) != len({(item.kind, item.key) for item in value.capabilities})):
        raise ValueError('插件画像能力覆盖与选定范围不一致，不能构建')
    result = value.model_dump(mode='json')
    source = canonical_json(result)
    if len(source.encode('utf-8')) > MAX_BLUEPRINT_REFERENCE_BYTES:
        raise ValueError('固定场景画像超过交付参考预算，不能构建')
    safe_text(source)
    return result
