<template>
  <section class="business-context" :class="{ 'is-compact': compact }" aria-label="场景业务认知" :aria-busy="loading">
    <template v-if="compact">
      <div class="context-compact" role="status">
        <div class="context-compact-summary">
          <strong>场景业务认知</strong>
          <span v-if="loading">正在读取…</span>
          <span v-else-if="context" class="handoff-state">{{ handoffLabels[context.handoff.status] }}</span>
          <span v-else-if="error">{{ unauthorized ? '不可访问' : '读取失败' }}</span>
          <span v-else>暂未读取</span>
          <span v-if="context" class="context-compact-goal">{{ context.business.desired_outcome || '目标与验收标准待澄清。' }}</span>
        </div>
        <div class="context-actions">
          <button type="button" :disabled="loading || navigating" aria-label="刷新场景业务认知" @click="load">刷新</button>
          <button type="button" class="continue-button" :disabled="loading || navigating || !context?.construction.can_continue" @click="continueConstruction">{{ navigating ? '正在进入…' : '继续场景建设' }}</button>
        </div>
      </div>
      <p v-if="navigationError" role="alert">{{ navigationError }}</p>
    </template>
    <template v-else>
    <details>
      <summary><strong>场景业务认知</strong><span v-if="loading" role="status">正在读取…</span><span v-else-if="context" class="handoff-state">{{ handoffLabels[context.handoff.status] }}</span><span v-else>暂未读取</span></summary>
      <p v-if="loading" role="status">正在读取已采用的阶段结论与授权资料目录…</p>
      <p v-else-if="error" role="alert">{{ error }}</p>
      <div v-else-if="context" class="context-details">
        <p class="context-description">{{ context.revision === null ? '当前尚无已采用的阶段结论，可通过业务蒸馏对话逐步澄清。' : '蒸馏 AI 与智能业务顾问共享这些已采用的阶段结论。' }}</p>
        <dl class="business-fields"><div v-for="item in businessFields" :key="item.label"><dt>{{ item.label }}</dt><dd>{{ item.value || '待澄清' }}</dd></div></dl>
        <div class="construction-state" role="status"><b>{{ decisionLabels[context.business.decision] }}</b><p>{{ context.business.decision_reason || '建设理由尚待澄清。' }}</p><p>{{ context.construction.reason }}</p><p v-if="context.handoff.publication_revision !== null">已交接版本 {{ context.handoff.publication_revision }}</p></div>
        <details v-if="context.business.open_questions.length"><summary>待澄清问题 · {{ context.business.open_questions.length }}</summary><ul><li v-for="question in context.business.open_questions" :key="question">{{ question }}</li></ul></details>
        <details v-if="context.processes.to_be.total_nodes || context.processes.as_is.total_nodes"><summary>如何完成业务 · 目标流程 {{ context.processes.to_be.total_nodes }} 步</summary><div v-for="flow in flows" :key="flow.label"><h3>{{ flow.label }}</h3><ol><li v-for="node in flow.value.nodes" :key="node.key"><b>{{ node.name }}</b><p>{{ node.owner ? `负责：${node.owner}。` : '' }}{{ node.outcome }}</p><p v-if="node.rule">规则：{{ node.rule }}</p><p v-if="node.exceptions">例外：{{ node.exceptions }}</p></li></ol><p v-if="flow.value.has_more">还有更多流程步骤，可在业务蒸馏中查看。</p></div></details>
        <details v-if="context.historical_cases.total_count"><summary>历史业务案例 · {{ context.historical_cases.total_count }}</summary><ul><li v-for="item in context.historical_cases.items" :key="item.key"><b>{{ item.title }}</b><p>{{ item.result_summary }}</p><p v-if="item.limitations">适用限制：{{ item.limitations }}</p></li></ul><p v-if="context.historical_cases.has_more">更多案例可在业务蒸馏中查看。</p></details>
        <details><summary>AI 可查阅的资料 · {{ context.materials.sources.length }}{{ context.materials.has_more ? '+' : '' }}</summary><p>这是授权资料目录；AI 按需读取正文，用于理解业务与建设能力。</p><ul v-if="context.materials.sources.length" class="materials"><li v-for="source in context.materials.sources" :key="source.data_source_id"><button type="button" @click="openMaterials(source.data_source_id)">{{ source.name }}</button><span>{{ source.scope === 'shared' ? '共享资料' : '场景资料' }}</span></li></ul><p v-else>暂无可访问资料；也可以通过对话明确无需数据的能力。</p><button v-if="context.materials.has_more" type="button" @click="openMaterials()">查看全部场景资料</button></details>
        <ul class="context-boundaries"><li v-for="boundary in context.boundaries" :key="boundary">{{ boundary }}</li></ul>
      </div>
    </details>
    <footer>
      <span v-if="context" class="context-goal">{{ context.business.desired_outcome || '从业务目标、边界与成功标准开始聊透。' }}</span><span v-else-if="error" role="alert">{{ unauthorized ? '业务认知不可访问' : '业务认知读取失败' }}</span>
      <div class="context-actions"><button type="button" :disabled="loading || navigating" aria-label="刷新场景业务认知" @click="load">刷新</button><button v-if="context && !context.construction.can_continue" type="button" :disabled="navigating" @click="openDistillation">回到业务蒸馏</button><button type="button" class="continue-button" :disabled="loading || navigating || !context?.construction.can_continue" @click="continueConstruction">{{ navigating ? '正在进入…' : '继续场景建设' }}</button></div>
    </footer>
    <p v-if="navigationError" role="alert">{{ navigationError }}</p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, toRef } from 'vue'
