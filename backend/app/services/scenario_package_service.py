"""Authorize and assemble an external plugin from an enabled immutable release."""
from __future__ import annotations

from copy import deepcopy

from fastapi import HTTPException
from sqlalchemy import select

from ..models import CapabilityInvocation, OntologyRelease, WorkflowRun
from . import capability_access_service, permission_service, release_service
from .scenario_package_acceptance import PackageValidationError, validate_acceptance
from .plugin_host_artifact import build_artifact
from .plugin_host_profile import package_name


def require_finished_workflows(receipts: dict, runs: dict, request) -> None:
    for case in request.acceptance_cases:
        receipt = receipts.get(case.invocation_id)
        if case.kind != 'workflow' or receipt is None or receipt.status != 'succeeded':
            continue
        output = (receipt.result_document or {}).get('output', {})
        run = runs.get(output.get('workflow_run_id')) if isinstance(output, dict) else None
        if (run is None or run.release_id != receipt.release_id or run.definition_hash != receipt.definition_hash
                or run.scenario_id != receipt.scenario_id or run.workflow_id != receipt.capability_key):
            raise PackageValidationError('工作流回执缺少属于本发布的实际执行结果')
        # Capability receipt success proves enqueueing, not business completion.
        if run.status not in {'succeeded', 'failed', 'rejected'}:
            raise PackageValidationError('工作流仍在执行或等待审批，不能作为完成验收证据')
        if case.role == 'success' and run.status != 'succeeded':
            raise PackageValidationError('成功案例对应的工作流没有实际执行成功')


def prepare_package(db, release_id: str, request) -> tuple[str, dict]:
    principal = permission_service.require_principal(db)
    release = db.scalar(select(OntologyRelease).where(
        OntologyRelease.id == release_id, OntologyRelease.tenant_id == principal.tenant_id,
        OntologyRelease.deleted_at.is_(None),
    ))
    if release is None:
        raise HTTPException(404, '发布不可用')
    scenario, _ = release_service._scenario_for_manage(db, release.scenario_id)
    if release.revision != request.expected_revision:
        raise PackageValidationError('发布状态已变更，请刷新后构建')
    if release.status != 'released' or not release.enabled:
        raise PackageValidationError('请先人工启用精确发布版本')
    try:
        access = capability_access_service.build_manifest(db, scenario.id, release_id=release.id)
    except capability_access_service.CapabilityAccessError as exc:
        raise PackageValidationError(str(exc)) from None
    ids = [case.invocation_id for case in request.acceptance_cases]
    rows = db.scalars(select(CapabilityInvocation).where(
        CapabilityInvocation.id.in_(ids), CapabilityInvocation.tenant_id == principal.tenant_id,
        CapabilityInvocation.scenario_id == scenario.id,
        CapabilityInvocation.requested_by_user_id == principal.user_id,
    )).all()
    receipts = {row.id: row for row in rows}
    capabilities = validate_acceptance(access['capabilities'], receipts, request,
        release_id=release.id, definition_hash=access['deployment']['definition_hash'])
    run_ids = {row.result_document.get('output', {}).get('workflow_run_id') for row in rows
        if row.capability_kind == 'workflow' and isinstance(row.result_document, dict)
        and isinstance(row.result_document.get('output'), dict)} - {None}
    runs = db.scalars(select(WorkflowRun).where(
        WorkflowRun.id.in_(run_ids), WorkflowRun.scenario_id == scenario.id,
        WorkflowRun.release_id == release.id,
    )).all() if run_ids else []
    require_finished_workflows(receipts, {run.id: run for run in runs}, request)
    name = package_name(release.id, request.target)
    manifest = {
        'package_version': 'scenario-plugin.v1', 'adapter_version': '1.0.0', 'package_name': name,
        'host': request.target, 'scenario': access['scenario'], 'deployment': access['deployment'],
        'capabilities': capabilities,
        'acceptance': {'kind': 'human_attested_server_receipts', 'reviewer_id': principal.user_id,
            'cases': [case.model_dump() for case in request.acceptance_cases]},
        'runtime': {'execution': 'platform', 'protocol': 'mcp', 'credentials': 'external_environment'},
    }
    # Refresh the mutable release state after assembling. The immutable content
    # stays pinned; a concurrent disable or revision change rejects this build.
    db.refresh(release)
    if release.revision != request.expected_revision or not release.enabled or release.status != 'released':
        raise PackageValidationError('发布在构建期间发生变化，请刷新后重试')
    return name, manifest


def build_package(db, release_id: str, request) -> tuple[str, bytes]:
    name, manifest = prepare_package(db, release_id, request)
    from .plugin_coding_contract import authoring_contract
    try:
        contract = authoring_contract(db, manifest)
        for field in ('scenario_blueprint', 'delivery_profile', 'scenario', 'client_contract'):
            manifest[field] = deepcopy(contract[field])
        artifact = build_artifact(manifest)
    except ValueError:
        raise PackageValidationError('固定发布画像或插件结构校验未通过，请刷新能力定义后重试') from None
    check_release_state(db, release_id, request.expected_revision)
    return name, artifact


def check_release_state(db, release_id: str, expected_revision: int) -> None:
    release = db.get(OntologyRelease, release_id)
    if release is None:
        raise PackageValidationError('发布不可用')
    db.refresh(release)
    if release.revision != expected_revision or not release.enabled or release.status != 'released':
        raise PackageValidationError('发布在构建期间发生变化，请刷新后重试')
