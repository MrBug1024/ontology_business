import assert from 'node:assert/strict'
import test from 'node:test'

import { actionArtifactAttachment } from '../src/utils/artifactAttachments.ts'
import { groupScenarioModelIssues, scenarioModelIssueLabel } from '../src/utils/assistantProposalGroups.ts'
import { compilationRetryDraft, retryAttachmentsForMessage } from '../src/utils/assistantRetry.ts'
import {
  clearCompilationJobBookmark,
  compilationPollDelay,
  readCompilationJobBookmark,
  readPendingCompilationJobBookmark,
  saveCompilationJobBookmark,
  savePendingCompilationJobBookmark,
  selectCompilationJobForRecovery,
} from '../src/utils/assistantCompilationRecovery.ts'
import { buildRelationConstraints, relationConstraintForm } from '../src/utils/relationConstraints.ts'
import { safeInternalReturnPath } from '../src/utils/navigation.ts'
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
  assert.ok(compilationPollDelay(true) > compilationPollDelay(false))
  assert.ok(compilationPollDelay(false, 2) > compilationPollDelay(false, 0))
})

test('scenario compiler issues are grouped by severity and stable code without dropping details', () => {
  const issues = [
    { code: 'unknown_rule_field', message: '字段 A 不存在', blocking: true, source_refs: ['p0001', 'p0001'] },
    { code: ' UNKNOWN_RULE_FIELD ', message: '字段 B 不存在', source_refs: ['p0002'] },
    { code: 'missing_reference', message: '缺少操作引用' },
    { code: 'unknown_rule_field', message: '建议补充字段说明', blocking: false },
  ]

  const groups = groupScenarioModelIssues(issues)
  assert.deepEqual(groups.map((group) => [group.key, group.issues.length]), [
    ['blocking:unknown_rule_field', 2],
    ['blocking:missing_reference', 1],
    ['notice:unknown_rule_field', 1],
  ])
  assert.deepEqual(groups[0].issues[0].sourceRefs, ['p0001'])
  assert.equal(groups.flatMap((group) => group.issues).length, issues.length)
  assert.equal(scenarioModelIssueLabel('unknown_rule_field'), '规则字段未定义')
  assert.equal(scenarioModelIssueLabel('invalid_relation_constraints'), '关系约束格式不正确')
  assert.equal(scenarioModelIssueLabel('MAPPING_DEFERRED_NO_DATA_SOURCE'), '数据映射等待数据源')
  assert.equal(scenarioModelIssueLabel('future_code'), '其他预检问题')
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
