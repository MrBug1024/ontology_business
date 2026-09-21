<template>
  <div class="discovery-workspace distillation-studio" :class="{ 'with-projects': projectsOpen && !embedded, 'is-embedded': embedded }">
    <aside v-if="!embedded" class="discovery-sidebar" aria-label="蒸馏会话">
      <div class="discovery-sidebar-heading">
        <strong>{{ embedded ? '蒸馏会话' : '业务蒸馏' }}</strong>
        <el-button text circle aria-label="收起会话列表" @click="projectsOpen = false"><el-icon><Close /></el-icon></el-button>
      </div>
      <el-button class="discovery-new" :disabled="!canCreate" @click="newConversation"><el-icon><Plus /></el-icon>新建会话</el-button>
      <div class="discovery-sidebar-label">
        <span>{{ embedded ? '当前场景' : historyScope === 'shared' ? '未关联场景' : '当前场景' }}</span>
        <el-button text circle :loading="listing" aria-label="刷新会话" @click="list"><el-icon><Refresh /></el-icon></el-button>
      </div>
      <div class="discovery-project-list">
        <button v-for="row in projects" :key="row.id" type="button" :aria-current="row.id === projectId ? 'page' : undefined" @click="openProject(row.id)">
          <span>{{ row.name }}</span><small>{{ DECISION_LABELS[row.document.decision] }}</small>
        </button>
        <p v-if="!projects.length && !listing" class="discovery-muted">暂无会话</p>
      </div>
      <div v-if="hasMore || offset" class="discovery-project-pages">
        <el-button text :disabled="!offset || listing" @click="offset -= 50">上一页</el-button>
        <el-button text :disabled="!hasMore || listing" @click="offset += 50">下一页</el-button>
      </div>
      <div v-if="!embedded" class="discovery-sources"><button type="button" @click="openUnscopedProjects">未关联场景</button></div>
    </aside>

    <section class="discovery-main" aria-label="业务蒸馏工作区">
      <header v-if="!embedded" class="discovery-toolbar">
        <div class="discovery-toolbar-title">
          <el-button text circle :aria-expanded="projectsOpen" aria-label="打开会话列表" @click="projectsOpen = !projectsOpen"><el-icon><Menu /></el-icon></el-button>
          <template v-if="!embedded">
            <label class="discovery-visually-hidden" for="distillation-scenario">业务场景</label>
            <el-select :key="scenarioPickerKey" id="distillation-scenario" :model-value="selectedScenario || undefined" filterable clearable placeholder="选择场景" :disabled="actionBusy || attachmentBusy" @change="changeScenario" @clear="showUnscopedProjects">
              <el-option v-for="scenario in scenarios" :key="scenario.id" :value="scenario.id || ''" :label="scenario.name" />
            </el-select>
          </template>
          <span v-if="project" class="discovery-project-title">{{ project.name }}</span>
          <small v-else class="discovery-project-title">新会话</small>
          <small v-if="project && !project.can_write">只读</small>
        </div>
        <div class="distill-actions">
          <el-button :disabled="(!canEdit && !project) || actionBusy || !!active || loading" @click="openSources">引用资料</el-button>
          <el-button v-if="publications.length" text @click="openLatestPublication">查看产物</el-button>
          <el-button v-if="!embedded && project?.scenario_id && publications.length" text type="primary" @click="buildScenario">进入场景</el-button>
          <el-button v-if="!embedded && project && !project.scenario_id && canEdit" text @click="copyDialog = true">复制到场景</el-button>
        </div>
      </header>
      <div v-if="error || notice" class="discovery-notices">
        <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
        <el-alert v-else-if="notice" :title="notice" type="success" closable @close="notice = ''" />
        <el-button v-if="error && projectId" text :disabled="actionBusy" @click="reloadSaved">重新加载</el-button>
      </div>
      <div v-if="projectId && !project && !loading" class="discovery-unavailable">
        <el-empty description="会话不可用" /><el-button @click="load">重新加载</el-button>
      </div>
      <div class="discovery-mobile-panels" role="group" aria-label="切换业务蒸馏工作区">
        <button type="button" :aria-pressed="mobilePane === 'findings'" @click="mobilePane = 'findings'">阶段结论</button>
        <button type="button" :aria-pressed="mobilePane === 'conversation'" @click="mobilePane = 'conversation'">业务蒸馏 AI</button>
      </div>
      <div v-if="!projectId || project || loading" class="distillation-studio-body" :class="`is-${mobilePane}`">
        <DistillationCanvas :key="draftKey" class="distillation-stage" embedded :document="artifactProposal?.proposal || draft.document" :publications="publications" :publication-busy="busy" :pending="!!artifactProposal" :project="project || undefined" :project-id="projectId" :revision="project?.revision" :dirty="dirty" :loading="loading" :can-edit="canEdit && !loading && !actionBusy" :can-publish="!!project && canEdit && !dirty && !actionBusy && !active && !artifactProposal" @ask="discussFinding" @publish="confirmPublish" @updated="acceptProjectUpdate" @open-publication="openMaterial" @download-publication="downloadPublication" @delete-publication="deletePublication" />
        <aside class="distillation-advisor-panel" aria-label="业务蒸馏顾问对话">
        <DistillationConversation v-model="input" :scope-key="draftKey" :scenario-id="props.scenarioId" :turns="turns" :loading="loading || conversationLoading" :has-more="conversationHasMore" :working="!!active" :sending="sending || busy === 'save'" :cancelling="cancelling" :applying="applying" :disabled="!canEdit || loading || actionBusy" :can-apply="canEdit && !dirty && !actionBusy" :error="conversationError" :blocked-reason="blockedReason" :upload-busy="attachmentBusy" :removing-attachment="removingAttachment" :compact="embedded" :workspace-actions="embedded" :streaming="streaming" :reconnecting="reconnecting" @send="sendMessage" @cancel="cancel" @reload="reconnectConversation" @older="loadConversation(true)" @sources="openSources" @files="addAttachments" @remove-submitted="removeSubmittedAttachment" @preview="previewTurn = $event" @apply="applyTurn" @new="newConversation" @history="projectsOpen = true" @systems="openSystems">
            <template #attachments><DistillationAttachments :items="attachments" :error="attachmentError" :disabled="!canEdit || !!active || sending" @retry="retryAttachment" @remove="removeAttachment" @reload="loadAttachments" /></template>
          </DistillationConversation>
        </aside>
      </div>
    </section>
    <button v-if="projectsOpen && !embedded" class="discovery-sidebar-scrim" type="button" aria-label="关闭会话列表" @click="projectsOpen = false" />

    <el-drawer v-if="embedded" v-model="projectsOpen" title="会话记录" size="min(420px, 96vw)" append-to-body>
      <div class="discovery-project-list is-drawer">
        <article v-for="row in projects" :key="row.id" :class="{ 'is-active': row.id === projectId }">
          <button type="button" :aria-current="row.id === projectId ? 'page' : undefined" @click="openProject(row.id)">
            <span>{{ row.name }}</span><small>{{ DECISION_LABELS[row.document.decision] }}</small>
          </button>
          <el-button text circle :disabled="actionBusy || !props.canWrite" :aria-label="`删除会话 ${row.name}`" title="删除会话" @click="deleteProject(row)"><el-icon><Delete /></el-icon></el-button>
        </article>
        <p v-if="!projects.length && !listing" class="discovery-muted">暂无会话</p>
      </div>
      <div v-if="hasMore || offset" class="discovery-project-pages">
        <el-button text :disabled="!offset || listing" @click="offset -= 50">上一页</el-button>
        <el-button text :disabled="!hasMore || listing" @click="offset += 50">下一页</el-button>
      </div>
      <p class="discovery-conversation-delete-note">删除会话只清理对话与临时输入；已保存的场景阶段结论和发布产物保留。</p>
      <template #footer>
        <el-button type="primary" :disabled="!canCreate" @click="newConversation">新建会话</el-button>
      </template>
    </el-drawer>

    <el-drawer v-model="systemsOpen" title="业务系统" size="min(540px, 96vw)"><DistillationSystemAccess v-if="systemsOpen && project" :key="project.id" :project="project" :can-edit="canEdit && !actionBusy && !active" :dirty="dirty" @updated="acceptProjectUpdate" /></el-drawer>
    <DistillationPublishDecisionDialog v-model="publishDialog" :document="draft.document" :busy="actionBusy" :error="error" @confirm="publishDecision" />
    <el-drawer v-model="sourcesOpen" title="引用场景资料" size="min(540px, 96vw)">
      <DistillationLibraryPicker
        v-model="draft.document"
        :materials="eligibleMaterials"
        :disabled="!canEdit || actionBusy || !!active"
        :loading="materialLoading"
        :offset="materialOffset"
        :page-size="materialPageSize"
        :has-more="materialHasMore"
        @previous="previousMaterialPage"
        @next="nextMaterialPage"
      />
      <el-button text :disabled="actionBusy" @click="refreshOptions">刷新</el-button>
      <template #footer><el-button @click="sourcesOpen = false">取消</el-button><el-button v-if="canEdit" type="primary" :disabled="actionBusy || !!active" :loading="busy === 'save'" @click="saveReferences">保存引用</el-button></template>
    </el-drawer>
    <el-dialog v-if="!embedded" v-model="copyDialog" title="复制到场景" width="min(520px, 94vw)">
      <el-form label-position="top"><el-form-item label="目标场景"><el-select v-model="copyScenarioId" filterable><el-option v-for="row in scenarios" :key="row.id" :label="row.name" :value="row.id || ''" /></el-select></el-form-item></el-form>
      <template #footer><el-button @click="copyDialog = false">取消</el-button><el-button type="primary" :disabled="!copyScenarioId || actionBusy" @click="copyProject">创建会话</el-button></template>
    </el-dialog>
    <el-dialog :model-value="!!previewTurn" title="阶段建议" width="min(860px, 94vw)" @update:model-value="(open: boolean) => { if (!open) previewTurn = null }">
      <div v-if="previewTurn?.proposal" class="distill-proposal"><DistillationSummary :document="previewTurn.proposal" /></div>
      <template #footer><el-button @click="previewTurn = null">关闭</el-button><el-button v-if="previewTurn && !previewTurn.applied_revision" type="primary" :disabled="!canEdit || dirty || actionBusy || !!active" @click="applyTurn(previewTurn)">确认采用</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter, type LocationQueryRaw } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { Close, Delete, Menu, Plus, Refresh } from '@element-plus/icons-vue'
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