import { isNavigationFailure, NavigationFailureType, useRoute, useRouter, type LocationQueryRaw } from 'vue-router'
import { useScenarioDiscoveryContext } from '@/composables/useScenarioDiscoveryContext'
import { openScenarioModelingAdvisor } from '@/utils/scenarioAdvisorEvents'

const props = withDefaults(defineProps<{ scenarioId: string; compact?: boolean }>(), { compact: false })
const { context, loading, error, unauthorized, load } = useScenarioDiscoveryContext(toRef(props, 'scenarioId'))
const route = useRoute(), router = useRouter()
const navigating = ref(false), navigationError = ref('')
let disposed = false
const handoffLabels = { missing: '尚未交接建设资料', current: '建设交接已同步', stale: '业务认知已更新，待重新交接' }
const decisionLabels = { undecided: '建设方向待明确', continue: '继续建设', adjust: '调整方向后建设', stop: '暂缓建设' }
const businessFields = computed(() => context.value ? [
  { label: '服务谁', value: context.value.business.beneficiary }, { label: '为什么做', value: context.value.business.pain },
  { label: '最终结果', value: context.value.business.desired_outcome }, { label: '如何验收', value: context.value.business.success_metric },
  { label: '做什么', value: context.value.business.scope }, { label: '不做什么', value: context.value.business.non_goals },
] : [])
const flows = computed(() => context.value ? [{ label: '目标流程', value: context.value.processes.to_be }, { label: '现状流程', value: context.value.processes.as_is }].filter(flow => flow.value.total_nodes) : [])

function openDistillation() { void router.push({ name: 'scenario-detail', params: { id: props.scenarioId }, query: { ...route.query, stage: 'distillation' } }) }
function openMaterials(sourceId?: string) {
  const query: LocationQueryRaw = { ...route.query, stage: 'materials', source_id: sourceId }
  delete query.materials_offset
  void router.push({ name: 'scenario-detail', params: { id: props.scenarioId }, query })
}
async function continueConstruction() {
  if (navigating.value || loading.value || !context.value?.construction.can_continue) return
  const id = props.scenarioId
  navigating.value = true
  navigationError.value = ''
  try {
    const result = await router.push({ name: 'scenario-detail', params: { id }, query: { ...route.query, stage: 'ontology' } })
    await nextTick()
    if ((result && !isNavigationFailure(result, NavigationFailureType.duplicated)) || disposed || props.scenarioId !== id || route.params.id !== id || route.query.stage !== 'ontology') return
    openScenarioModelingAdvisor({ scenario_id: id, prompt: '请结合当前场景已采用的业务目标、范围边界、成功标准和已交接资料，说明能力建设计划与待澄清问题。' })
  } catch (caught: unknown) { if (!disposed && props.scenarioId === id) navigationError.value = caught instanceof Error ? caught.message : '未能进入能力建设，请重试。' }
  finally { if (!disposed) navigating.value = false }
}
onBeforeUnmount(() => { disposed = true })
</script>

<style scoped>
.business-context { margin: 0 0 18px; padding: 0 16px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface); min-width: 0; }
.business-context.is-compact { padding: 0 12px; }
.context-compact { display: flex; align-items: center; gap: 12px; min-height: 56px; }
.context-compact-summary { display: flex; align-items: baseline; flex-wrap: wrap; gap: 8px; min-width: 0; flex: 1; font-size: 12px; color: var(--text-2); }
.context-compact-summary strong { color: var(--text); font-size: 13px; }
.context-compact-goal { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
summary { cursor: pointer; min-height: 44px; padding: 12px 0; font-size: 13px; line-height: 1.6; }
summary span { margin-left: 12px; color: var(--text-2); font-size: 12px; }
.context-details { padding-bottom: 12px; }
p, li, dd { color: var(--text-2); font-size: 13px; line-height: 1.7; overflow-wrap: anywhere; }
.business-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px 24px; margin: 16px 0; }
dt { font-size: 12px; color: var(--text-2); }
dd { margin: 5px 0 0; color: var(--text); white-space: pre-wrap; }
h3 { font-size: 13px; font-weight: 600; }
.construction-state { padding: 12px; background: var(--surface-2); border-radius: 6px; font-size: 13px; }
.construction-state p { margin: 6px 0 0; }
ul, ol { padding-left: 20px; }
.materials { padding: 0; list-style: none; }
.materials li { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.materials span { font-size: 12px; flex-shrink: 0; }
.context-boundaries { font-size: 12px; }
footer { border-top: 1px solid var(--border); display: flex; align-items: center; gap: 12px; padding: 10px 0; }
.context-goal { flex: 1; min-width: 0; font-size: 12px; line-height: 1.6; color: var(--text-2); overflow-wrap: anywhere; }
.context-actions { display: flex; flex-shrink: 0; gap: 8px; flex-wrap: wrap; }
button { min-height: 44px; border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; background: var(--surface); color: var(--text); font: inherit; font-size: 12px; cursor: pointer; }
.continue-button { color: var(--primary); background: var(--primary-soft); }
button:disabled { cursor: not-allowed; opacity: .6; }
button:focus-visible, summary:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
@media (max-width: 650px) { .business-fields { grid-template-columns: minmax(0, 1fr); } footer { align-items: flex-start; flex-direction: column; } .context-actions { width: 100%; } .materials li { align-items: flex-start; flex-direction: column; gap: 0; } }
@media (max-width: 650px) { .context-compact { align-items: flex-start; flex-direction: column; padding: 10px 0; } .context-compact-summary { width: 100%; } .context-compact-goal { white-space: normal; } .is-compact .context-actions { width: auto; } }
</style>
