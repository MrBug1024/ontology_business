<template>
  <section class="discovery-canvas" :class="{ 'is-embedded': embedded }" aria-label="业务蒸馏结论">
    <header v-if="!embedded">
      <div><h2>业务蒸馏结论</h2><small>随对话逐步形成</small></div>
      <el-button text circle aria-label="收起业务蒸馏结论" @click="$emit('close')"><el-icon><Close /></el-icon></el-button>
    </header>
    <div class="discovery-canvas-tabs" role="tablist" aria-label="业务蒸馏结论分类">
      <button v-for="(item, index) in tabs" :id="`${canvasId}-tab-${item.key}`" :key="item.key" ref="tabButtons"
        role="tab" :aria-selected="tab === item.key" :aria-controls="`${canvasId}-panel-${item.key}`"
        :tabindex="tab === item.key ? 0 : -1" @click="tab = item.key" @keydown="navigateTabs($event, index)">
        {{ item.name }}<span v-if="item.key === 'questions' && document.open_questions.length" class="discovery-tab-count">{{ document.open_questions.length }}</span>
      </button>
    </div>
    <div :id="`${canvasId}-panel-${tab}`" class="discovery-canvas-content" role="tabpanel" :aria-labelledby="`${canvasId}-tab-${tab}`" tabindex="0" :aria-busy="loading">
      <p v-if="loading" class="discovery-muted" role="status">正在恢复阶段结论…</p>
      <template v-else>
        <DistillationEvidenceFindings v-if="tab === 'evidence'" :document="document" />
        <template v-else-if="tab === 'questions'">
          <ul v-if="document.open_questions.length" class="discovery-canvas-list"><li v-for="question in document.open_questions" :key="question">{{ question }}</li></ul>
          <p v-else class="discovery-empty-note">暂无待澄清问题</p>
        </template>
        <DistillationCases v-else-if="tab === 'cases'" :document="document" />
        <DistillationFindings v-else :document="document" :tab="tab" />
      </template>
    </div>
    <footer>
      <span class="discovery-canvas-state">{{ pending ? 'AI 产物 · 待采用' : dirty ? '资料引用有待保存的调整' : hasArtifacts ? '已保存的阶段产物' : '等待产物' }}</span>
      <el-button type="primary" plain :disabled="!canPublish || !hasArtifacts || pending" @click="$emit('publish')">保存到资料库</el-button>
    </footer>
  </section>
</template>
<script setup lang="ts">
import { computed, nextTick, ref, useId } from 'vue'
import { Close } from '@element-plus/icons-vue'
import type { DistillationDocument, DistillationProject } from '@/types/businessDistillation'
import DistillationFindings from './DistillationFindings.vue'
import DistillationEvidenceFindings from './DistillationEvidenceFindings.vue'
import DistillationCases from './DistillationCases.vue'

const props = defineProps<{ document: DistillationDocument; project?: DistillationProject; projectId?: string; revision?: number; dirty: boolean; canEdit: boolean; canPublish: boolean; embedded?: boolean; loading?: boolean; pending?: boolean }>()
defineEmits<{ close: []; publish: []; ask: [message: string]; updated: [project: DistillationProject] }>()
const hasArtifacts = computed(() => {
  const document = props.document
  return [document.beneficiary, document.pain, document.desired_outcome, document.success_metric, document.scope, document.non_goals].some(value => value.trim())
    || [document.entities, document.as_is.nodes, document.to_be.nodes, document.assertions, document.evidence, document.historical_cases, document.lineage, document.open_questions].some(items => items.length > 0)
})
const tabs = [
  { key: 'value', name: '业务价值' },
  { key: 'entities', name: 'ER' },
  { key: 'process', name: '流程' },
  { key: 'lineage', name: '血缘' },
  { key: 'cases', name: '历史案例' },
  { key: 'evidence', name: '证据' },
  { key: 'questions', name: '待澄清' },
] as const
type FindingTab = typeof tabs[number]['key']
const canvasId = `distillation-${useId()}`
const tab = ref<FindingTab>('value')
const tabButtons = ref<HTMLButtonElement[]>([])
async function navigateTabs(event: KeyboardEvent, index: number) {
  let nextIndex: number
  if (event.key === 'ArrowRight') nextIndex = (index + 1) % tabs.length
  else if (event.key === 'ArrowLeft') nextIndex = (index + tabs.length - 1) % tabs.length
  else if (event.key === 'Home') nextIndex = 0
  else if (event.key === 'End') nextIndex = tabs.length - 1
  else return
  event.preventDefault()
  const next = tabs[nextIndex]
  if (!next) return
  tab.value = next.key
  await nextTick()
  tabButtons.value[nextIndex]?.focus()
}
</script>
