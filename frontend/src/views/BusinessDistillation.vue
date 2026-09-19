<template>
  <div class="discovery-workspace distillation-studio" :class="{ 'with-projects': projectsOpen }">
    <aside class="discovery-sidebar" aria-label="蒸馏项目与资料">
      <div class="discovery-sidebar-heading"><strong>业务蒸馏</strong><el-button text circle aria-label="收起项目列表" @click="projectsOpen = false"><el-icon><Close /></el-icon></el-button></div>
      <el-button class="discovery-new" :disabled="!canCreate" @click="newConversation"><el-icon><Plus /></el-icon>新建对话</el-button>
      <div class="discovery-sidebar-label"><span>{{ historyScope === 'shared' ? '未关联场景的对话' : '当前场景的对话' }}</span><el-button text circle :loading="listing" aria-label="刷新对话历史" @click="list"><el-icon><Refresh /></el-icon></el-button></div>
      <div class="discovery-project-list"><button v-for="row in projects" :key="row.id" :aria-current="row.id === projectId ? 'page' : undefined" @click="openProject(row.id)"><span>{{ row.name }}</span><small>{{ DECISION_LABELS[row.document.decision] }}</small></button><p v-if="!projects.length && !listing" class="discovery-muted">从第一段对话开始。</p></div>
      <div v-if="hasMore || offset" class="discovery-project-pages"><el-button text :disabled="!offset || listing" @click="offset -= 50">上一页</el-button><el-button text :disabled="!hasMore || listing" @click="offset += 50">下一页</el-button></div>
      <div class="discovery-sources"><button @click="openUnscopedProjects">未关联场景的项目</button></div>
    </aside>
    <section class="discovery-main" aria-label="业务蒸馏工作区">
      <header class="discovery-toolbar"><div class="discovery-toolbar-title"><el-button text circle :aria-expanded="projectsOpen" aria-label="打开对话历史" @click="projectsOpen = !projectsOpen"><el-icon><Menu /></el-icon></el-button><label class="discovery-visually-hidden" for="distillation-scenario">业务场景</label><el-select :key="scenarioPickerKey" id="distillation-scenario" :model-value="selectedScenario || undefined" filterable clearable placeholder="可选：关联业务场景" :disabled="actionBusy || attachmentBusy" @change="changeScenario" @clear="showUnscopedProjects"><el-option v-for="scenario in scenarios" :key="scenario.id" :value="scenario.id || ''" :label="scenario.name" /></el-select><span v-if="project" class="discovery-project-title">{{ project.name }}</span><small v-else-if="!selectedScenario" class="discovery-project-title">未关联场景</small><small v-if="project && !project.can_write">只读</small></div><div class="distill-actions"><el-button :disabled="!canEdit || actionBusy || !!active || loading" @click="openSystems">业务系统</el-button><el-button :disabled="!canEdit || actionBusy || !!active" @click="openSources('materials')">资料库</el-button><el-button v-if="publications.length" text @click="openLatestPublication">查看资料库</el-button><el-button v-if="project?.scenario_id && publications.length" text type="primary" @click="buildScenario">进入场景能力</el-button><el-button v-if="project && !project.scenario_id && canEdit" text @click="copyDialog = true">复制到场景</el-button></div></header>
      <div v-if="error || notice" class="discovery-notices"><el-alert v-if="error" :title="error" type="error" :closable="false" show-icon /><el-alert v-else-if="notice" :title="notice" type="success" closable @close="notice = ''" /><el-button v-if="error && projectId" text :disabled="actionBusy" @click="reloadSaved">重新加载已保存版本</el-button></div>
      <div v-if="projectId && !project && !loading" class="discovery-unavailable"><el-empty description="项目未加载或无权访问" /><el-button @click="load">重新加载</el-button></div>
      <div class="discovery-mobile-panels" role="group" aria-label="切换业务蒸馏工作区"><button :aria-pressed="mobilePane === 'findings'" @click="mobilePane = 'findings'">阶段结论</button><button :aria-pressed="mobilePane === 'conversation'" @click="mobilePane = 'conversation'">业务蒸馏 AI</button></div>
      <div v-if="!projectId || project || loading" class="distillation-studio-body" :class="`is-${mobilePane}`">
        <DistillationCanvas :key="draftKey" class="distillation-stage" embedded :document="artifactProposal?.proposal || draft.document" :pending="!!artifactProposal" :project="project || undefined" :project-id="projectId" :revision="project?.revision" :dirty="dirty" :loading="loading" :can-edit="canEdit && !loading && !actionBusy" :can-publish="!!project && canEdit && !dirty && !actionBusy && !active && !artifactProposal" @ask="discussFinding" @publish="confirmPublish" @updated="acceptProjectUpdate" />
        <aside class="distillation-advisor-panel" aria-label="业务蒸馏顾问对话">
          <DistillationConversation v-model="input" :scope-key="draftKey" :turns="turns" :loading="loading || conversationLoading" :has-more="conversationHasMore" :working="!!active" :sending="sending || busy === 'save'" :cancelling="cancelling" :applying="applying" :disabled="!canEdit || loading || actionBusy" :can-apply="canEdit && !dirty && !actionBusy" :error="conversationError" :blocked-reason="blockedReason" :upload-busy="attachmentBusy" :removing-attachment="removingAttachment" @send="sendMessage" @cancel="cancel" @reload="reconnectConversation" @older="loadConversation(true)" @sources="openSources('materials')" @files="addAttachments" @remove-submitted="removeSubmittedAttachment" @preview="previewTurn = $event" @apply="applyTurn"><template #attachments><DistillationAttachments :items="attachments" :error="attachmentError" :disabled="!!active || sending" @retry="retryAttachment" @remove="removeAttachment" @reload="loadAttachments" /></template></DistillationConversation>
        </aside>
      </div>
    </section>
    <button v-if="projectsOpen" class="discovery-sidebar-scrim" aria-label="关闭项目列表" @click="projectsOpen = false" />

    <el-drawer v-model="systemsOpen" title="业务系统" size="min(540px, 96vw)"><DistillationSystemAccess v-if="systemsOpen && project" :key="project.id" :project="project" :can-edit="canEdit && !actionBusy && !active" :dirty="dirty" @updated="acceptProjectUpdate" /></el-drawer>
    <DistillationPublishDecisionDialog v-model="publishDialog" :document="draft.document" :busy="actionBusy" :error="error" @confirm="publishDecision" />
    <el-drawer v-model="sourcesOpen" title="引用资料库" size="min(540px, 96vw)"><DistillationLibraryPicker v-model="draft.document" :materials="eligibleMaterials" :disabled="!canEdit || actionBusy || !!active" /><el-button text :disabled="actionBusy" @click="refreshOptions">刷新可用资料</el-button><template #footer><el-button @click="sourcesOpen = false">返回对话</el-button><el-button v-if="canEdit" type="primary" :disabled="actionBusy || !!active" :loading="busy === 'save'" @click="saveReferences">保存引用</el-button></template></el-drawer>
    <el-dialog v-model="copyDialog" title="复制对话项目到目标场景" width="min(520px, 94vw)"><el-form label-position="top"><el-form-item label="目标业务场景"><el-select v-model="copyScenarioId" filterable><el-option v-for="row in scenarios" :key="row.id" :label="row.name" :value="row.id || ''" /></el-select></el-form-item></el-form><template #footer><el-button @click="copyDialog = false">取消</el-button><el-button type="primary" :disabled="!copyScenarioId || actionBusy" @click="copyProject">创建场景对话项目</el-button></template></el-dialog>
    <el-dialog :model-value="!!previewTurn" title="阶段建议" width="min(860px, 94vw)" @update:model-value="(open: boolean) => { if (!open) previewTurn = null }"><div v-if="previewTurn?.proposal" class="distill-proposal"><el-alert title="这是 AI 根据当前资料与对话整理的阶段建议。确认采用后才会形成新的阶段结论；保存到资料库仍需单独确认。" type="info" :closable="false" /><DistillationSummary :document="previewTurn.proposal" /></div><template #footer><el-button @click="previewTurn = null">继续对话</el-button><el-button v-if="previewTurn && !previewTurn.applied_revision" type="primary" :disabled="!canEdit || dirty || actionBusy || !!active" @click="applyTurn(previewTurn)">确认采用</el-button></template></el-dialog>
  </div>
