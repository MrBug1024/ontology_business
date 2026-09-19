"""Persistent scenario boundary for external invocation attachments."""
from __future__ import annotations

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..external_api_models import ExternalScenarioAsset
from ..models import DataAsset, DataAssetVersion, DatasetVersionAsset


def bind_created_asset(db: Session, asset: DataAsset) -> None:
    scenario_id = db.info.get("external_scenario_id")
    if scenario_id:
        db.add(ExternalScenarioAsset(asset_id=asset.id, tenant_id=asset.tenant_id,
                                     scenario_id=scenario_id))
        db.flush()


def can_use_asset(db: Session, asset: DataAsset, *, scenario_id: str | None = None, purpose: str = "") -> bool:
    bound = db.info.get("external_scenario_id")
    if bound is not None and scenario_id is not None and scenario_id != bound:
        return False
    expected = scenario_id or bound
    ownership = db.get(ExternalScenarioAsset, asset.id)
    if ownership is not None:
        return bool(expected and ownership.scenario_id == expected and ownership.tenant_id == asset.tenant_id)
    if db.info.get("external_scenario_id") is not None:
        # Old tenant/global temporary uploads have no provable scenario owner.
        # Governed shared assets retain their existing contract/ACL policy.
        return purpose != "invocation_attachment" and (asset.labels or {}).get("catalog_purpose") != "invocation_attachment"
    return True


def can_use_dataset(db: Session, version_id: str, *, scenario_id: str | None = None) -> bool:
    """A dataset/head alias cannot erase its uploaded assets' ownership."""
    bound = db.info.get("external_scenario_id")
    if bound is not None and scenario_id is not None and scenario_id != bound:
        return False
    expected = scenario_id or bound
    forbidden = ExternalScenarioAsset.scenario_id != expected if expected else ExternalScenarioAsset.asset_id.is_not(None)
    if bound is not None:
        forbidden = or_(forbidden, and_(ExternalScenarioAsset.asset_id.is_(None), or_(
            DataAsset.labels["catalog_purpose"].as_string() == "invocation_attachment",
            DataAssetVersion.version_document["lifecycle"]["purpose"].as_string() == "invocation_attachment")))
    return db.scalar(select(DataAsset.id).select_from(DatasetVersionAsset)
        .join(DataAssetVersion, DataAssetVersion.id == DatasetVersionAsset.asset_version_id)
        .join(DataAsset, DataAsset.id == DataAssetVersion.asset_id)
        .outerjoin(ExternalScenarioAsset, ExternalScenarioAsset.asset_id == DataAsset.id)
        .where(DatasetVersionAsset.dataset_version_id == version_id, forbidden).limit(1)) is None
