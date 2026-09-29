import assert from 'node:assert/strict'
import test from 'node:test'
import { compilationFailureSummary, decisionGatePresentation, modelTaskNeedsCorrection } from '../src/utils/assistantModelOutcome.ts'

test('provider failure is a retryable service problem, not completed analysis or missing business evidence', () => {
  const summary = compilationFailureSummary([{ code: 'COMPILER_PROVIDER_UNAVAILABLE', message: '模型连接失败' }])
  assert.match(summary, /未生成可用定义/)
  assert.doesNotMatch(summary, /已完成.*分析|补充信息后/)
  assert.equal(decisionGatePresentation({ mode: 'candidate_review', reason_codes: ['COMPILATION_UNAVAILABLE'] }).label, '恢复后重试')
})

test('business ambiguity keeps its evidence clarification flow', () => {
  assert.equal(compilationFailureSummary([{ code: 'missing_primary_key', message: '主键未声明' }]), null)
  assert.equal(decisionGatePresentation({ mode: 'clarify' }).label, '先对齐问题')
})

test('interrupted compilation does not ask the user to reinterpret business evidence', () => {
  const summary = compilationFailureSummary([{ code: 'COMPILER_EXECUTION_INTERRUPTED', message: '编译中断' }])
  assert.match(summary, /未生成可用定义/)
  assert.match(summary, /不是需要补充业务资料/)
})

test('candidate defects are not presented as business ambiguity or side-effect risk', () => {
  const presentation = decisionGatePresentation({ mode: 'candidate_review', reason_codes: ['CANDIDATE_VALIDATION_FAILED'] })
  assert.equal(presentation.label, '修正定义')
  assert.equal(presentation.title, '顾问生成的定义仍需修正')
  assert.equal(presentation.type, 'warning')
})


test('blocked tasks request correction while server-approved partial application remains available', () => {
  assert.equal(modelTaskNeedsCorrection({ status: 'blocked' }, { can_apply: false }), true)
  assert.equal(modelTaskNeedsCorrection({ status: 'blocked' }, null), true)
  assert.equal(modelTaskNeedsCorrection({ status: 'blocked' }, { can_apply: true }), false)
  assert.equal(modelTaskNeedsCorrection({ status: 'ready' }, { can_apply: true }), false)
})


test('changed scene context asks for candidate revalidation without inventing business ambiguity', () => {
  assert.deepEqual(decisionGatePresentation({ mode: 'candidate_review', reason_codes: ['SCENARIO_CONTEXT_CHANGED'] }), {
    title: '场景定义已变化', label: '重新校验候选', type: 'warning',
  })
})