const props = withDefaults(defineProps<{ scenarioId?: string; canWrite?: boolean; embedded?: boolean }>(), {
  scenarioId: '', canWrite: false, embedded: false,
})
const route = useRoute(), router = useRouter(), auth = useAuthStore()
function queryValue(value: unknown) { return Array.isArray(value) ? String(value[0] || '') : typeof value === 'string' ? value : '' }
const projectId = computed(() => props.embedded ? queryValue(route.query.distillation_id) : queryValue(route.params.id))
function routeScope() { return props.scenarioId || (route.query.shared === '1' ? 'shared' : queryValue(route.query.scenario_id) || 'shared') }
const historyScope = ref(routeScope())
const selectedScenario = computed(() => historyScope.value === 'shared' ? '' : historyScope.value)
const draftKey = computed(() => projectId.value || `new:${historyScope.value}`)
const {
  projects, project, draft, scenarios, materials, publications, error, notice, loading, listing, busy,
  offset, hasMore, dirty, materialOffset, materialHasMore, materialLoading, materialPageSize,
  list, load, save, publish, download, refreshOptions, previousMaterialPage, nextMaterialPage, copyToScenario, remove, removePublication,
} = useBusinessDistillation(projectId, historyScope, props.embedded)
const authorizedProjectId = computed(() => {
  const row = project.value
  if (!row || row.id !== projectId.value) return ''
  if (props.embedded && row.scenario_id !== props.scenarioId) return ''
  return row.id
})
const { turns, input, error: conversationError, loading: conversationLoading, sending, applying, cancelling, hasMore: conversationHasMore, streaming, reconnecting, active, load: loadConversation, send, cancel, apply } = useDistillationConversation(authorizedProjectId, draftKey)
const { attachments, error: attachmentError, busy: attachmentBusy, blocked: attachmentBlocked, readyIds, add: uploadAttachments, retry: retryAttachment, remove: removeAttachment, sent: attachmentsSent, load: loadAttachments, removeSubmitted } = useDistillationAttachments(authorizedProjectId)
const artifactProposal = computed(() => project.value ? latestArtifactProposal(turns.value, project.value.id, project.value.revision) : undefined)
const canCreate = computed(() => props.embedded ? props.canWrite : auth.user?.workspace_role !== 'viewer')
const canEdit = computed(() => project.value ? project.value.can_write : !projectId.value && canCreate.value)
const removingAttachment = ref('')
const actionBusy = computed(() => !!busy.value || !!applying.value || sending.value || !!removingAttachment.value)
const eligibleMaterials = computed(() => materials.value.filter(source => !source.scenario_id || source.scenario_id === historyScope.value))
const blockedReason = computed(() => dirty.value ? '请先保存阶段结论或资料引用。' : attachmentBlocked.value ? '请等待附件就绪，或重试、移除未就绪附件。' : '')
const projectsOpen = ref(false), sourcesOpen = ref(false), systemsOpen = ref(false)
const mobilePane = ref<'findings' | 'conversation'>('conversation')
const publishDialog = ref(false)
let publicationOwner: { id: string; revision: number } | undefined
const scenarioPickerKey = ref(0), copyDialog = ref(false), copyScenarioId = ref(''), previewTurn = ref<DistillationTurn | null>(null)
let internalNavigation = false
const hasPendingWork = computed(() => dirty.value || actionBusy.value || attachmentBusy.value || !!input.value.trim() || attachments.value.some(item => item.status !== 'bound'))

