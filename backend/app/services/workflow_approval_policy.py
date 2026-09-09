"""Actor assignment and immutable evidence requirements for business decisions."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..channel_interaction_schemas import ApprovalAudience, EvidenceReference
from ..approval_models import WorkflowApprovalEvidence
from ..models import DataAssetVersion, DatasetVersion
from . import managed_attachment_access, permission_service
from .policies import PolicyViolation


def audience(config: Mapping[str, Any]) -> ApprovalAudience:
    try:
        values = {key: config[key] for key in ApprovalAudience.model_fields if key in config}
        for key in ("approver_user_ids", "approver_roles"):
            if isinstance(values.get(key), tuple):
                values[key] = list(values[key])
        return ApprovalAudience.model_validate(values)
    except ValidationError:
        raise PolicyViolation("审批人员、角色或证据要求配置无效") from None


def node_config(workflow: Any, node_id: str) -> Mapping[str, Any]:
    for index, node in enumerate(workflow.nodes or workflow.steps or []):
        candidate_id = str(node.get("id") or node.get("node_id") or f"step-{node.get('step', index + 1)}")
        if candidate_id == node_id and node.get("type") == "approval":
            return node.get("data") or node
    raise PolicyViolation("固定审批节点不存在")


def require_audience(db: Session, config: Mapping[str, Any]) -> ApprovalAudience:
    principal = permission_service.require_principal(db)
    policy = audience(config)
    if policy.approver_user_ids and principal.user_id not in policy.approver_user_ids:
        raise PolicyViolation("当前人员不在本次审批的指定人员中")
    if policy.approver_roles and principal.role_key not in policy.approver_roles:
        raise PolicyViolation("当前人员不具备本次审批要求的角色")
    return policy


def prepare_evidence(
    db: Session, config: Mapping[str, Any], evidence: Sequence[Mapping[str, Any]], *, approved: bool,
) -> list[dict[str, Any]]:
    principal = permission_service.require_principal(db)
    policy = require_audience(db, config)
    if len(evidence) > 20:
        raise PolicyViolation("一次审批最多附带 20 项证据")
    try:
        references = [EvidenceReference.model_validate(item) for item in evidence]
        managed_attachment_access.validate_attachments(db, references, user_id=principal.user_id)
    except (ValidationError, managed_attachment_access.AttachmentAccessError):
        raise PolicyViolation("审批证据不存在、已失效或无权访问，请重新上传") from None
    if approved and policy.requires_evidence and not references:
        raise PolicyViolation("本次审批需要佐证文件，请附上文件后再回复同意")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in references:
        reference_id = item.asset_version_id or item.dataset_version_id
        if reference_id in seen:
            raise PolicyViolation("审批证据不能重复")
        seen.add(reference_id)
        version = db.get(DataAssetVersion if item.asset_version_id else DatasetVersion, reference_id)
        signature = version.content_sha256 if item.asset_version_id else version.content_hash
        result.append({**item.model_dump(exclude_none=True), "expected_signature": signature})
    return result


def may_reply(db: Session, workflow: Any, node_id: str) -> bool:
    try:
        require_audience(db, node_config(workflow, node_id))
        return True
    except PolicyViolation:
        return False


def retain_evidence(db: Session, approval: Any, evidence: Sequence[Mapping[str, Any]]) -> None:
    principal = permission_service.require_principal(db)
    for reference in evidence:
        asset_id = reference.get("asset_version_id")
        version = db.get(DataAssetVersion, asset_id) if asset_id else None
        db.add(WorkflowApprovalEvidence(
            approval_id=approval.id, scenario_id=approval.scenario_id, tenant_id=principal.tenant_id,
            asset_version_id=asset_id, dataset_version_id=reference.get("dataset_version_id"),
            bucket_file_id=version.bucket_file_id if version else None,
            content_signature=reference["expected_signature"],
        ))
    db.flush()