</template>
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { Close, Menu, Plus, Refresh } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { useBusinessDistillation } from '@/composables/useBusinessDistillation'
import { useDistillationConversation } from '@/composables/useDistillationConversation'
import { useDistillationAttachments } from '@/composables/useDistillationAttachments'
import { DECISION_LABELS, draftOf } from '@/utils/businessDistillation'
import { conversationTitle, latestArtifactProposal } from '@/utils/distillationConversation'
import type { DistillationProject, DistillationPublication } from '@/types/businessDistillation'
import type { DistillationResourceSelection, DistillationTurn } from '@/types/distillationConversation'
import DistillationConversation from '@/components/distillation/DistillationConversation.vue'
import DistillationCanvas from '@/components/distillation/DistillationCanvas.vue'
import DistillationSystemAccess from '@/components/distillation/DistillationSystemAccess.vue'
import DistillationLibraryPicker from '@/components/distillation/DistillationLibraryPicker.vue'
import DistillationAttachments from '@/components/distillation/DistillationAttachments.vue'
import DistillationSummary from '@/components/distillation/DistillationSummary.vue'
import DistillationPublishDecisionDialog from '@/components/distillation/DistillationPublishDecisionDialog.vue'
import type { handoffDecision } from '@/utils/distillationHandoff'
import '@/styles/distillation.css'
import '@/styles/distillation-workspace.css'
const route = useRoute(), router = useRouter(), auth = useAuthStore()
const projectId = computed(() => typeof route.params.id === 'string' ? route.params.id : '')
function routeScope() { return route.query.shared === '1' ? 'shared' : typeof route.query.scenario_id === 'string' ? route.query.scenario_id : 'shared' }
const historyScope = ref(routeScope())
const scenarioPickerKey = ref(0)
const selectedScenario = computed(() => historyScope.value === 'shared' ? '' : historyScope.value)
const draftKey = computed(() => projectId.value || `new:${historyScope.value}`)
const { projects, project, draft, scenarios, materials, publications, error, notice, loading, listing, busy, offset, hasMore, dirty, list, load, save, publish, refreshOptions, copyToScenario } = useBusinessDistillation(projectId, historyScope)
const { turns, input, error: conversationError, loading: conversationLoading, sending, applying, cancelling, hasMore: conversationHasMore, active, load: loadConversation, send, cancel, apply } = useDistillationConversation(projectId, draftKey)
const { attachments, error: attachmentError, busy: attachmentBusy, blocked: attachmentBlocked, readyIds, add: uploadAttachments, retry: retryAttachment, remove: removeAttachment, sent: attachmentsSent, load: loadAttachments, removeSubmitted } = useDistillationAttachments(projectId)
const artifactProposal = computed(() => project.value ? latestArtifactProposal(turns.value, project.value.id, project.value.revision) : undefined)
const canCreate = computed(() => auth.user?.workspace_role !== 'viewer')
const canEdit = computed(() => project.value ? project.value.can_write : !projectId.value && canCreate.value)
const removingAttachment = ref('')
const actionBusy = computed(() => !!busy.value || !!applying.value || sending.value || !!removingAttachment.value)
const eligibleMaterials = computed(() => materials.value.filter(source => !source.scenario_id || source.scenario_id === draft.value.scenario_id))
const blockedReason = computed(() => dirty.value ? '阶段结论或资料引用有未保存修改，请先保存后继续。' : attachmentBlocked.value ? '请等待临时附件就绪，或重试、移除未就绪的附件。' : '')
const projectsOpen = ref(false), sourcesOpen = ref(false), systemsOpen = ref(false)
const mobilePane = ref<'findings' | 'conversation'>('conversation')
const publishDialog = ref(false)
let publicationOwner: { id: string; revision: number } | undefined
const copyDialog = ref(false), copyScenarioId = ref(''), previewTurn = ref<DistillationTurn | null>(null)
let internalNavigation = false
watch(project, row => { if (row?.id === projectId.value) historyScope.value = row.scenario_id || 'shared' })
watch(() => [projectId.value, route.query.scenario_id, route.query.shared], () => {
  historyScope.value = projectId.value ? project.value?.id === projectId.value ? project.value.scenario_id || 'shared' : '' : routeScope()
  previewTurn.value = null; sourcesOpen.value = false; systemsOpen.value = false; publishDialog.value = false; publicationOwner = undefined
})
function closeNarrowHistory() { if (window.matchMedia('(max-width: 1060px)').matches) projectsOpen.value = false }
async function newConversation() {
  const query = selectedScenario.value ? { scenario_id: selectedScenario.value } : { shared: '1' }
  if (!await router.push({ name: 'business-distillation', query })) closeNarrowHistory()
}
async function changeScenario(id: string) {
  if (!id) return
  const failure = await router.push({ name: 'business-distillation', query: { scenario_id: id } })
  if (failure) { scenarioPickerKey.value += 1; await nextTick(); document.getElementById('distillation-scenario')?.focus() }
  else closeNarrowHistory()
}
async function openUnscopedProjects() { if (await router.push({ name: 'business-distillation', query: { shared: '1' } }) === undefined) projectsOpen.value = true }
async function showUnscopedProjects() {
  const failure = await router.push({ name: 'business-distillation', query: { shared: '1' } })
  if (failure) { scenarioPickerKey.value += 1; await nextTick(); document.getElementById('distillation-scenario')?.focus() }
}
async function openProject(id: string) { if (!await router.push(`/business-distillation/${id}`)) closeNarrowHistory() }
function openSources(_tab: string) { sourcesOpen.value = true; void refreshOptions() }
async function discussFinding(message: string) {
  if (!canEdit.value || loading.value || actionBusy.value) return
  input.value = input.value.trim() ? `${input.value}\n\n${message}` : message
  mobilePane.value = 'conversation'
  await nextTick()
  document.getElementById('distillation-message')?.focus()
}
async function saveDraft() {
  if (!draft.value.name.trim()) draft.value.name = conversationTitle(input.value || '新的业务探索')
  const row = await save()
  if (row && row.id !== projectId.value) {
    const text = input.value
    internalNavigation = true
    try { await router.replace({ name: 'business-distillation', params: { id: row.id } }); await nextTick(); input.value = text }
    finally { internalNavigation = false }
  }
  return row
}
async function ensureProject(title: string) {
  if (project.value) return project.value
  if (!canCreate.value) return null
  const previousName = draft.value.name
  draft.value.name = conversationTitle(title)
  const row = await saveDraft()
  if (!row) draft.value.name = previousName
  return row
}
async function openSystems() {
  if (!canEdit.value || actionBusy.value || active.value) return
  if (await ensureProject(input.value || '业务系统调查')) systemsOpen.value = true
}
async function addAttachments(files: File[]) {
  if (!files.length || actionBusy.value || attachmentBusy.value || active.value || !canEdit.value) return
  if (await ensureProject(input.value || files[0]?.name || '新的对话')) await uploadAttachments(files)
}
async function saveReferences() { if (await saveDraft()) sourcesOpen.value = false }
async function removeSubmittedAttachment(id: string) {
  if (actionBusy.value) return
  const ownerProject = projectId.value
  try { await ElMessageBox.confirm('移除后，本次对话将不能再读取该附件；正在使用它的调查也会停止。', '移除临时附件', { confirmButtonText: '移除', cancelButtonText: '保留', type: 'warning' }) } catch { return }
  if (projectId.value !== ownerProject) return
  removingAttachment.value = id
  try { if (await removeSubmitted(id)) await loadConversation() } finally { removingAttachment.value = '' }
}
async function sendMessage(selection: DistillationResourceSelection = {}) {
  const text = input.value
  if (!text.trim() || !canEdit.value || dirty.value || actionBusy.value || attachmentBlocked.value || active.value) return
  const row = await ensureProject(text), ids = [...readyIds.value]
  if (row && await send(text, row.revision, ids, selection)) { attachmentsSent(ids); notice.value = '' }
}
function acceptProjectUpdate(row: DistillationProject) {
  if (row.id !== projectId.value) return
  project.value = row; draft.value = draftOf(row); void list()
}
async function applyTurn(turn: DistillationTurn) {
  if (!project.value || dirty.value || actionBusy.value || !canEdit.value) return
  const row = await apply(turn, project.value.revision)
  if (row) { project.value = row; draft.value = draftOf(row); previewTurn.value = null; notice.value = '已采用 AI 提出的阶段建议。你可以查看各类结论，继续对话调整，或保存到资料库。'; void list() }
}
async function copyProject() { const row = await copyToScenario(copyScenarioId.value); if (row) { copyDialog.value = false; await router.push(`/business-distillation/${row.id}`) } }
function confirmPublish() {
  if (!project.value || dirty.value || actionBusy.value || active.value) return
  publicationOwner = { id: project.value.id, revision: project.value.revision }
  error.value = ''
  publishDialog.value = true
}
async function publishDecision(decision: ReturnType<typeof handoffDecision>) {
  if (actionBusy.value || active.value || !publicationOwner || project.value?.id !== publicationOwner.id || project.value?.revision !== publicationOwner.revision) return
  const owner = { ...publicationOwner }
  const document = draft.value.document
  const previous = { decision: document.decision, decision_reason: document.decision_reason }
  const submitted = { ...decision }
  Object.assign(document, submitted)
  if (dirty.value) {
    const row = await save()
    if (!row) {
      // Revert only this submission's transient fields; newer documents or human edits own their values.
      if (project.value?.id === owner.id && project.value?.revision === owner.revision
        && draft.value.document === document && document.decision === submitted.decision
        && document.decision_reason === submitted.decision_reason) Object.assign(document, previous)
      return
    }
    publicationOwner = { id: row.id, revision: row.revision }
  }
  const result = await publish()
  if (result) { publishDialog.value = false; publicationOwner = undefined }
}