function scenarioLocation(scenarioId: string, id = '') {
  const query: LocationQueryRaw = { ...route.query, stage: 'distillation' }
  delete query.shared; delete query.scenario_id; delete query.source_id; delete query.library_tab; delete query.materials_offset
  if (id) query.distillation_id = id
  else delete query.distillation_id
  return { name: 'scenario-detail', params: { id: scenarioId }, query }
}
function legacyLocation(id = '') {
  return { name: 'business-distillation', params: id ? { id } : {}, query: id ? {} : { shared: '1' } }
}
function closeNarrowHistory() { if (window.matchMedia('(max-width: 1060px)').matches) projectsOpen.value = false }
async function changeWorkspace(location: ReturnType<typeof scenarioLocation> | ReturnType<typeof legacyLocation>, replace = false) {
  internalNavigation = true
  try { await (replace ? router.replace(location) : router.push(location)); await nextTick(); closeNarrowHistory(); return true }
  finally { internalNavigation = false }
}
async function newConversation() {
  if (!await allowLeave()) return
  if (!projectId.value) { input.value = ''; await load(); closeNarrowHistory(); return }
  await changeWorkspace(props.embedded ? scenarioLocation(props.scenarioId) : legacyLocation())
}
async function changeScenario(id: string) {
  if (!id || !await allowLeave()) return
  await changeWorkspace(scenarioLocation(id))
}
async function openUnscopedProjects() { if (await allowLeave()) await changeWorkspace(legacyLocation()) }
async function showUnscopedProjects() {
  if (!await allowLeave()) { scenarioPickerKey.value += 1; await nextTick(); document.getElementById('distillation-scenario')?.focus(); return }
  await changeWorkspace(legacyLocation())
}
async function openProject(id: string) {
  if (id === projectId.value || !await allowLeave()) return
  await changeWorkspace(props.embedded ? scenarioLocation(props.scenarioId, id) : legacyLocation(id))
}

