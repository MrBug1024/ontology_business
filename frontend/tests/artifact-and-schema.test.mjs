import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { actionArtifactAttachment } from '../src/utils/artifactAttachments.ts'
import { actionConfirmationParams } from '../src/utils/actionConfirmation.ts'
import { groupScenarioModelIssues, scenarioModelIssueLabel } from '../src/utils/assistantProposalGroups.ts'
import { compilationRetryDraft, retryAttachmentsForMessage } from '../src/utils/assistantRetry.ts'
import {
  draftRefToken,
  normalizeScenarioModelDrafts,
  scenarioDraftBlockingIssueCount,
  scenarioDraftIssueCount,
  scenarioDraftStage,
} from '../src/utils/scenarioModelDrafts.ts'
import {
  clearCompilationJobBookmark,
  readCompilationJobBookmark,
  readPendingCompilationJobBookmark,
  saveCompilationJobBookmark,
  savePendingCompilationJobBookmark,
  selectCompilationJobForRecovery,
} from '../src/utils/assistantCompilationRecovery.ts'
import { buildRelationConstraints, relationConstraintForm } from '../src/utils/relationConstraints.ts'
import { safeInternalReturnPath } from '../src/utils/navigation.ts'
import { parseStandardMCPConfig } from '../src/utils/mcpConfig.ts'
import { dataSourceLocationLabel } from '../src/utils/dataSources.ts'
import {
  buildRelationMappingPayload,
  missingRelationMappingFields,
  relationMappingModeLabel,
  relationMappingPayloadFingerprint,
} from '../src/utils/relationMappings.ts'
import { cloneAgentCapabilityScope, emptyAgentCapabilityScope } from '../src/utils/agentCapabilities.ts'
import { buildSchemaFromFields, flattenSchemaFields } from '../src/utils/schemaBuilder.ts'
import {
  cleanTemplateExecutorConfig,
  isTemplateBucketInScope,
  isSupportedTemplateFilename,
  templatePathsToSchema,
  templateUnavailableReason,
} from '../src/utils/templates.ts'

function memoryStorage() {
  const values = new Map()
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  }
}

test('data source labels report the configured storage authority', () => {
  assert.equal(
    dataSourceLocationLabel({ type: 'file_bucket', config: { storage_backend: 'minio' } }),
    'MinIO',
  )
  assert.equal(
    dataSourceLocationLabel({ type: 'file_bucket', config: { storage_backend: 'LOCAL' } }),
    '托管存储',
  )
  assert.equal(
    dataSourceLocationLabel({ type: 'file_bucket', config: {} }),
    '托管存储',
  )
  assert.equal(
    dataSourceLocationLabel({ type: 'postgres', config: { host: 'db.internal' } }),
    'db.internal',
  )
})

test('action confirmation keeps original parameters when the preview is compacted', () => {
  const params = { report_name: 'annual-audit', payload: 'x'.repeat(9000) }
  const compactPlan = {
    action_id: 'action-a',
    parameters_omitted: true,
    parameters_sha256: 'digest-only',
  }

  assert.equal(actionConfirmationParams({ args: { params } }, compactPlan), params)
  assert.deepEqual(
    actionConfirmationParams(
      { arguments: JSON.stringify({ params: { account_id: 'AP001' } }) },
      compactPlan,
    ),
    { account_id: 'AP001' },
  )
  assert.deepEqual(
    actionConfirmationParams({}, { parameters: { account_id: 'legacy' } }),
    { account_id: 'legacy' },
  )
  assert.equal(actionConfirmationParams({}, compactPlan), null)

  const source = readFileSync(
    new URL('../src/views/AgentChat.vue', import.meta.url),
    'utf8',
  )
  assert.match(source, /const params = actionConfirmationParams\(toolCall, plan\)/)
  assert.doesNotMatch(source, /params:\s*plan\.parameters\s*\|\|\s*\{\}/)
})

test('global assistant shows safe references and keeps capability changes governed', () => {
  const source = readFileSync(
    new URL('../src/components/GlobalAssistant.vue', import.meta.url),
    'utf8',
  )
  assert.match(source, /reference\.display_name/)
  assert.match(source, /智能业务顾问/)
  assert.match(source, /自动判断下一步/)
  assert.doesNotMatch(source, /assistant-task-preset|本条消息/)
  assert.match(source, /拆解任务并受控建设模型/)
  assert.doesNotMatch(source, /return '场景已有定义'/)
})

