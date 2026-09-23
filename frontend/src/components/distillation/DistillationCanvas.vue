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
    <div :id="`${canvasId}-panel-${tab}`" class="discovery-canvas-content" :class="{ 'is-graph-tab': tab === 'entities' || tab === 'process' || tab === 'lineage' }" role="tabpanel" :aria-labelledby="`${canvasId}-tab-${tab}`" tabindex="0" :aria-busy="loading">
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
      <section v-if="activePublications.length" class="discovery-tab-publications" aria-label="当前分类已保存交付物">
        <div class="discovery-tab-publications-heading"><strong>{{ activeTabName }}交付物</strong><small>每个版本可独立删除</small></div>
        <article v-for="publication in activePublications" :key="publication.version.id" class="discovery-tab-publication-row">
          <span>版本 {{ publication.version.project_revision }}</span>
          <div class="discovery-tab-publication-actions">
            <el-button text :disabled="!publication.version.data_source_id" @click="$emit('open-publication', publication.version)">查看资料</el-button>
            <el-button v-for="artifact in publication.artifacts" :key="artifact.key" text :disabled="!!publicationBusy" @click="$emit('download-publication', publication.version, artifact)">下载 {{ artifact.filename }}</el-button>
            <el-button v-if="canEdit" type="danger" text :disabled="!!publicationBusy" @click="$emit('delete-publication', publication.version)">删除版本</el-button>
          </div>
        </article>
      </section>
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
import type { DistillationArtifact, DistillationDocument, DistillationProject, DistillationPublication } from '@/types/businessDistillation'
import DistillationFindings from './DistillationFindings.vue'
import DistillationEvidenceFindings from './DistillationEvidenceFindings.vue'
import DistillationCases from './DistillationCases.vue'

const props = withDefaults(defineProps<{ document: DistillationDocument; project?: DistillationProject; projectId?: string; revision?: number; dirty: boolean; canEdit: boolean; canPublish: boolean; embedded?: boolean; loading?: boolean; pending?: boolean; publications?: DistillationPublication[]; publicationBusy?: string }>(), {
  publications: () => [], publicationBusy: '',
})
defineEmits<{ close: []; publish: []; ask: [message: string]; updated: [project: DistillationProject]; 'open-publication': [publication: DistillationPublication]; 'download-publication': [publication: DistillationPublication, artifact: DistillationArtifact]; 'delete-publication': [publication: DistillationPublication] }>()
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
const artifactKeysByTab: Record<FindingTab, readonly string[]> = {
  value: ['brief', 'contract'],
  entities: ['er'],
  process: ['as_is', 'to_be'],
  lineage: ['lineage'],
  cases: ['brief', 'contract'],
  evidence: ['provenance', 'brief'],
  questions: ['brief'],
}
const canvasId = `distillation-${useId()}`
const tab = ref<FindingTab>('value')
const tabButtons = ref<HTMLButtonElement[]>([])
const activeTabName = computed(() => tabs.find(item => item.key === tab.value)?.name || '')
const activePublications = computed(() => props.publications.map(version => ({
  version,
  artifacts: version.artifacts.filter(artifact => artifactKeysByTab[tab.value].includes(artifact.key)),
})).filter(publication => publication.artifacts.length > 0))
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