async function deleteProject(row: DistillationProject) {
  if (actionBusy.value || !props.canWrite) return
  try {
    await ElMessageBox.confirm(
      `删除会话“${row.name}”？对话记录和临时附件会清理；场景阶段结论与已发布产物保留。`,
      '删除会话',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch { return }
  if (await remove(row.id)) {
    if (row.id === projectId.value) await changeWorkspace(scenarioLocation(props.scenarioId), true)
    else void list()
  }
}
function openSources() {
  if ((!canEdit.value && !project.value) || actionBusy.value || active.value || loading.value) return
  sourcesOpen.value = true
  void refreshOptions()
}
async function discussFinding(message: string) {
  if (!canEdit.value || loading.value || actionBusy.value) return
  input.value = input.value.trim() ? `${input.value}\n\n${message}` : message
  mobilePane.value = 'conversation'
  await nextTick()
  document.getElementById('distillation-message')?.focus()
}
async function saveDraft(navigate = true) {
  if (!draft.value.name.trim()) draft.value.name = conversationTitle(input.value || '新的业务探索')
  const row = await save()
  if (navigate && row && row.id !== projectId.value) {
    const text = input.value
    await changeWorkspace(props.embedded ? scenarioLocation(props.scenarioId, row.id) : legacyLocation(row.id), true)
    input.value = text
  }
  return row
}
async function ensureProject(title: string, navigate = true) {
  if (project.value) return project.value
  if (!canCreate.value) return null
  const previousName = draft.value.name
  draft.value.name = conversationTitle(title)
  const row = await saveDraft(navigate)
  if (!row) draft.value.name = previousName
  return row
}
async function openSystems() {
  if ((!canEdit.value && !project.value) || actionBusy.value || active.value || loading.value) return
  if (project.value || await ensureProject(input.value || '业务系统调查')) systemsOpen.value = true
}
async function addAttachments(files: File[]) { if (files.length && !actionBusy.value && !attachmentBusy.value && !active.value && canEdit.value && await ensureProject(input.value || files[0]?.name || '新的会话')) await uploadAttachments(files) }
async function saveReferences() { if (await saveDraft()) sourcesOpen.value = false }
async function removeSubmittedAttachment(id: string) {
  if (!canEdit.value || actionBusy.value) return
  const ownerProject = projectId.value
  try { await ElMessageBox.confirm('移除此附件？正在使用它的调查也会停止。', '移除临时附件', { confirmButtonText: '移除', cancelButtonText: '保留', type: 'warning' }) } catch { return }
  if (projectId.value !== ownerProject) return
  removingAttachment.value = id
  try { if (await removeSubmitted(id)) await loadConversation() } finally { removingAttachment.value = '' }
}
async function sendMessage(selection: DistillationResourceSelection = {}) {
  const text = input.value
  if (!text.trim() || !canEdit.value || dirty.value || actionBusy.value || attachmentBlocked.value || active.value) return
  const existingProject = project.value
  const row = existingProject || await ensureProject(text, false), ids = [...readyIds.value]
  const sent = row ? await send(text, row.revision, ids, selection, row.id) : false
  if (sent) { attachmentsSent(ids); notice.value = '' }
  if (!existingProject && row) {
    await changeWorkspace(props.embedded ? scenarioLocation(props.scenarioId, row.id) : legacyLocation(row.id), true)
    if (!sent && !input.value.trim()) input.value = text
  }
}
function acceptProjectUpdate(row: DistillationProject) { if (row.id === projectId.value) { project.value = row; draft.value = draftOf(row); void list() } }
async function applyTurn(turn: DistillationTurn) {
  if (!project.value || dirty.value || actionBusy.value || !canEdit.value) return
  const row = await apply(turn, project.value.revision)
  if (row) { project.value = row; draft.value = draftOf(row); previewTurn.value = null; notice.value = '阶段结论已更新。'; void list() }
}
async function copyProject() {
  const row = await copyToScenario(copyScenarioId.value)
  if (row) { copyDialog.value = false; await changeWorkspace(scenarioLocation(copyScenarioId.value, row.id), true) }
}
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
      if (project.value?.id === owner.id && project.value?.revision === owner.revision && draft.value.document === document && document.decision === submitted.decision && document.decision_reason === submitted.decision_reason) Object.assign(document, previous)
      return
    }
    publicationOwner = { id: row.id, revision: row.revision }
  }
  const result = await publish()
  if (result) { publishDialog.value = false; publicationOwner = undefined }
}
function openMaterial(version: DistillationPublication) {
  if (props.embedded) {
    const query: LocationQueryRaw = { ...route.query, stage: 'materials', source_id: version.data_source_id, distillation_id: projectId.value }
    delete query.materials_offset
    void router.push({ name: 'scenario-detail', params: { id: props.scenarioId }, query })
    return
  }
  void router.push({ name: 'data-sources', query: { source_id: version.data_source_id, return_to: route.fullPath } })
}
function openLatestPublication() { const latest = publications.value[0]; if (latest) openMaterial(latest) }
async function downloadPublication(publication: DistillationPublication, artifact: DistillationPublication['artifacts'][number]) {
  await download(publication, artifact)
}
async function deletePublication(publication: DistillationPublication) {
  try {
    await ElMessageBox.confirm(
      `删除业务蒸馏产物版本 ${publication.project_revision}？资料库中的对应投影也会移除。`,
      '删除业务蒸馏产物',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch { return }
  await removePublication(publication)
}
function buildScenario() { if (project.value?.scenario_id) void router.push({ name: 'scenario-detail', params: { id: project.value.scenario_id }, query: { stage: 'ontology', return_to: route.fullPath } }) }
async function allowLeave() {
  if (internalNavigation) return true
  if (actionBusy.value || attachmentBusy.value) { error.value = '操作正在处理，请稍后切换。'; return false }
  if (!hasPendingWork.value) return true
  try { await ElMessageBox.confirm('当前有未保存内容，确认离开？', '保留当前内容', { confirmButtonText: '离开', cancelButtonText: '继续编辑', type: 'warning' }); return true } catch { return false }
}
async function reloadSaved() { if (await allowLeave()) await load() }
async function reconnectConversation() { if (!dirty.value && !actionBusy.value) await load(); await Promise.all([loadConversation(), loadAttachments()]) }

watch(project, row => {
  if (!row || row.id !== projectId.value) return
  if (props.embedded) return
  historyScope.value = row.scenario_id || 'shared'
  if (row.scenario_id) void changeWorkspace(scenarioLocation(row.scenario_id, row.id), true)
})
watch(() => [projectId.value, route.query.scenario_id, route.query.shared], () => {
  historyScope.value = routeScope()
  previewTurn.value = null; sourcesOpen.value = false; systemsOpen.value = false; publishDialog.value = false; publicationOwner = undefined
})
onBeforeRouteLeave(allowLeave)
onBeforeRouteUpdate((to, from) => {
  if (internalNavigation) return true
  if (props.embedded) {
    const sameScenario = queryValue(to.params.id) === queryValue(from.params.id)
    const sameProject = queryValue(to.query.distillation_id) === queryValue(from.query.distillation_id)
    if (sameScenario && sameProject) return true
  } else if (queryValue(to.params.id) === queryValue(from.params.id) && to.query.scenario_id === from.query.scenario_id && to.query.shared === from.query.shared) return true
  return allowLeave()
})
function beforeUnload(event: BeforeUnloadEvent) { if (hasPendingWork.value) { event.preventDefault(); event.returnValue = '' } }
onMounted(() => window.addEventListener('beforeunload', beforeUnload))
onBeforeUnmount(() => window.removeEventListener('beforeunload', beforeUnload))
defineExpose({ allowLeave, hasPendingWork })
</script>