test('global assistant delegates task routing to the semantic planner', () => {
  const source = readFileSync(
    new URL('../src/components/GlobalAssistant.vue', import.meta.url),
    'utf8',
  )
  const sendStart = source.indexOf('function send(text?: string)')
  const sendEnd = source.indexOf('\nasync function answerQuestion', sendStart)
  assert.notEqual(sendStart, -1)
  assert.notEqual(sendEnd, -1)
  const sendSource = source.slice(sendStart, sendEnd)

  assert.doesNotMatch(sendSource, /submittedPreset|requestRouteForPreset|taskPreset/)
  assert.match(sendSource, /mode: 'ask'/)
  assert.match(sendSource, /draft_kind: 'auto'/)
  assert.match(sendSource, /LangGraph\/LLM semantic planner/)

  assert.doesNotMatch(source, /watch\(\(\) => context\.value\.scenario_id/)
})

test('scenario modelling stays as a durable sequential plan until final summary', () => {
  const source = readFileSync(
    new URL('../src/components/GlobalAssistant.vue', import.meta.url),
    'utf8',
  )
  assert.match(source, /场景建模计划/)
  assert.match(source, /按依赖顺序执行；需要写入时会明确向你确认/)
  assert.match(source, /modelPlanSummary/)
  assert.match(source, /applyCurrentModelTask/)
  assert.match(source, /当前持续任务停在确认点/)
  assert.match(source, /直接在下方说明修正、新增或删除要求/)
  assert.match(source, /保留草稿并继续/)
  assert.match(source, /全部任务已推进，存在待补全项/)
  assert.match(source, /解决建议/)
  assert.match(source, /modelRunAwaitingConfirmation/)
  assert.match(source, /latestModelRunMessage/)
  assert.match(source, /isActiveModelRun\(message\)/)
  assert.match(source, /event\.data\.thread_id/)
  assert.match(source, /task_update_text/)
  assert.match(source, /completed_with_gaps/)
  assert.doesNotMatch(source, /if \(modelRunAwaitingConfirmation\.value\) \{\s*ElMessage\.info/)
  assert.doesNotMatch(source, /先跳过，保留问题/)
})

test('global assistant keeps work records in the conversation instead of stacking status cards', () => {
  const source = readFileSync(
    new URL('../src/components/GlobalAssistant.vue', import.meta.url),
    'utf8',
  )
  assert.match(source, /class="assistant-worklog"/)
  assert.match(source, /aria-label="助手工作过程"/)
  assert.match(source, /function shouldShowWorkLog/)
  assert.match(source, /function workTranscriptEntries/)
  assert.match(source, /function compilationNarrative/)
  assert.match(source, /class="worklog-live-feed"/)
  assert.match(source, /aria-label="实时工作播报"/)
  assert.match(source, /function recordCompilationLiveness/)
  assert.match(source, /\.slice\(-4\)/)
  assert.match(source, /本阶段已发起 1 次受控模型调用/)
  assert.match(source, /仅展示可验证、可回看的工作信息，不包含模型的隐藏推理/)
  assert.doesNotMatch(source, /class="worklog-toggle"/)
  assert.match(source, /'is-model-result': proposalOf\(message\)\?\.kind === 'scenario_model'/)
  assert.match(source, /modelTasks\(proposalOf\(message\)\)\.length && Boolean\(expandedProposal\[index\]\)/)
  assert.doesNotMatch(source, /class="compilation-plan-card"/)
  assert.doesNotMatch(source, /class="thinking-summary"/)
  assert.doesNotMatch(source, /class="assistant-trace"/)
})

test('scenario modelling uses one assistant event stream instead of browser status polling', () => {
  const source = readFileSync(
    new URL('../src/components/GlobalAssistant.vue', import.meta.url),
    'utf8',
  )
  const apiSource = readFileSync(
    new URL('../src/api/index.ts', import.meta.url),
    'utf8',
  )

  assert.match(apiSource, /function streamAssistantEvents/)
  assert.match(apiSource, /export function streamAssistantCompilationJob/)
  assert.match(source, /case 'compilation_progress'/)
  assert.match(source, /case 'compilation_result'/)
  assert.match(source, /case 'compilation_liveness'/)
  assert.match(source, /case 'tool_event'/)
  assert.match(source, /attachCompilationEventStream/)
  assert.doesNotMatch(source, /scheduleCompilationPoll|pollCompilationJob|compilationPollTimer/)
  assert.doesNotMatch(source, /window\.setTimeout|window\.setInterval/)
})

test('live modelling projects draft checkpoints and accepts guidance while minimized', () => {
  const source = readFileSync(
    new URL('../src/components/GlobalAssistant.vue', import.meta.url),
    'utf8',
  )
  const apiSource = readFileSync(
    new URL('../src/api/index.ts', import.meta.url),
    'utf8',
  )
  const scenarioSource = readFileSync(
    new URL('../src/views/ScenarioDetail.vue', import.meta.url),
    'utf8',
  )

  assert.match(apiSource, /submitAssistantCompilationGuidance/)
  assert.match(apiSource, /\/compilation-jobs\/\$\{jobId\}\/guidance/)
  assert.match(source, /case 'draft_checkpoint'/)
  assert.match(source, /function projectDraftCheckpoint/)
  assert.match(source, /assistant-scenario-drafts-updated/)
  assert.match(scenarioSource, /onAssistantScenarioDraftsUpdated[\s\S]*?loadScenarioDrafts/)
  assert.match(source, /v-if="showLauncher && !visible"/)
  assert.match(source, /'is-working': advisorWorking/)
  assert.match(source, /launcherStatus/)
  assert.match(source, /async function openAssistant\(\)[\s\S]*?if \(!threadId\.value\)[\s\S]*?await loadContext\(\)[\s\S]*?onCompilationVisibilityChange\(\)/)
  assert.match(source, /async function submitCompilationGuidance/)
  assert.match(source, /if \(content && compilationRunning\.value\)/)
  assert.match(source, /补充、纠正或删除要求/)
  assert.doesNotMatch(
    source,
    /:disabled="!canSend[^\n]*\|\| compilationBusy/,
  )
})

test('scenario model write success requires a persisted mutation ledger', () => {
  const source = readFileSync(
    new URL('../src/components/GlobalAssistant.vue', import.meta.url),
    'utf8',
  )
  const start = source.indexOf('function modelTaskHasPersistedWrites')
  const end = source.indexOf('\nfunction modelFullyAppliedTaskCount', start)
  assert.notEqual(start, -1)
  assert.notEqual(end, -1)
  const functionSource = source.slice(start, end)
  const body = functionSource.slice(
    functionSource.indexOf('{') + 1,
    functionSource.lastIndexOf('}'),
  )
  const hasPersistedWrites = Function(`return function (task) {${body}}`)()
  const result = (status, extra = {}) => ({
    id: 'ontology',
    status,
    apply_result: {
      kind: 'scenario_model',
      task_id: 'ontology',
      task_status: status,
      ...extra,
    },
  })

  assert.equal(hasPersistedWrites(result('applied', { applied_change_keys: ['entity.project'] })), true)
  assert.equal(hasPersistedWrites(result('partially_applied', { counts: { entities_added: 1 } })), true)
  assert.equal(hasPersistedWrites(result('applied')), false)
  assert.equal(hasPersistedWrites(result('applied', { applied_change_keys: [], counts: { properties_skipped: 4 } })), false)
  assert.equal(hasPersistedWrites(result('deferred', { applied_change_keys: ['entity.project'] })), false)
  assert.equal(hasPersistedWrites({
    ...result('applied', { applied_change_keys: ['entity.project'] }),
    apply_result: {
      kind: 'scenario_model', task_id: 'ontology', task_status: 'partially_applied', applied_change_keys: ['entity.project'],
    },
  }), false)
})

test('zero-write scenario model finals stay neutral and suppress false apply copy', () => {
  const source = readFileSync(
    new URL('../src/components/GlobalAssistant.vue', import.meta.url),
    'utf8',
  )
  const template = source.slice(0, source.indexOf('<script setup'))
  const finalHelpers = source.slice(
    source.indexOf('function modelRunStatusType'),
    source.indexOf('function modelNextAction'),
  )

  assert.match(template, /:content="assistantMessageContent\(message\)"/)
  assert.doesNotMatch(template, /全部任务已完成/)
  assert.match(template, /modelRunStatusType\(proposalOf\(message\)\)/)
  assert.match(template, /modelRunSummaryTitle\(proposalOf\(message\)\)/)
  assert.match(template, /正式写入任务/)
  assert.match(finalHelpers, /modelRunFinishedWithoutPersistedWrites/)
  assert.match(finalHelpers, /'建模计划已结束，无正式写入'/)
  assert.match(finalHelpers, /'completed_without_writes'/)
  assert.doesNotMatch(finalHelpers, /场景建模已完成并应用/)
  assert.match(source, /\.model-run-summary\.is-success/)
  assert.match(source, /\.model-run-summary\.is-warning/)
  assert.doesNotMatch(
    source,
    /\.model-run-summary \{[^}]*var\(--success\)/,
  )

  const statusStart = source.indexOf('function modelRunStatusType')
  const statusEnd = source.indexOf('\nfunction modelRunSummaryTitle', statusStart)
  const statusSource = source.slice(statusStart, statusEnd)
  const statusBody = statusSource.slice(statusSource.indexOf('{') + 1, statusSource.lastIndexOf('}'))
  const statusType = Function(
    'modelExecutionSummary',
    'modelRunHasPersistedWrites',
    'modelDraftOnlyTaskCount',
    'modelPartiallyAppliedTaskCount',
    `return function (proposal) {${statusBody}}`,
  )(
    (proposal) => proposal.summary,
    (proposal) => proposal.persisted,
    (proposal) => proposal.drafts,
    (proposal) => proposal.partial,
  )
  assert.equal(statusType({ summary: { final: true, status: 'completed', remaining_issue_count: 0 }, persisted: false, drafts: 0, partial: 0 }), 'info')
  assert.equal(statusType({ summary: { final: true, status: 'completed_with_gaps', remaining_issue_count: 1 }, persisted: false, drafts: 1, partial: 0 }), 'warning')
  assert.equal(statusType({ summary: { final: true, status: 'completed', remaining_issue_count: 0 }, persisted: true, drafts: 0, partial: 0 }), 'success')

  const contentStart = source.indexOf('function assistantMessageContent')
  const contentEnd = source.indexOf('\nfunction proposalStatusType', contentStart)
  const contentSource = source.slice(contentStart, contentEnd)
  const contentBody = contentSource.slice(contentSource.indexOf('{') + 1, contentSource.lastIndexOf('}'))
  const displayContent = Function(
    'proposalOf',
    'modelRunFinishedWithoutPersistedWrites',
    'modelRunSummaryMessage',
    `return function (message) {${contentBody}}`,
  )(
    (message) => message.proposal,
    () => true,
    () => '本轮未确认到正式资源写入；计划没有产生可应用的正式定义。',
  )
  const displayed = displayContent({
    content: '**场景建模已完成并应用**',
    proposal: { kind: 'scenario_model' },
  })
  assert.doesNotMatch(displayed, /已完成并应用/)
  assert.match(displayed, /本轮未确认到正式资源写入/)
})

test('scenario task outcome only emits applied feedback for verified writes', () => {
  const source = readFileSync(
    new URL('../src/components/GlobalAssistant.vue', import.meta.url),
    'utf8',
  )
  const outcome = source.slice(
    source.indexOf('function announceModelTaskOutcome'),
    source.indexOf('function beginModelTaskRecovery'),
  )
  const recovery = source.slice(
    source.indexOf('function beginModelTaskRecovery'),
    source.indexOf('async function applyModelTask'),
  )
  const apply = source.slice(
    source.indexOf('async function applyModelTask'),
    source.indexOf('async function applyProposal'),
  )

  assert.match(
    outcome,
    /if \(modelTaskHasPersistedWrites\(task\)\) \{[\s\S]*?assistant-proposal-applied[\s\S]*?ElMessage\.success/,
  )
  assert.equal((outcome.match(/assistant-proposal-applied/g) || []).length, 1)
  assert.match(outcome, /task\.status === 'drafted_with_gaps'[\s\S]*?ElMessage\.warning/)
  assert.match(outcome, /\['deferred', 'skipped'\]\.includes\(task\.status\)[\s\S]*?ElMessage\.info/)
  assert.match(outcome, /task\.status === 'empty'[\s\S]*?ElMessage\.info/)
  assert.doesNotMatch(recovery, /assistant-proposal-applied/)
  assert.match(recovery, /announceModelTaskOutcome\(recoveredProposal, taskId, true\)/)
  assert.doesNotMatch(apply, /assistant-proposal-applied/)
  assert.match(apply, /announceModelTaskOutcome\(updatedProposal, task\.id, result\?\.status === 'replayed'\)/)
  assert.doesNotMatch(apply, /本任务已确认并写入/)
})

test('only the active message card is actionable when two messages share a model proposal id', () => {
  const source = readFileSync(
    new URL('../src/components/GlobalAssistant.vue', import.meta.url),
    'utf8',
  )
  const start = source.indexOf('function isActiveModelRun')
  const end = source.indexOf('\nfunction modelTasks', start)
  assert.notEqual(start, -1)
  assert.notEqual(end, -1)
  const functionSource = source.slice(start, end)
  const body = functionSource.slice(
    functionSource.indexOf('{') + 1,
    functionSource.lastIndexOf('}'),
  )
  const sharedProposal = {
    kind: 'scenario_model',
    proposal_id: 'shared-run-id',
    status: 'in_progress',
    payload: {},
  }
  const activeMessage = { id: 'message-new', proposal: sharedProposal }
  const staleMessage = { id: 'message-old', proposal: sharedProposal }
  const sameDurableMessageReloaded = { id: 'message-new', proposal: { ...sharedProposal } }
  const isActiveModelRun = Function(
    'activeModelRunMessage',
    `return function (message) {${body}}`,
  )({ value: activeMessage })

  assert.equal(isActiveModelRun(activeMessage), true)
  assert.equal(isActiveModelRun(sameDurableMessageReloaded), true)
  assert.equal(isActiveModelRun(staleMessage), false)
  assert.doesNotMatch(functionSource, /proposal_id|proposalOf\(/)
})

test('scenario model recovery reloads the durable proposal instead of fabricating one', () => {
  const source = readFileSync(
    new URL('../src/components/GlobalAssistant.vue', import.meta.url),
    'utf8',
  )
  const start = source.indexOf('async function recoverSucceededCompilation')
  const end = source.indexOf('async function recoverFailedCompilation', start)
  assert.notEqual(start, -1)
  assert.notEqual(end, -1)
  const recovery = source.slice(start, end)

  assert.match(
    recovery,
    /if \(result\.apply_ready && proposalId && !hasProposalMessage\) \{[\s\S]*?await api\.listAssistantMessages\(/,
  )
  assert.doesNotMatch(
    recovery,
    /recoveredMessages\s*=\s*\[\.\.\.recoveredMessages,\s*\{/,
  )
  assert.doesNotMatch(recovery, /compilation-result-\$\{job\.id\}/)
})

test('scenario model recovery remains bound to its durable scope and monotonic revision', () => {
  const source = readFileSync(
    new URL('../src/components/GlobalAssistant.vue', import.meta.url),
    'utf8',
  )
  const taskRecovery = source.slice(
    source.indexOf('function beginModelTaskRecovery'),
    source.indexOf('async function applyModelTask'),
  )
  const discovery = source.slice(
    source.indexOf('async function discoverCompilationForThread'),
    source.indexOf('function beginCompilationRecoveryFromEvent'),
  )
  const succeeded = source.slice(
    source.indexOf('async function recoverSucceededCompilation'),
    source.indexOf('async function recoverFailedCompilation'),
  )

  assert.match(taskRecovery, /ownerScopeKey = assistantScopeKey\.value/)
  assert.match(taskRecovery, /generation === modelTaskRecoveryGeneration/)
  assert.match(taskRecovery, /const recoveryContext = apiContext\(\)/)
  assert.match(taskRecovery, /if \(!isCurrent\(\)\) return[\s\S]*?await api\.listAssistantMessages[\s\S]*?if \(!isCurrent\(\)\) return/)
  assert.match(source, /Math\.max\(localRevision, responseRevision\)/)
  assert.match(discovery, /attachCompilationEventStream\(job, scope\)/)
  assert.doesNotMatch(discovery, /listAssistantCompilationJobs|getAssistantCompilationJob/)
  assert.match(succeeded, /compilationPathFromScopeKey\(result\.proposal_scope_key, scope\.scenarioId\)/)
  assert.match(succeeded, /if \(canonicalPath !== currentPath\)/)
})

test('scenario model cards never expose the legacy whole-proposal apply action', () => {
  const source = readFileSync(
    new URL('../src/components/GlobalAssistant.vue', import.meta.url),
    'utf8',
  )

  assert.match(
    source,
    /v-if="proposalOf\(message\)\?\.kind !== 'scenario_model' && !modelTasks\(proposalOf\(message\)\)\.length" class="proposal-actions"/,
  )
  assert.doesNotMatch(
    source,
    /v-if="!modelTasks\(proposalOf\(message\)\)\.length" class="proposal-actions"/,
  )
})

test('materialized AI drafts stay durable, disabled and linked to the original modelling stages', () => {
  const drafts = normalizeScenarioModelDrafts({
    items: [{
      id: 'draft-action-1',
      scenario_id: 'scenario-1',
      proposal_id: 'proposal-1',
      task_id: 'capabilities',
      resource_kind: 'action',
      resource_key: 'action:create-order',
      payload: { name: '创建订单', enabled: true },
      validation_issues: [
        { code: 'EXECUTOR_MISSING', message: '尚未绑定执行方式', blocking: true, resolution_hint: '选择受治理执行器' },
      ],
      issues_count: 1,
      blocking_issue_count: 1,
      draft_status: 'needs_revision',
      enabled: true,
      publishable: true,
    }],
  })

  assert.equal(drafts.length, 1)
  assert.equal(drafts[0].enabled, false)
  assert.equal(drafts[0].publishable, false)
  assert.equal(scenarioDraftStage(drafts[0].resource_kind), 'actions')
  assert.equal(scenarioDraftIssueCount(drafts[0]), 1)
  assert.equal(scenarioDraftBlockingIssueCount(drafts[0]), 1)
  assert.equal(drafts[0].validation_issues[0].resolution_hint, '选择受治理执行器')

  const conceptual = normalizeScenarioModelDrafts([{
    id: 'draft-mapping-1', proposal_id: 'proposal-1', task_id: 'mapping',
    resource_kind: 'conceptual_mapping', resource_key: 'mapping:order',
    payload: { entity: '订单' }, validation_issues: [], draft_status: 'needs_binding',
    enabled: false, publishable: false,
  }])
  assert.equal(scenarioDraftStage(conceptual[0].resource_kind), 'mappings')
  const instance = normalizeScenarioModelDrafts([{
    id: 'draft-instance-1', proposal_id: 'proposal-1', task_id: 'ontology',
    resource_kind: 'instance', resource_key: 'instance:order-1', revision: 0,
    payload: { name: '订单 1' }, validation_issues: [], draft_status: 'needs_attention',
    enabled: false, publishable: false,
  }])
  assert.equal(scenarioDraftStage(instance[0].resource_kind), 'instances')
  const property = normalizeScenarioModelDrafts([{
    id: 'draft-property-1', proposal_id: 'proposal-1', task_id: 'ontology',
    resource_kind: 'property', resource_key: 'entity:order:property:code', revision: 0,
    payload: { entity_ref: 'entity:order', name: '订单编号', data_type: 'string' },
    validation_issues: [], draft_status: 'ready_for_review', enabled: false, publishable: false,
  }])
  assert.equal(scenarioDraftStage(property[0].resource_kind), 'ontology')
  assert.equal(draftRefToken({ kind: 'entity', key: 'entity:Order' }), 'entity:Order')
  assert.equal(draftRefToken({ kind: 'data_source', id: 'source-1' }), 'source-1')
})

test('scenario page projects durable model resources into normal canvases and tabs without AI warning chrome', () => {
  const source = readFileSync(
    new URL('../src/views/ScenarioDetail.vue', import.meta.url),
    'utf8',
  )
  const graphCanvas = readFileSync(
    new URL('../src/components/GraphCanvas.vue', import.meta.url),
    'utf8',
  )
  const apiSource = readFileSync(
    new URL('../src/api/index.ts', import.meta.url),
    'utf8',
  )
  const editorPanel = readFileSync(
    new URL('../src/components/EditorPanel.vue', import.meta.url),
    'utf8',
  )

  assert.doesNotMatch(source, /ScenarioDraftWorkbench/)
  assert.doesNotMatch(source, /AI 待修正草稿|待修正草稿/)
  const pageTemplate = source.slice(0, source.indexOf('<script setup'))
  assert.doesNotMatch(pageTemplate, /AI 草稿|AI 已写入|草稿停用|>修正(?:并编排)?</)
  assert.doesNotMatch(pageTemplate, /class="mapping-prerequisite"|class="mapping-readiness-alert"/)
  assert.doesNotMatch(pageTemplate, /stat-draft|is-ai-draft|inlineDraftRowClass/)
  assert.doesNotMatch(pageTemplate, /scenarioDraftsLoading|scenarioDraftsError|scenarioDraftPromotionError/)
  assert.doesNotMatch(graphCanvas, /AI 已写入|draftStatus|e\.draft/)
  assert.doesNotMatch(graphCanvas, /:stroke-dasharray="n\.meta\?\.aiDraft|e\.dashed \|\| e\.draft/)
  assert.match(source, /对象类型 <b>\{\{ detail\.entities\.length \+ scenarioDraftsOf\('entity'\)\.length \}\}<\/b>/)
  assert.match(source, /runtime_instance_count \|\| objectTotal \+ scenarioDraftsOf\('instance'\)\.length/)
  assert.match(source, /api\.searchObjects/)
  assert.match(source, /api\.listRelationInstances/)
  for (const rowsName of ['objectMappingRows', 'relationMappingRows', 'functionRows', 'actionRows', 'ruleRows', 'eventRows', 'workflowRows']) {
    assert.match(source, new RegExp(`<b>\\{\\{ ${rowsName}\\.length \\}\\}</b>`))
  }
  assert.match(source, /loadScenarioModelDrafts|listScenarioModelDrafts/)
  assert.match(source, /const openScenarioDrafts = computed\(\(\) => scenarioDrafts\.value\.filter\(scenarioDraftIsOpen\)\)/)
  assert.match(source, /function scenarioDraftDisplayId[\s\S]*?`ai-draft:\$\{item\.resource_kind\}:\$\{item\.id\}`/)

  const schemaGraph = source.slice(
    source.indexOf('const schemaGraph = computed'),
    source.indexOf('const instanceGraph = computed'),
  )
  assert.match(schemaGraph, /scenarioDraftsOf\('entity'\)/)
  assert.match(schemaGraph, /scenarioDraftsOf\('property'\)/)
  assert.match(schemaGraph, /scenarioDraftsOf\('relation'\)/)
  assert.match(schemaGraph, /meta:\s*\{[\s\S]*?aiDraft:\s*draft/)

  const instanceGraph = source.slice(
    source.indexOf('const instanceGraph = computed'),
    source.indexOf('const legend = computed'),
  )
  assert.match(instanceGraph, /scenarioDraftsOf\('instance'\)/)
  assert.match(instanceGraph, /aiDraft:\s*draft/)

  const mergedRows = [
    ['objectMappingRows', /scenarioDraftsOf\('mapping', 'data_mapping', 'conceptual_mapping'\)[\s\S]*?detail\.value\.mappings/],
    ['relationMappingRows', /scenarioDraftsOf\('relation_mapping'\)[\s\S]*?detail\.value\.relation_mappings/],
    ['functionRows', /scenarioDraftsOf\('function'\)[\s\S]*?detail\.value\.functions/],
    ['actionRows', /scenarioDraftsOf\('action'\)[\s\S]*?detail\.value\.actions/],
    ['ruleRows', /scenarioDraftsOf\('rule'\)[\s\S]*?detail\.value\.rules/],
    ['eventRows', /scenarioDraftsOf\('event'\)[\s\S]*?detail\.value\.events/],
    ['workflowRows', /scenarioDraftsOf\('workflow'\)[\s\S]*?detail\.value\.workflows/],
  ]
  for (const [name, projection] of mergedRows) {
    assert.match(source, new RegExp(`const ${name} = computed`))
    assert.match(source, projection)
  }
  assert.match(source, /v-if="objectMappingRows\.length"[\s\S]*?v-for="row in objectMappingRows"/)
  assert.match(source, /v-if="relationMappingRows\.length"[\s\S]*?v-for="row in relationMappingRows"/)
  for (const name of ['functionRows', 'actionRows', 'ruleRows', 'eventRows', 'workflowRows']) {
    assert.match(source, new RegExp(`:data="${name}"`))
  }

  const operationBranches = [
    ['objectMappingRows', 'doPreviewMapping'],
    ['functionRows', 'doRunFunction'],
    ['actionRows', 'doExecuteAction'],
    ['ruleRows', 'doEvalRule'],
    ['eventRows', 'publishEvent'],
    ['workflowRows', 'doExecuteWorkflow'],
  ]
  for (const [rowsName, dangerousOperation] of operationBranches) {
    const surfaceStart = rowsName === 'objectMappingRows'
      ? source.indexOf('v-if="objectMappingRows.length"')
      : source.indexOf(`:data="${rowsName}"`)
    const nextTab = source.indexOf('<el-tab-pane', surfaceStart)
    const section = source.slice(surfaceStart, nextTab === -1 ? source.length : nextTab)
    const draftAction = section.indexOf('row._isAiDraft')
    const formalBranch = section.indexOf('<template v-else>', draftAction)
    const operation = section.indexOf(dangerousOperation, draftAction)
    assert.ok(surfaceStart >= 0, `${rowsName} must render in its original surface`)
    assert.ok(draftAction >= 0, `${rowsName} must branch on AI draft rows`)
    assert.ok(formalBranch > draftAction, `${rowsName} must keep formal operations in a separate branch`)
    assert.ok(operation > formalBranch, `${dangerousOperation} must be hidden from AI draft rows`)
  }

  const directEditor = source.slice(
    source.indexOf('async function startEditingScenarioDraft'),
    source.indexOf('function unresolvedDraftReferenceIssue'),
  )
  assert.match(directEditor, /^async function startEditingScenarioDraft\(item: ScenarioModelDraftResource\)/)
  assert.match(directEditor, /editor\.value =/)
  assert.match(directEditor, /mappingForm\.value =/)
  assert.match(directEditor, /functionForm\.value =/)
  assert.match(directEditor, /wfEditor\.value =/)
  assert.doesNotMatch(directEditor, /selectedScenarioDraft|scenarioDraftDrawer|inspectScenarioDraft/)
  assert.match(source, /node\.meta\?\.aiDraft[\s\S]*?startEditingScenarioDraft\(node\.meta\.aiDraft\)/)
  assert.match(source, /@click="startEditingScenarioDraft\(row\._scenarioDraft\)"/)
  assert.match(source, /v-if="row\._isAiDraft"[^>]*@click="startEditingScenarioDraft\(row\._scenarioDraft\)"[^>]*>编辑<\/el-button>/)
  assert.match(source, /activeScenarioDraftPromotion/)
  assert.match(source, /resolveScenarioDraftAfterFormalSave/)
  assert.match(source, /resolveScenarioModelDraft/)
  assert.match(apiSource, /listScenarioModelDrafts: \(id: string, params:/)
  assert.match(apiSource, /model-drafts`, \{ params \}/)
  assert.match(source, /while \(true\)/)
  assert.match(source, /metadata\?\.has_more !== true/)
  assert.match(source, /nextOffset <= offset/)
  assert.match(source, /草稿分页游标无效/)
  assert.match(source, /draftsById\.size !== expectedTotal/)
  assert.match(source, /formalPropertyResourceId/)
  assert.match(source, /openEntity\(entity\.id\)/)
  assert.match(source, /draftPropertyEditorIndex/)
  assert.match(source, /runtime_kind: 'contract'/)
  assert.match(source, /status: 'draft'/)
  assert.match(source, /enabled: false/)
  assert.doesNotMatch(source, /detail\.value\.(?:functions|actions|rules|events|workflows)\s*=\s*\[\.\.\..*scenarioDrafts/)
  assert.match(editorPanel, /focusPropertyIndex/)
  assert.match(editorPanel, /draft-property-focus/)
})

test('successful Action artifacts ignore a forged download URL', () => {
  const id = 'a'.repeat(32)
  const attachment = actionArtifactAttachment({
    name: 'execute_action',
    result: {
      status: 'success',
      result: {
        artifact: {
          id,
          filename: '项目报告.docx',
          format: 'docx',
          mime: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
          size: 123,
          sha256: 'b'.repeat(64),
          download_url: 'https://attacker.example/steal',
        },
      },
    },
  })
  assert.equal(attachment?.url, `/api/data-sources/files/${id}/download`)
  assert.equal(attachment?.mime, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
})

test('only validated successful execute_action results become download cards', () => {
  const artifact = {
    id: 'c'.repeat(32),
    filename: 'report.xlsx',
    format: 'xlsx',
    mime: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    size: 42,
    sha256: 'd'.repeat(64),
  }
  assert.equal(actionArtifactAttachment({ name: 'read_file', result: { status: 'success', result: { artifact } } }), null)
  assert.equal(actionArtifactAttachment({ name: 'execute_action', result: { status: 'dry_run', result: { artifact } } }), null)
  assert.equal(actionArtifactAttachment({ name: 'execute_action', result: { status: 'success', result: { artifact: { ...artifact, id: '../escape' } } } }), null)
  assert.equal(actionArtifactAttachment({ name: 'execute_action', result: { status: 'success', result: { artifact: { ...artifact, filename: 'run.exe' } } } }), null)
  assert.equal(actionArtifactAttachment({ name: 'execute_action', result: { status: 'success', result: { artifact: { ...artifact, format: 'docx' } } } }), null)
  assert.equal(actionArtifactAttachment({ name: 'execute_action', result: { status: 'success', result: { artifact: { ...artifact, mime: 'text/html' } } } }), null)
  assert.equal(actionArtifactAttachment({ name: 'execute_action', result: { status: 'success', result: { artifact: { ...artifact, sha256: '' } } } }), null)
})

test('native DOCX, XLSX and Markdown artifacts keep matching format and MIME metadata', () => {
  const cases = [
    ['report.docx', 'docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'],
    ['budget.xlsx', 'xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'],
    ['summary.md', 'markdown', 'text/markdown; charset=utf-8'],
  ]
  for (const [filename, format, mime] of cases) {
    const attachment = actionArtifactAttachment({
      name: 'execute_action',
      result: {
        status: 'success',
        result: {
          artifact: {
            id: 'e'.repeat(32), filename, format, mime, size: 64, sha256: 'f'.repeat(64),
          },
        },
      },
    })
    assert.equal(attachment?.filename, filename)
    assert.equal(attachment?.format, format)
    assert.equal(attachment?.mime, mime)
  }
})

test('standard mcpServers HTTP config is normalized for a safe preview', () => {
  const parsed = parseStandardMCPConfig(JSON.stringify({
    mcpServers: {
      search: {
        type: 'http',
        url: 'https://example.test/mcp',
        headers: { Authorization: 'Bearer test-only' },
      },
    },
  }))
  assert.equal(parsed.preview[0].name, 'search')
  assert.equal(parsed.preview[0].transport, 'streamable_http')
  assert.deepEqual(parsed.preview[0].headerKeys, ['Authorization'])
  assert.equal(JSON.stringify(parsed.preview).includes('test-only'), false)
})

test('standard MCP config rejects missing wrappers and non-string header values', () => {
  assert.throws(() => parseStandardMCPConfig('{"service":{}}'), /mcpServers/)
  assert.throws(() => parseStandardMCPConfig(JSON.stringify({
    mcpServers: { search: { type: 'http', url: 'https://example.test/mcp', headers: { Authorization: 1 } } },
  })), /必须是文本/)
  assert.throws(() => parseStandardMCPConfig(JSON.stringify({
    mcpServers: { search: { type: 'http', url: 'https://user:password@example.test/mcp' } },
  })), /用户凭据/)
  assert.throws(() => parseStandardMCPConfig(JSON.stringify({
    mcpServers: { search: { type: 'http', url: 'https://example.test/mcp?access_token=test-only' } },
  })), /查询参数不能携带凭据/)
  assert.throws(() => parseStandardMCPConfig(JSON.stringify({
    mcpServers: {
      search: {
        type: 'http',
        url: 'https://example.test/mcp',
        headers: { Authorization: 'one', authorization: 'two' },
      },
    },
  })), /重复键名/)
})

test('Agent MCP publication relies on the standard discoverable tool schema', () => {
  const source = readFileSync(
    new URL('../src/components/AgentMCPPublications.vue', import.meta.url),
    'utf8',
  )
  assert.match(source, /使用标准 MCP 工具发现与调用/)
  assert.match(source, /tools\/list 自动发现 invoke_agent 的 message 和 conversation_id 参数/)
  assert.doesNotMatch(source, /ai\.rhzy\//)
  assert.doesNotMatch(source, /宿主透传契约/)
  assert.match(source, /@closed="clearCreatedService"/)
})

test('nested object and array schema survives no-JSON editor round trip', () => {
  const source = {
    type: 'object',
    properties: {
      project: {
        type: 'object',
        description: '项目信息',
        properties: {
          name: { type: 'string', description: '项目名称' },
          code: { type: 'string', enum: ['A', 'B'] },
        },
        required: ['name'],
        additionalProperties: false,
      },
      lines: {
        type: 'array',
        minItems: 1,
        items: {
          type: 'object',
          properties: {
            sku: { type: 'string' },
            quantity: { type: 'integer', minimum: 1 },
          },
          required: ['sku', 'quantity'],
          additionalProperties: false,
        },
      },
    },
    required: ['project', 'lines'],
    additionalProperties: false,
  }
  const fields = flattenSchemaFields(source)
  assert.deepEqual(
    fields.map((field) => field.name),
    ['project', 'project.name', 'project.code', 'lines', 'lines.0.sku', 'lines.0.quantity'],
  )
  const rebuilt = buildSchemaFromFields(fields)
  assert.deepEqual(rebuilt.required, ['project', 'lines'])
  assert.deepEqual(rebuilt.properties.project.required, ['name'])
  assert.deepEqual(rebuilt.properties.project.properties.code.enum, ['A', 'B'])
  assert.equal(rebuilt.properties.lines.minItems, 1)
  assert.equal(rebuilt.properties.lines.items.type, 'object')
  assert.deepEqual(rebuilt.properties.lines.items.required, ['sku', 'quantity'])
  assert.equal(rebuilt.properties.lines.items.properties.quantity.minimum, 1)
})

test('guided retry restores attachments paired with the clicked response, not newer uploads', () => {
  const original = { id: 'original', filename: 'original.docx', size: 100, status: 'parsed' }
  const newer = { id: 'newer', filename: 'newer.xlsx', size: 200, status: 'parsed' }
  const originalResponse = { id: 'assistant-1', thread_id: 'thread-1', role: 'assistant', content: 'timeout' }
  const messages = [
    { id: 'user-1', thread_id: 'thread-1', role: 'user', content: 'compile', attachments: [original] },
    originalResponse,
    { id: 'user-2', thread_id: 'thread-1', role: 'user', content: 'another task', attachments: [newer] },
    { id: 'assistant-2', thread_id: 'thread-1', role: 'assistant', content: 'done' },
  ]

  assert.deepEqual(retryAttachmentsForMessage(messages, originalResponse, 'thread-1'), [original])
})

test('guided retry accepts only well-formed parsed attachments from the current thread', () => {
  const valid = { id: 'valid', filename: 'model.docx', size: 100, status: 'parsed' }
  const response = { id: 'assistant-1', thread_id: 'thread-1', role: 'assistant', content: 'timeout' }
  const messages = [
    {
      id: 'user-1',
      thread_id: 'thread-1',
      role: 'user',
      content: 'compile',
      attachments: [
        valid,
        { ...valid },
        { id: 'failed', filename: 'failed.docx', size: 100, status: 'error' },
        { id: '', filename: 'missing-id.docx', size: 100, status: 'parsed' },
      ],
    },
    response,
  ]

  assert.deepEqual(retryAttachmentsForMessage(messages, response, 'thread-1'), [valid])
  assert.deepEqual(retryAttachmentsForMessage(messages, response, 'thread-2'), [])
})

test('failed attachment compilation creates an editable correction draft instead of replaying old chat', () => {
  const draft = compilationRetryDraft('revise_and_retry')
  assert.match(draft, /对附件模型的补充\/修正/)
  assert.match(draft, /连同原附件重新编译/)
  assert.match(draft, /保留每项模型的来源段落/)
  assert.match(draft, /需要补充或修正的内容/)
  assert.equal(compilationRetryDraft('keep_failed'), '')

  const guided = compilationRetryDraft('retry', '只修正关系约束')
  assert.match(guided, /连同原附件重新编译/)
  assert.match(guided, /只修正关系约束/)
})

test('compilation recovery bookmarks are isolated by tenant, user, scenario and thread', () => {
  const storage = memoryStorage()
  const scope = { tenantId: 'tenant-a', userId: 'user-a', scenarioId: 'scenario-a', threadId: 'thread-a' }
  assert.equal(saveCompilationJobBookmark(storage, scope, 'job-a'), true)
  assert.equal(readCompilationJobBookmark(storage, scope), 'job-a')
  for (const mismatch of [
    { ...scope, tenantId: 'tenant-b' },
    { ...scope, userId: 'user-b' },
    { ...scope, scenarioId: 'scenario-b' },
    { ...scope, threadId: 'thread-b' },
  ]) assert.equal(readCompilationJobBookmark(storage, mismatch), '')
  clearCompilationJobBookmark(storage, scope)
  assert.equal(readCompilationJobBookmark(storage, scope), '')

  assert.equal(savePendingCompilationJobBookmark(storage, scope, 'job-pending'), true)
  assert.equal(readPendingCompilationJobBookmark(storage, scope), 'job-pending')
  assert.equal(readPendingCompilationJobBookmark(storage, { ...scope, scenarioId: 'scenario-b' }), '')
})

test('compilation recovery selects a bookmarked terminal job or a live job only within the scenario', () => {
  const base = {
    thread_id: 'thread-a', progress: {}, llm_calls_used: 1, llm_call_budget: 10,
    result_ready: false, error_code: '', error_message: '', started_at: '', updated_at: '',
  }
  const jobs = [
    { ...base, id: 'other-running', scenario_id: 'scenario-b', status: 'running' },
    { ...base, id: 'finished', scenario_id: 'scenario-a', status: 'succeeded', result_ready: true },
    { ...base, id: 'live', scenario_id: 'scenario-a', status: 'running' },
  ]
  assert.equal(selectCompilationJobForRecovery(jobs, 'scenario-a')?.id, 'live')
  assert.equal(selectCompilationJobForRecovery(jobs, 'scenario-a', 'finished')?.id, 'finished')
  assert.equal(selectCompilationJobForRecovery(jobs, 'scenario-b')?.id, 'other-running')
  assert.equal(selectCompilationJobForRecovery(jobs, 'scenario-c'), null)
})

test('scenario compiler issues collapse repeated root causes to one representative with accurate counts', () => {
  const issues = [
    { code: 'unknown_rule_field', message: '字段 A 不存在', blocking: true, source_refs: ['p0001', 'p0001'] },
    { code: ' UNKNOWN_RULE_FIELD ', message: '字段 B 不存在', source_refs: ['p0002'] },
    { code: 'missing_reference', message: '缺少操作引用' },
    { code: 'unknown_rule_field', message: '建议补充字段说明', blocking: false },
  ]

  const groups = groupScenarioModelIssues(issues)
  assert.deepEqual(groups.map((group) => [group.key, group.count, group.blockingCount, group.issues.length]), [
    ['unknown_rule_field', 3, 2, 1],
    ['missing_reference', 1, 1, 1],
  ])
  assert.deepEqual(groups[0].issues[0].sourceRefs, ['p0001'])
  assert.equal(groups.reduce((total, group) => total + group.count, 0), issues.length)
  assert.equal(groups.flatMap((group) => group.issues).length, groups.length)
  assert.equal(scenarioModelIssueLabel('unknown_rule_field'), '规则字段未定义')
  assert.equal(scenarioModelIssueLabel('invalid_relation_constraints'), '关系约束格式不正确')
  assert.equal(scenarioModelIssueLabel('MAPPING_DEFERRED_NO_DATA_SOURCE'), '数据映射等待数据源')
  assert.equal(scenarioModelIssueLabel('data_source_dependency'), '数据源尚未接入或绑定')
  assert.equal(scenarioModelIssueLabel('PREREQUISITE_DRAFT_ONLY'), '前置任务仅保留草稿')
  assert.equal(scenarioModelIssueLabel('INVALID_TASK_DEPENDENCY'), '建模任务依赖异常')
  assert.equal(scenarioModelIssueLabel('future_code'), '其他预检问题')
})

test('data source issue aliases produce one summary row and preserve aggregate scale', () => {
  const groups = groupScenarioModelIssues([
    {
      code: 'missing_data_source',
      message: '订单映射缺少数据源',
      count: 3,
      blocking_count: 3,
      affected_count: 2,
      source_refs: ['mapping:order', 'mapping:order'],
    },
    {
      code: 'MAPPING_DEFERRED_NO_DATA_SOURCE',
      message: '客户映射等待数据源',
      count: 5,
    },
    {
      code: 'document_reported_issue',
      reported_code: 'DATA_SOURCE_UNAVAILABLE',
      message: '附件声明的数据源当前不可用',
      count: 4,
      blocking: false,
    },
    {
      code: 'missing_reference',
      message: '关系映射引用的数据源不存在',
      count: 2,
    },
  ])

  assert.equal(groups.length, 1)
  assert.equal(groups[0].key, 'data_source_dependency')
  assert.equal(groups[0].count, 14)
  assert.equal(groups[0].blockingCount, 10)
  assert.equal(groups[0].affectedCount, 13)
  assert.equal(groups[0].issues.length, 1)
  assert.deepEqual(groups[0].issues[0].sourceRefs, ['mapping:order'])
  assert.equal(groups[0].message, '数据源、物理表或字段尚未接入或绑定。')
})

test('relation axiom form produces a closed payload without JSON authoring', () => {
  const form = relationConstraintForm({
    asymmetric: true,
    source_min_cardinality: 0,
    source_max_cardinality: 2,
  })
  const payload = buildRelationConstraints(form, {
    relationType: 'N:M',
    sourceEntityId: 'object-a',
    targetEntityId: 'object-a',
  })
  assert.deepEqual(payload, {
    irreflexive: true,
    asymmetric: true,
    source_min_cardinality: 0,
    source_max_cardinality: 2,
  })
})

test('relation axiom form blocks incompatible endpoints and cardinality ranges', () => {
  const transitive = relationConstraintForm({ transitive: true })
  assert.throws(
    () => buildRelationConstraints(transitive, {
      relationType: 'N:M', sourceEntityId: 'source', targetEntityId: 'target',
    }),
    /只适用于源\/目标相同/,
  )
  const invalidRange = relationConstraintForm({
    source_min_cardinality: 2,
    source_max_cardinality: 1,
  })
  assert.throws(
    () => buildRelationConstraints(invalidRange, {
      relationType: 'N:M', sourceEntityId: 'object', targetEntityId: 'object',
    }),
    /最小基数不能大于最大基数/,
  )
})

test('scenario return_to accepts one same-origin slash and rejects navigation escapes', () => {
  assert.equal(safeInternalReturnPath('/agents?scenario_id=s1#scope'), '/agents?scenario_id=s1#scope')
  assert.equal(safeInternalReturnPath(['/tasks/1', '/forged']), '/tasks/1')
  for (const value of [
    'https://attacker.example',
    '//attacker.example/path',
    '/\\attacker.example',
    '/%5C%5Cattacker.example',
    '/%2F%2Fattacker.example',
    '/ok%0d%0aLocation:https://attacker.example',
    'scenarios',
    undefined,
  ]) {
    assert.equal(safeInternalReturnPath(value, '/scenarios'), '/scenarios')
  }
})

test('relation mapping payload closes mode-specific fields and trims user selections', () => {
  const sourceForeignKey = buildRelationMappingPayload({
    relation_id: ' relation-a ',
    source_mapping_id: ' source-map ',
    target_mapping_id: ' target-map ',
    mode: 'source_fk',
    foreign_key_column: ' target_id ',
    join_data_source_id: 'stale-source',
    join_table_name: 'stale-table',
    source_key_column: 'stale-source-key',
    target_key_column: 'stale-target-key',
  })
  assert.deepEqual(sourceForeignKey, {
    relation_id: 'relation-a',
    source_mapping_id: 'source-map',
    target_mapping_id: 'target-map',
    mode: 'source_fk',
    foreign_key_column: 'target_id',
    join_data_source_id: '',
    join_table_name: '',
    source_key_column: '',
    target_key_column: '',
  })
  assert.equal(relationMappingModeLabel('source_fk'), '源对象表保存目标主键')
  assert.deepEqual(missingRelationMappingFields(sourceForeignKey), [])

  const targetForeignKey = buildRelationMappingPayload({
    ...sourceForeignKey,
    mode: 'target_fk',
    foreign_key_column: ' source_id ',
  })
  assert.equal(targetForeignKey.mode, 'target_fk')
  assert.equal(targetForeignKey.foreign_key_column, 'source_id')
  assert.equal(targetForeignKey.join_data_source_id, '')
  assert.equal(relationMappingModeLabel('target_fk'), '目标对象表保存源主键')
})

test('join-table relation mapping never leaks a stale foreign key field', () => {
  const payload = buildRelationMappingPayload({
    relation_id: 'relation-a',
    source_mapping_id: 'source-map',
    target_mapping_id: 'target-map',
    mode: 'join_table',
    foreign_key_column: 'stale_foreign_key',
    join_data_source_id: 'source-a',
    join_table_name: 'project_members',
    source_key_column: 'project_id',
    target_key_column: 'member_id',
  })
  assert.equal(payload.foreign_key_column, '')
  assert.equal(payload.join_table_name, 'project_members')
  assert.deepEqual(missingRelationMappingFields(payload), [])
  assert.equal(relationMappingPayloadFingerprint(payload), JSON.stringify(payload))

  const incomplete = buildRelationMappingPayload({ ...payload, target_key_column: ' ' })
  assert.deepEqual(missingRelationMappingFields(incomplete), ['目标主键列'])
  assert.throws(() => buildRelationMappingPayload({ mode: 'invalid' }), /请选择关系映射方式/)
})

test('new Agents and scenario switches receive independent empty capability scopes', () => {
  const first = emptyAgentCapabilityScope()
  const second = emptyAgentCapabilityScope()
  first.functions.selected_ids.push('function-a')
  assert.deepEqual(second, {
    functions: { mode: 'explicit', selected_ids: [] },
    actions: { mode: 'explicit', selected_ids: [] },
    rules: { mode: 'explicit', selected_ids: [] },
    events: { mode: 'explicit', selected_ids: [] },
    workflows: { mode: 'explicit', selected_ids: [] },
  })
})

test('capability editor clones valid selections and fails closed on missing categories', () => {
  const cloned = cloneAgentCapabilityScope({
    functions: { mode: 'explicit', selected_ids: ['function-a', 'function-a'] },
    actions: { mode: 'all', selected_ids: ['forged-id'] },
  })
  assert.deepEqual(cloned.functions.selected_ids, ['function-a'])
  assert.deepEqual(cloned.actions, { mode: 'all', selected_ids: [] })
  assert.deepEqual(cloned.rules, { mode: 'explicit', selected_ids: [] })
})

test('template center accepts only supported native template formats', () => {
  for (const filename of ['audit.docx', 'statements.XLSX', 'notes.md', 'guide.Markdown']) {
    assert.equal(isSupportedTemplateFilename(filename), true)
  }
  for (const filename of ['report.pdf', 'macro.docm', 'macro.xlsm', 'report.docx.exe', '']) {
    assert.equal(isSupportedTemplateFilename(filename), false)
  }
})

test('template placeholders become nested required Action input schema', () => {
  const schema = templatePathsToSchema(['project.code', 'project.name', 'report_opinion', 'project.code', ''])
  assert.deepEqual(schema.required, ['project', 'report_opinion'])
  assert.deepEqual(schema.properties.project.required, ['code', 'name'])
  assert.equal(schema.properties.project.properties.code.description, '模板变量：project.code')
  assert.equal('type' in schema.properties.report_opinion, false)
  assert.equal(schema.additionalProperties, false)
})

test('template numeric path segments produce array schema without guessing leaf types', () => {
  const schema = templatePathsToSchema(['lines.0.amount', 'lines.2.approved'])
  assert.equal(schema.properties.lines.type, 'array')
  assert.equal(schema.properties.lines.minItems, 3)
  assert.equal(schema.properties.lines.items.type, 'object')
  assert.deepEqual(schema.properties.lines.items.required, ['amount', 'approved'])
  assert.equal(schema.properties.lines.items.properties.amount.description, '模板变量：lines.0.amount')
  assert.equal('type' in schema.properties.lines.items.properties.amount, false)
  assert.equal('type' in schema.properties.lines.items.properties.approved, false)
})

test('template paths reject prototype-chain segments without mutating Object.prototype', () => {
  assert.equal({}.polluted, undefined)
  for (const path of ['__proto__.polluted', 'safe.__proto__.polluted', 'constructor.prototype.polluted', 'SAFE.Constructor.value']) {
    assert.throws(() => templatePathsToSchema([path]), /无效或不安全/)
  }
  assert.equal({}.polluted, undefined)
})

test('managed template Action config cannot retain raw bucket file identifiers', () => {
  const migrated = cleanTemplateExecutorConfig({
    template_file_id: 'legacy-file',
    template_data_source_id: 'legacy-bucket',
    template_sha256: 'server-owned',
    template_variable_paths: ['forged'],
    target_data_source_id: 'output-bucket',
    output_filename: 'report',
  }, 'template-1', 4)
  assert.deepEqual(migrated, {
    template_id: 'template-1',
    template_version: 4,
    target_data_source_id: 'output-bucket',
    output_filename: 'report',
  })
  assert.deepEqual(cleanTemplateExecutorConfig(migrated, 'template-1', ''), {
    template_id: 'template-1',
    target_data_source_id: 'output-bucket',
    output_filename: 'report',
  })
})

test('template binding explains inactive and versionless resources', () => {
  const base = {
    id: 'template-1', key: 'report', name: '审计报告', status: 'active',
    version_count: 1, reference_count: 0, deletable: true, current_version_id: 'version-1',
  }
  assert.equal(templateUnavailableReason({ ...base, current_version: null }), '模板没有可用版本')
  assert.equal(templateUnavailableReason({ ...base, status: 'deprecated', current_version: null }), '模板已停用，不能建立新的操作绑定')
  assert.equal(templateUnavailableReason({ ...base, current_version: { id: 'version-1' } }), '')
})

test('shared templates never use a scenario-owned file bucket', () => {
  assert.equal(isTemplateBucketInScope(null, null), true)
  assert.equal(isTemplateBucketInScope('scenario-a', null), false)
  assert.equal(isTemplateBucketInScope(null, 'scenario-a'), true)
  assert.equal(isTemplateBucketInScope('scenario-a', 'scenario-a'), true)
  assert.equal(isTemplateBucketInScope('scenario-b', 'scenario-a'), false)
})
