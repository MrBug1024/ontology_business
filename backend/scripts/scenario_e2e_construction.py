"""Authorized real-model application harness for the three synthetic scenarios.

This is not an HTTP/authentication test. The account and scene are supplied from
the root task's authenticated browser, then revalidated by the ordinary services.
No model, permission, worker, definition, receipt, or publication is mocked.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import time
from typing import Any, Iterator
from uuid import uuid4

# Application routes persist bounded public diagnostics. Provider tracebacks
# are not suitable for this user-visible harness console.
logging.disable(logging.CRITICAL)

from fastapi import Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import BusinessScenario, OntologyRule, OntologyWorkflow, User
from app.routers import assistant, plugin_coding, plugin_publications, scenario_releases, scenarios
from app.schemas import (
    AssistantChatRequest, AssistantModelTaskContinuationRequest,
    AssistantProposalApplyRequest, ScenarioModelCandidateBatchPromotionRequest,
    RuleIn, WorkflowIn,
)
from app.scenario_release_schemas import ScenarioReleaseChange, ScenarioReleaseCreate
from app.plugin_coding_schemas import PluginCodingDraftCreate, PluginCodingReview, PluginCodingUpdate
from app.plugin_publication_schemas import PluginPublicationChange
from app.scenario_package_schemas import ScenarioPackageBuild
from app.services import capability_application_service, permission_service
from app.services.capability_contracts import Actor, CapabilityRef, Request


class HarnessBlocked(RuntimeError):
    """A safe, observable application gate halted the real experiment."""


def plain(value: Any) -> Any:
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='json')
    return value


class ScenarioHarness:
    def __init__(self, scene_id: str, username: str, report_path: str | Path, spec: dict):
        self.scene_id = scene_id
        self.username = username
        self.report_path = Path(report_path)
        self.spec = spec
        if self.report_path.exists():
            self.report = json.loads(self.report_path.read_text(encoding='utf-8'))
            if self.report.get('scene_id') != scene_id or self.report.get('username') != username:
                raise HarnessBlocked('报告身份与明确授权的场景/账户不一致')
        else:
            self.report = {'version': 1, 'scene_id': scene_id, 'username': username,
                           'spec': spec, 'stages': [], 'business_cases': [], 'receipts': []}
        with SessionLocal() as db:
            users = db.scalars(select(User).where(or_(User.display_name == username, User.email == username))).all()
            if len(users) != 1:
                raise HarnessBlocked('明确账户必须唯一解析，不按最近项目或管理员身份推测')
            scene = db.get(BusinessScenario, scene_id)
            if scene is None or not scene.tenant_id:
                raise HarnessBlocked('明确创建的场景不存在或缺少工作区身份')
            self.user_id, self.tenant_id = users[0].id, scene.tenant_id
            db.info.update(user_id=self.user_id, tenant_id=self.tenant_id)
            principal = permission_service.require_principal(db)
            permission_service.require_scenario_permission(db, scene, 'manage')
            self.actor = Actor(actor_type='user', principal_id=principal.user_id,
                               tenant_id=principal.tenant_id, user_id=principal.user_id,
                               roles=(principal.role_key,), scopes=('capability:read', 'capability:invoke'))
            resources = plain(plugin_coding.resource_catalog(scenario_id=scene_id, db=db))
        tools_models = [item for item in resources['models'] if item['supports_tools']]
        if not tools_models:
            raise HarnessBlocked('真实语义规划与编码需要当前工作区可用的工具模型')
        selected_id = self.report.get('model', {}).get('id')
        if selected_id and not any(item['id'] == selected_id for item in tools_models):
            raise HarnessBlocked('已记录的真实模型不再可用，不静默换模型')
        self.model = next((item for item in tools_models if item['id'] == selected_id), tools_models[0])
        self.skill_ids = [item['id'] for item in resources['skills']]
        self.path = f'/scenarios/{scene_id}'
        self.release = self.report.get('release')
        self.checkpoint('actor_verified', model=self.model, actor={'user_id': self.user_id,
                        'tenant_id': self.tenant_id, 'role': self.actor.roles[0]})

    @contextmanager
    def actor_db(self) -> Iterator[Session]:
        with SessionLocal() as db:
            db.info.update(user_id=self.user_id, tenant_id=self.tenant_id)
            permission_service.require_principal(db)
            scene = db.get(BusinessScenario, self.scene_id)
            if scene is None:
                raise HarnessBlocked('授权场景不再可用')
            permission_service.require_scenario_permission(db, scene, 'manage')
            yield db

    def checkpoint(self, stage: str, **values: Any) -> None:
        self.report.update(values)
        self.report['stages'].append({'stage': stage, 'at': datetime.now(timezone.utc).isoformat()})
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(json.dumps(self.report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        print(json.dumps({'scene_id': self.scene_id, 'stage': stage}, ensure_ascii=True), flush=True)

    def candidates(self, proposal_id: str | None = None) -> list[dict]:
        with self.actor_db() as db:
            result = scenarios.list_scenario_model_drafts(
                self.scene_id, proposal_id=proposal_id, resource_kind=None, draft_status=None,
                include_resolved=True, include_issues=True, offset=0, limit=1000, db=db)
            return plain(result)['items']

    def latest_proposal(self) -> dict:
        thread_id = self.report.get('thread_id')
        if not thread_id:
            return {}
        with self.actor_db() as db:
            messages = assistant.list_thread_messages(thread_id, scenario_id=self.scene_id,
                        page='业务场景', path=self.path, db=db)
            proposals = [message.proposal for message in messages if message.role == 'assistant'
                         and isinstance(message.proposal, dict) and message.proposal.get('kind') == 'scenario_model']
            return proposals[-1] if proposals else {}

    def wait_compilation(self, job_id: str, *, timeout: int = 1200) -> dict:
        deadline = time.monotonic() + timeout
        prior = ''
        while time.monotonic() < deadline:
            with self.actor_db() as db:
                status = plain(assistant.get_compilation_job(job_id, response=Response(), db=db))
                if status['status'] == 'succeeded':
                    result = plain(assistant.get_compilation_job_result(job_id, response=Response(), db=db))
                    self.checkpoint('compilation_succeeded', last_compilation=status, proposal=result['proposal'])
                    return result['proposal']
                if status['status'] == 'failed':
                    self.checkpoint('compilation_failed', last_compilation=status)
                    raise HarnessBlocked(status['error_message'] or '真实编译任务失败，未绕过门禁')
            current = json.dumps(status['progress'], ensure_ascii=True, sort_keys=True)
            if current != prior:
                self.checkpoint('compilation_progress', last_compilation=status)
                prior = current
            time.sleep(3)
        raise HarnessBlocked('真实编译尚未终态，保持同一任务等待而不重复调用')

    def _govern_task(self, proposal: dict, task_id: str) -> dict:
        open_rows = [item for item in self.candidates(proposal['proposal_id'])
                     if item['task_id'] == task_id and item['draft_status'] not in {'resolved', 'superseded', 'applied'}]
        if not open_rows:
            with self.actor_db() as db:
                applied = assistant.apply_proposal(AssistantProposalApplyRequest(
                    kind='scenario_model', scenario_id=self.scene_id, thread_id=self.report['thread_id'],
                    proposal_id=proposal['proposal_id'], task_id=task_id, confirm=True), db=db)
            self.checkpoint('task_confirmed_without_open_candidates', last_task_result=plain(applied))
            return self.latest_proposal()
        request = ScenarioModelCandidateBatchPromotionRequest(items=[
            {'draft_id': row['id'], 'expected_revision': row['revision']} for row in open_rows])
        with self.actor_db() as db:
            result = plain(scenarios.revalidate_scenario_model_candidates_batch(self.scene_id, request, db=db))
        refreshed = [item for item in self.candidates(proposal['proposal_id']) if item['id'] in {row['id'] for row in open_rows}]
        self.checkpoint('candidate_revalidated', candidates=self.candidates(), last_revalidation=result)
        if result['blocked_count']:
            raise HarnessBlocked('实际候选仍有质量/依赖阻塞；保持候选，不替换为手写正式定义')
        request = ScenarioModelCandidateBatchPromotionRequest(items=[
            {'draft_id': row['id'], 'expected_revision': row['revision']} for row in refreshed])
        with self.actor_db() as db:
            result = plain(scenarios.promote_scenario_model_candidates_batch(self.scene_id, request, db=db))
        self.checkpoint('candidate_promoted', candidates=self.candidates(), last_promotion=result)
        return self.latest_proposal()

    def resolve_model(self, rationale: str) -> dict:
        """Explicitly replan a saved proposal through the ordinary CAS endpoint."""
        proposal = self.latest_proposal()
        if not proposal:
            raise HarnessBlocked('没有可澄清的持久场景候选')
        revision = proposal.get('run_revision') or (proposal.get('payload') or {}).get('execution_revision')
        if not isinstance(revision, int) or revision < 1:
            raise HarnessBlocked('持久草稿缺少服务端revision，不能猜版本')
        resolution = {'proposal_id': proposal['proposal_id'], 'expected_revision': revision,
                      'action': 'replan', 'rationale': rationale}
        self.checkpoint('construction_resolution_started', construction_resolution=resolution)
        with self.actor_db() as db:
            result = plain(assistant.chat(AssistantChatRequest(
                message='请根据以下明确补充重新生成当前场景候选并继续原有建设范围：' + rationale,
                request_id=uuid4().hex, scenario_id=self.scene_id, thread_id=self.report['thread_id'],
                llm_config_id=self.model['id'], page='业务场景', path=self.path,
                mode='draft', draft_kind='scenario_model', construction_resolution=resolution), db=db))
        self.checkpoint('construction_resolution_reply', proposal=result['proposal'],
                        construction_reply=result['reply'], construction_questions=result['questions'])
        if result['proposal']:
            return result['proposal']
        with self.actor_db() as db:
            jobs = assistant.list_thread_compilation_jobs(self.report['thread_id'], Response(),
                      scenario_id=self.scene_id, page='业务场景', path=self.path, db=db)
        if jobs and plain(jobs[0])['status'] == 'running':
            return self.wait_compilation(plain(jobs[0])['id'])
        raise HarnessBlocked('实际重新规划没有形成候选，保持原记录待恢复')

    def build_model(self, prompt: str) -> dict:
        proposal = self.latest_proposal()
        if not proposal:
            request_id = self.report.get('construction_request_id') or uuid4().hex
            self.checkpoint('construction_started', construction_request_id=request_id, construction_prompt=prompt)
            with self.actor_db() as db:
                result = plain(assistant.chat(AssistantChatRequest(
                    message=prompt, request_id=request_id, scenario_id=self.scene_id,
                    thread_id=self.report.get('thread_id'), llm_config_id=self.model['id'],
                    page='业务场景', path=self.path, mode='draft', draft_kind='scenario_model'), db=db))
            self.checkpoint('construction_reply', thread_id=result['thread_id'], proposal=result['proposal'],
                            construction_reply=result['reply'], construction_questions=result['questions'])
            proposal = result['proposal']
            if not proposal:
                with self.actor_db() as db:
                    jobs = assistant.list_thread_compilation_jobs(result['thread_id'], response=Response(), scenario_id=self.scene_id,
                                  page='业务场景', path=self.path, db=db)
                if jobs:
                    proposal = self.wait_compilation(plain(jobs[0])['id'])
                else:
                    raise HarnessBlocked('真实语义规划未产生场景候选；查看保留的问题后修正本次输入')
        for _ in range(20):
            self.checkpoint('model_task_state', proposal=proposal, candidates=self.candidates())
            payload = proposal.get('payload') or {}
            next_action = payload.get('next_action') or {}
            action = next_action.get('type')
            if action == 'confirm_task':
                proposal = self._govern_task(proposal, next_action['task_id'])
            elif action == 'generate_task':
                with self.actor_db() as db:
                    job = plain(assistant.continue_model_task(AssistantModelTaskContinuationRequest(
                        scenario_id=self.scene_id, thread_id=self.report['thread_id'],
                        proposal_id=proposal['proposal_id'], task_id=next_action['task_id']), db=db))
                self.checkpoint('next_task_started', last_compilation=job)
                proposal = self.wait_compilation(job['id'])
            elif payload.get('execution_status') in {'completed', 'completed_no_changes'}:
                self.checkpoint('model_completed', proposal=proposal, candidates=self.candidates())
                return proposal
            else:
                self.checkpoint('model_blocked', proposal=proposal, candidates=self.candidates())
                raise HarnessBlocked('建模计划需要澄清或重新规划，未静默跳过阻塞或扩大范围')
        raise HarnessBlocked('建模阶段超过有界推进次数')

    def enable_capabilities(self, capabilities: list[dict], *, rule_severity_overrides: dict[str, str] | None = None) -> None:
        """Explicitly activate exact new definitions before their first snapshot."""
        if self.release:
            raise HarnessBlocked('已有不可变发布后不修改当前定义来修补旧发布')
        activated = []
        for capability in capabilities:
            kind, key = capability['kind'], capability['key']
            if kind not in {'rule', 'workflow'}:
                raise HarnessBlocked('仅规则/工作流有本批显式激活步骤')
            with self.actor_db() as db:
                model = OntologyRule if kind == 'rule' else OntologyWorkflow
                row = db.get(model, key)
                if row is None or row.scenario_id != self.scene_id:
                    raise HarnessBlocked('精确待启用定义不属于当前授权场景')
                schema = RuleIn if kind == 'rule' else WorkflowIn
                values = {field: getattr(row, field) for field in schema.model_fields}
                previous = {'enabled': row.enabled}
                values['enabled'] = True
                if kind == 'rule' and key in (rule_severity_overrides or {}):
                    from app.services.scenario_model_compiler import _normalize_rule_severity
                    replacement = rule_severity_overrides[key]
                    if replacement != _normalize_rule_severity(row.severity):
                        raise HarnessBlocked('严重级别修正必须符合既有确定性别名映射')
                    previous['severity'] = row.severity
                    values['severity'] = replacement
                if kind == 'workflow':
                    previous['status'] = row.status
                    values['status'] = 'active'
                payload = schema(**values)
                if kind == 'rule':
                    result = plain(scenarios.update_rule(key, payload, db=db))
                else:
                    result = plain(scenarios.update_workflow(key, payload, db=db))
            activated.append({**capability, 'before': previous, 'enabled': result['enabled'],
                              'status': result.get('status'), 'severity': result.get('severity'), 'actor': self.user_id})
        self.checkpoint('definitions_explicitly_activated', definition_activation=activated,
                        activation_authorization='本批用户明确授权发布和验收，仅精确新定义，捕获正式快照前')

    def acknowledge_governed_model(self, *, review_note: str) -> dict:
        """Keep the assistant's stale gaps visible after human candidate review."""
        if not review_note.strip():
            raise HarnessBlocked('需要明确审阅说明，不自动忽略模型建设问题')
        proposal = self.latest_proposal()
        payload = proposal.get('payload') or {}
        rows = self.candidates(proposal['proposal_id']) if proposal else []
        if not rows or any(row['draft_status'] not in {'resolved', 'superseded', 'applied'} for row in rows):
            raise HarnessBlocked('当前候选闭包仍有未治理资源')
        tasks = payload.get('tasks') or []
        if not tasks or any(task['status'] != 'applied' for task in tasks):
            raise HarnessBlocked('当前建设仍有未完成任务，不能以人工审阅覆盖')
        self.checkpoint('human_governance_acknowledged', proposal=proposal,
                        human_governance_acknowledgement={'actor': self.user_id, 'note': review_note,
                            'assistant_execution_status': payload.get('execution_status'),
                            'preserved_issue_codes': [issue.get('code') for issue in payload.get('unresolved', [])],
                            'selected_proposal_id': proposal['proposal_id']})
        return proposal

    def govern_and_release(self) -> dict:
        if self.release:
            with self.actor_db() as db:
                current = plain(scenario_releases.get_release(self.release['id'], db=db))
                if not current['enabled'] and not self.release['enabled'] and current['revision'] == self.release['revision']:
                    current = plain(scenario_releases.change_release(current['id'], ScenarioReleaseChange(
                        action='enable', expected_revision=current['revision']), db=db))
                self.release = current
            if not self.release['enabled']:
                raise HarnessBlocked('已记录发布当前未启用，不自动重启或换版本')
        else:
            proposal = self.latest_proposal()
            if not proposal:
                raise HarnessBlocked('缺少真实候选来源，不能创建正式发布')
            selected = self.candidates(proposal['proposal_id'])
            remaining = [row for row in selected if row['draft_status'] not in {'resolved', 'superseded', 'applied'}]
            if remaining:
                raise HarnessBlocked('当前明确建设范围仍有未处理候选，不能将部分建设冒称完整发布')
            unselected = [row for row in self.candidates() if row['proposal_id'] != proposal['proposal_id']
                          and row['draft_status'] not in {'resolved', 'superseded', 'applied'}]
            self.checkpoint('release_candidate_scope_verified', selected_proposal_id=proposal['proposal_id'],
                            unselected_inert_candidates=[{'id': row['id'], 'resource_kind': row['resource_kind'],
                                'resource_key': row['resource_key'], 'draft_status': row['draft_status']} for row in unselected])
            with self.actor_db() as db:
                release = plain(scenario_releases.create_release(ScenarioReleaseCreate(
                    scenario_id=self.scene_id, name='真实端到端验收 1.0.0',
                    notes='本批用户授权的合成场景；实际AI候选治理后人工创建不可变发布', confirmed=True), db=db))
            self.checkpoint('release_created', release=release, release_id=release['id'])
            with self.actor_db() as db:
                self.release = plain(scenario_releases.change_release(release['id'], ScenarioReleaseChange(
                    action='enable', expected_revision=release['revision']), db=db))
        with self.actor_db() as db:
            context = plain(plugin_coding.context(self.release['id'], db=db))
        self.checkpoint('release_enabled', release=self.release, release_id=self.release['id'],
                        definition_hash=context['deployment']['definition_hash'], capability_catalog=context['capabilities'])
        return self.release

    def invoke(self, kind: str, key: str, inputs: dict, idempotency_key: str | None = None) -> dict:
        if not self.release:
            raise HarnessBlocked('需要精确的已启用发布')
        with self.actor_db() as db:
            scene = db.get(BusinessScenario, self.scene_id)
            receipt = capability_application_service.invoke(db, scene, self.actor,
                Request(capability=CapabilityRef(kind=kind, resource_id=key), inputs=inputs,
                        mode='execute', idempotency_key=idempotency_key or uuid4().hex,
                        correlation_id=f'synthetic-e2e:{uuid4().hex}',
                        expected_definition_hash=self.report['definition_hash']),
                release_id=self.release['id'], invocation_source='internal')
            db.commit()
            result = capability_application_service.receipt_document(receipt)
        self.report['receipts'].append(result)
        self.checkpoint('capability_invoked')
        return result

    def start_coding(self, capabilities: list[dict], instruction: str) -> dict:
        workspace_id = self.report.get('workspace_id')
        if not workspace_id:
            request_id = self.report.get('coding_request_id') or uuid4().hex
            self.checkpoint('coding_started', coding_request_id=request_id, capabilities=capabilities,
                            coding_instruction=instruction)
            with self.actor_db() as db:
                workspace = plain(plugin_coding.draft(PluginCodingDraftCreate(
                    expected_revision=self.release['revision'], capabilities=capabilities, request_id=request_id,
                    llm_config_id=self.model['id'], skill_ids=self.skill_ids, instruction=instruction,
                    plugin_version='1.0.0'), release_id=self.release['id'], db=db))
            workspace_id = workspace['id']
            self.checkpoint('coding_workspace_created', workspace_id=workspace_id)
        deadline = time.monotonic() + 1800
        previous = ''
        while time.monotonic() < deadline:
            with self.actor_db() as db:
                workspace = plain(plugin_coding.get(workspace_id, db=db))
            marker = f"{workspace['revision']}:{workspace['run_status']}:{workspace['phase']}"
            if marker != previous:
                self.checkpoint('coding_progress', workspace=workspace,
                                source_files={item['path']: item['content'] for item in workspace['files']})
                previous = marker
            if workspace['run_status'] in {'failed', 'cancelled'} or workspace['phase'] == 'validation_failed':
                raise HarnessBlocked('实际编码轮次未完成，保留源码与诊断供显式修正')
            human_edit = self.report.get('human_source_edit') or {}
            if (workspace['phase'] == 'draft' and workspace['active_run_id'] is None and not workspace['validation']
                    and human_edit.get('actor') == self.user_id
                    and human_edit.get('files_hash') == workspace['files_hash']
                    and isinstance(human_edit.get('note'), str) and human_edit['note'].strip()
                    and human_edit.get('original_files') and human_edit.get('diff')):
                self.checkpoint('coding_human_source_ready_for_review', workspace=workspace,
                                source_files={item['path']: item['content'] for item in workspace['files']})
                return workspace
            if workspace['phase'] in {'ready_for_review', 'released'} and workspace['run_status'] not in {'queued', 'running', 'waiting_upload'}:
                self.checkpoint('coding_ready_for_review', workspace=workspace,
                                source_files={item['path']: item['content'] for item in workspace['files']})
                return workspace
            time.sleep(3)
        raise HarnessBlocked('实际编码尚未终态，不自动重试或声称完成')

    def revise_coding(self, instruction: str) -> dict:
        workspace_id = self.report.get('workspace_id')
        if not workspace_id:
            raise HarnessBlocked('没有可继续的真实编码项目')
        with self.actor_db() as db:
            workspace = plain(plugin_coding.get(workspace_id, db=db))
            revised = plain(plugin_coding.revise(PluginCodingUpdate(
                expected_revision=workspace['revision'], base_files_hash=workspace['files_hash'],
                request_id=uuid4().hex, action='generate', instruction=instruction),
                workspace_id=workspace_id, db=db))
        self.checkpoint('coding_explicit_revision', previous_run_id=workspace['active_run_id'],
                        new_run_id=revised['active_run_id'], coding_revision_instruction=instruction)
        return revised

    def code_review_publish(self, capabilities: list[dict], cases: list[dict], instruction: str,
                            *, review_note: str) -> dict:
        if not review_note.strip():
            raise HarnessBlocked('必须先读取实际源码并记录审阅结论')
        workspace = self.start_coding(capabilities, instruction)
        if not self.report.get('artifact_id'):
            self.checkpoint('source_reviewed', source_review={'files_hash': workspace['files_hash'], 'note': review_note},
                            acceptance_cases=cases, capabilities=capabilities)
            with self.actor_db() as db:
                artifact = plain(plugin_coding.review(PluginCodingReview(
                    expected_revision=workspace['revision'], files_hash=workspace['files_hash'], confirmed_code_review=True,
                    acceptance=ScenarioPackageBuild(expected_revision=self.release['revision'],
                        capabilities=capabilities, acceptance_cases=cases, confirmed_business_acceptance=True)),
                    workspace_id=workspace['id'], db=db))
            self.checkpoint('plugin_reviewed', artifact=artifact, artifact_id=artifact['id'], artifact_hash=artifact['artifact_hash'])
        else:
            with self.actor_db() as db:
                artifact = plain(plugin_coding.version(self.report['artifact_id'], db=db))
        with self.actor_db() as db:
            publication = plain(plugin_publications.state(artifact['id'], db=db))
            self.checkpoint('plugin_publication_state_verified', publication=publication,
                            publication_id=publication['publication_id'], installation=publication['installation'])
            if publication['status'] != 'published':
                publication = plain(plugin_publications.change(PluginPublicationChange(
                    artifact_hash=artifact['artifact_hash'], expected_revision=publication['revision'],
                    action='publish', confirmed_publication=True), artifact_id=artifact['id'], db=db))
        self.checkpoint('plugin_published', publication=publication, publication_id=publication['publication_id'],
                        installation=publication['installation'])
        if not publication['available']:
            raise HarnessBlocked(publication['unavailable_reason'])
        return {'workspace': workspace, 'artifact': artifact, 'publication': publication}
