import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  candidateApiFailure,
  candidateFailureDraftIds,
  candidatePromotionRequest,
  normalizeCandidateBlockers,
} from '../src/utils/candidateGovernance.ts'
import { normalizeScenarioModelDrafts, scenarioDraftStage } from '../src/utils/scenarioModelDrafts.ts'

function rawCandidate(overrides = {}) {
  return {
    id: 'candidate-1',
    scenario_id: 'scenario-1',
    proposal_id: 'proposal-1',
    task_id: 'task-1',
    resource_kind: 'action',
    resource_key: 'action.review',
    title: 'Review',
    payload: { name: 'Review' },
    validation_issues: [],
    issues_count: 0,
    blocking_issue_count: 0,
    draft_status: 'ready_for_review',
    enabled: false,
    publishable: false,
    materialization_source: 'assistant_compiler',
    source_origin: 'assistant',
    validation_status: 'valid',
    lifecycle_status: 'candidate',
    promotion_eligible: true,
    promotion_blockers: [],
    activation_status: 'inactive',
    quality_fingerprint: 'fingerprint-1',
    revision: 3,
    ...overrides,
  }
}

test('candidate normalization preserves server governance independently of provenance', () => {
  const [assistant, manual] = normalizeScenarioModelDrafts([
    rawCandidate(),
    rawCandidate({ id: 'candidate-2', source_origin: 'manual', materialization_source: 'manual' }),
  ])

  assert.equal(assistant.source_origin, 'assistant')
  assert.equal(manual.source_origin, 'manual')
  assert.equal(assistant.validation_status, manual.validation_status)
  assert.equal(assistant.promotion_eligible, true)
  assert.equal(manual.promotion_eligible, true)
  assert.equal(assistant.activation_status, 'inactive')
  assert.equal(manual.activation_status, 'inactive')
})

test('capability port candidates remain visible in the protocol-neutral review stage', () => {
  const [port] = normalizeScenarioModelDrafts([
    rawCandidate({ resource_kind: 'capability_port', resource_key: 'port.analyze' }),
  ])
  assert.equal(port.resource_kind, 'capability_port')
  assert.equal(scenarioDraftStage(port.resource_kind), 'candidates')
})

test('structured blockers retain candidate targets, field paths and recovery hints', () => {
  const blockers = normalizeCandidateBlockers([{
    code: 'missing_reference',
    message: 'Referenced definition is missing.',
    draft_ids: ['candidate-1', 'candidate-1'],
    resource_keys: ['action.review'],
    field_path: ['payload', 'entity_ref'],
    resolution_hint: 'Create or select the referenced definition.',
  }])

  assert.deepEqual(blockers[0].draft_ids, ['candidate-1'])
  assert.deepEqual(blockers[0].field_path, ['payload', 'entity_ref'])
  assert.equal(blockers[0].resolution_hint, 'Create or select the referenced definition.')
})

test('batch promotion payload pins every selected server revision and removes duplicates', () => {
  const candidates = normalizeScenarioModelDrafts([
    rawCandidate(),
    rawCandidate({ id: 'candidate-2', revision: 8, source_origin: 'manual' }),
  ])
  assert.deepEqual(candidatePromotionRequest(candidates, ['candidate-2', 'candidate-1', 'candidate-2']), {
    items: [
      { draft_id: 'candidate-2', expected_revision: 8 },
      { draft_id: 'candidate-1', expected_revision: 3 },
    ],
  })
  assert.throws(() => candidatePromotionRequest(candidates, ['stale-candidate']), /重新加载/)
  assert.throws(() => candidatePromotionRequest(candidates, []), /至少选择一个/)
})

test('atomic promotion failures expose structured blockers and focus targets', () => {
  const failure = candidateApiFailure({
    status: 409,
    detail: {
      code: 'candidate_promotion_blocked',
      message: 'The atomic batch was blocked.',
      blockers: [{
        code: 'invalid_candidate',
        message: 'Candidate validation failed.',
        draft_ids: ['candidate-2'],
      }],
    },
  })
  assert.equal(failure.code, 'candidate_promotion_blocked')
  assert.deepEqual(candidateFailureDraftIds(failure), ['candidate-2'])

  const serialized = candidateApiFailure({ message: JSON.stringify({
    code: 'candidate_revision_conflict',
    message: 'Revision changed.',
    blockers: [{ message: 'Reload candidate.', draft_ids: ['candidate-1'] }],
  }) })
  assert.equal(serialized.code, 'candidate_revision_conflict')
  assert.deepEqual(candidateFailureDraftIds(serialized), ['candidate-1'])
})

test('candidate review exposes every governed action without client-side activation', () => {
  const source = readFileSync(
    new URL('../src/components/CandidateReviewPanel.vue', import.meta.url),
    'utf8',
  )
  assert.match(source, /api\.revalidateScenarioModelCandidate/)
  assert.match(source, /api\.revalidateScenarioModelCandidates/)
  assert.match(source, /一键确定性校验/)
  assert.match(source, /api\.promoteScenarioModelCandidate\(/)
  assert.match(source, /api\.promoteScenarioModelCandidates/)
  assert.match(source, /candidate\.promotion_eligible !== true/)
  assert.match(source, /role="alert"[\s\S]*?tabindex="-1"/)
  assert.match(source, /errorSummary\.value\?\.focus\(\)/)
  assert.match(source, /result\.atomic !== true/)
  assert.doesNotMatch(source, /api\.[A-Za-z0-9_]*activate/)
})