function openMaterial(version: DistillationPublication) { void router.push({ name: 'data-sources', query: { source_id: version.data_source_id, return_to: route.fullPath } }) }
function openLatestPublication() { const latest = publications.value[0]; if (latest) openMaterial(latest) }
function buildScenario() {
  if (!project.value?.scenario_id) return
  void router.push({ name: 'scenario-detail', params: { id: project.value.scenario_id }, query: { stage: 'ontology', return_to: route.fullPath } })
}
async function allowLeave() {
  if (internalNavigation) return true
  if (actionBusy.value || attachmentBusy.value) { error.value = '正在处理操作，请等待完成后再切换。'; return false }
  if (!dirty.value && !input.value.trim() && !attachments.value.length) return true
  try { await ElMessageBox.confirm('还有未保存修改、未发送消息或临时附件。切换会放弃未保存的阶段结论；离开页面或刷新也会丢失未发送消息。确认继续？', '保留当前内容', { confirmButtonText: '切换', cancelButtonText: '继续编辑', type: 'warning' }); return true } catch { return false }
}
async function reloadSaved() { if (await allowLeave()) await load() }
async function reconnectConversation() { if (!dirty.value && !actionBusy.value) await load(); await Promise.all([loadConversation(), loadAttachments()]) }
onBeforeRouteLeave(allowLeave)
onBeforeRouteUpdate((to, from) => to.params.id === from.params.id && to.query.scenario_id === from.query.scenario_id && to.query.shared === from.query.shared ? true : allowLeave())
function beforeUnload(event: BeforeUnloadEvent) { if (dirty.value || actionBusy.value || attachmentBusy.value || input.value.trim() || attachments.value.length) { event.preventDefault(); event.returnValue = '' } }
onMounted(() => window.addEventListener('beforeunload', beforeUnload))
onBeforeUnmount(() => window.removeEventListener('beforeunload', beforeUnload))
</script>
