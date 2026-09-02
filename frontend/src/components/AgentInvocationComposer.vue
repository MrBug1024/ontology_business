<template>
  <div class="chat-composer">
    <div v-if="attachments.length" class="attachment-list" aria-label="本次对话附件">
      <article v-for="item in attachments" :key="item.uid" class="attachment-item">
        <el-icon aria-hidden="true"><Document /></el-icon>
        <div class="attachment-copy">
          <strong>{{ item.filename }}</strong>
          <small v-if="item.status === 'uploading'">正在上传 {{ item.progress }}%</small>
          <small v-else-if="item.status === 'ready'">
            {{ item.persistent ? '验证资料库' : '仅本次' }} · {{ formatSize(item.size) }}
          </small>
          <small v-else class="attachment-error">{{ item.error || '上传失败' }}</small>
          <el-progress v-if="item.status === 'uploading'" :percentage="item.progress" :show-text="false" :stroke-width="3" />
        </div>
        <el-button text circle size="small" :disabled="busy || materializing" :aria-label="`移除附件 ${item.filename}`" @click="removeAttachment(item.uid)">
          <el-icon><Close /></el-icon>
        </el-button>
      </article>
    </div>

    <el-input
      ref="messageInputRef"
      v-model="message"
      type="textarea"
      :rows="3"
      resize="none"
      :disabled="disabled || busy"
      :placeholder="placeholder"
      aria-label="输入消息"
      @keydown.enter.exact.prevent="submitDraft"
    />

    <div class="composer-actions">
      <div class="composer-tools">
        <el-segmented
          v-model="uploadMode"
          class="upload-mode"
          :options="uploadModeOptions"
          :disabled="disabled || busy || materializing"
          size="small"
          aria-label="附件保存方式"
        />
        <label class="attachment-button" :class="{ disabled: disabled || busy || materializing }" :title="uploadMode === 'validation_asset' ? '上传并保存到验证资料库' : '上传仅供本次对话使用'">
          <el-icon aria-hidden="true"><Paperclip /></el-icon>
          <span>上传</span>
          <input
            type="file"
            multiple
            :accept="AGENT_INVOCATION_FILE_ACCEPT"
            :disabled="disabled || busy || materializing"
            aria-label="上传验证附件"
            @change="onFilesPicked"
          />
        </label>
        <el-button text :disabled="disabled || busy || materializing" title="选择已上传的验证资料" @click="openLibrary">
          <el-icon><FolderOpened /></el-icon>
          资料库
        </el-button>
        <span class="keyboard-hint">Enter 发送 · Shift + Enter 换行</span>
      </div>
      <div class="submit-actions">
        <el-button v-if="busy" @click="$emit('stop')"><el-icon><VideoPause /></el-icon>停止</el-button>
        <el-button v-else type="primary" :loading="materializing" :disabled="disabled || uploading || (!message.trim() && !readyAttachments.length)" @click="submitDraft">
          <el-icon><Promotion /></el-icon>发送
        </el-button>
      </div>
    </div>
    <section v-if="materializing" class="preparation-status" role="status" aria-live="polite" aria-atomic="true">
      <div class="preparation-heading">
        <strong>验证需求已排队</strong>
        <span>{{ preparationStep }}</span>
      </div>
      <el-progress :percentage="preparationPercentage" :show-text="false" :stroke-width="5" />
      <p>{{ preparationMessage }}</p>
      <small>服务端正在后台流式读取并生成 Parquet；原始大文件不会进入聊天请求。离开页面后任务仍会继续，返回本 Agent 会自动恢复。</small>
    </section>
    <p v-if="uploadError" class="composer-error" role="alert">{{ uploadError }}</p>

    <el-dialog v-model="libraryVisible" title="验证资料库" width="min(620px, 92vw)" append-to-body>
      <div v-loading="libraryLoading" class="library-list">
        <el-empty v-if="!libraryLoading && !savedAssets.length" description="暂无可复用资料" :image-size="64" />
        <div v-for="item in savedAssets" :key="item.versionId" class="library-row">
          <el-icon aria-hidden="true"><Document /></el-icon>
          <div class="library-copy">
            <strong>{{ item.filename }}</strong>
            <small>{{ formatSize(item.size) }} · {{ formatDate(item.createdAt) }}</small>
          </div>
          <el-button
            :type="isAttached(item.versionId) ? 'success' : 'primary'"
            plain
            size="small"
            :disabled="isAttached(item.versionId)"
            @click="attachSaved(item)"
          >
            <el-icon><Check /></el-icon>{{ isAttached(item.versionId) ? '已选择' : '选择' }}
          </el-button>
          <el-button text circle type="danger" title="彻底删除资料" @click="deleteSaved(item)">
            <el-icon><Delete /></el-icon>
          </el-button>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import type { AgentChatRequest, CatalogAsset, CatalogAssetVersion, ValidationDataset, ValidationDatasetJob } from '@/types'
import {
  AGENT_INVOCATION_FILE_ACCEPT,
  isSupportedInvocationFile,
  isTabularInvocationAsset,
} from '@/utils/agentInvocation'

type AttachmentStatus = 'uploading' | 'ready' | 'error'
type ChatAttachmentDraft = {
  uid: string
  file?: File
  assetId?: string
  filename: string
  size: number
  progress: number
  status: AttachmentStatus
  assetVersionId?: string
  expectedSignature?: string
  contentCategory?: string
  persistent?: boolean
  error?: string
}

type SavedAsset = {
  assetId: string
  versionId: string
  filename: string
  size: number
  signature: string
  contentCategory: string | undefined
  createdAt: string
}

type PendingValidationPreparation = {
  version: 1
  agentId: string
  jobId: string
  tableAssetVersionIds: string[]
  tableCount: number
  request: AgentChatRequest
  createdAt: string
}

const props = withDefaults(defineProps<{
  agentId?: string
  conversationId?: string
  disabled?: boolean
  busy?: boolean
  placeholder?: string
  acceptedAttachmentKinds?: string[]
}>(), {
  agentId: '',
  conversationId: '',
  disabled: false,
  busy: false,
  placeholder: '输入业务问题或需求，也可以上传本次处理所需的文件',
  acceptedAttachmentKinds: () => [],
})

const emit = defineEmits<{
  submit: [payload: AgentChatRequest]
  stop: []
}>()

const message = ref('')
const attachments = ref<ChatAttachmentDraft[]>([])
const uploadError = ref('')
const messageInputRef = ref()
const uploadMode = ref<'validation_asset' | 'invocation_attachment'>('validation_asset')
const uploadModeOptions = [
  { label: '保存复用', value: 'validation_asset' },
  { label: '仅本次', value: 'invocation_attachment' },
]
const libraryVisible = ref(false)
const libraryLoading = ref(false)
const savedAssets = ref<SavedAsset[]>([])
const materializing = ref(false)
const preparationJob = ref<ValidationDatasetJob | null>(null)
const preparationTableCount = ref(0)
let preparationController: AbortController | null = null
let recoveryAgentId = ''

const readyAttachments = computed(() => attachments.value.filter((item) => item.status === 'ready' && item.assetVersionId))
const uploading = computed(() => materializing.value || attachments.value.some((item) => item.status === 'uploading'))
const preparationStep = computed(() => (
  preparationJob.value?.status === 'running' ? '第 2/3 步 · 正在构建数据集' : '第 1/3 步 · 等待后台处理'
))
const preparationPercentage = computed(() => (
  preparationJob.value?.status === 'running' ? 68 : preparationJob.value?.status === 'succeeded' ? 100 : 34
))
const preparationMessage = computed(() => {
  const count = preparationTableCount.value
  return preparationJob.value?.status === 'running'
    ? `正在准备 ${count} 个表格；完成后会自动发送这次验证需求。`
    : `已保存本次需求和受管文件引用，等待处理 ${count} 个表格。`
})

async function uploadOne(item: ChatAttachmentDraft) {
  if (!item.file) return
  item.status = 'uploading'
  item.progress = 0
  item.error = ''
  uploadError.value = ''
  try {
    const uploaded = await api.uploadCatalogAttachment({
      file: item.file,
      purpose: uploadMode.value,
      onProgress: (percent) => { item.progress = percent },
    })
    item.assetId = uploaded.asset.id
    item.assetVersionId = uploaded.version.id
    item.expectedSignature = uploaded.version.content_sha256
    item.contentCategory = typeof uploaded.version.profile?.category === 'string'
      ? uploaded.version.profile.category
      : undefined
    item.persistent = uploaded.purpose === 'validation_asset'
    item.progress = 100
    item.status = 'ready'
    if (item.persistent) void loadSavedAssets()
  } catch (error: any) {
    item.status = 'error'
    item.error = error?.response?.data?.detail || error?.message || '附件上传失败'
    uploadError.value = item.error || '附件上传失败'
  }
}

function onFilesPicked(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = ''
  for (const file of files) {
    if (!isSupportedInvocationFile(file.name)) {
      ElMessage.warning(`${file.name}：暂不支持该文件格式`)
      continue
    }
    const draft = reactive<ChatAttachmentDraft>({
      uid: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      file,
      filename: file.name,
      size: file.size,
      progress: 0,
      status: 'uploading',
      persistent: uploadMode.value === 'validation_asset',
    })
    attachments.value.push(draft)
    void uploadOne(draft)
  }
}

async function loadSavedAssets() {
  libraryLoading.value = true
  try {
    const assets = (await api.listCatalogAssets()).filter((item: CatalogAsset) => (
      item.lifecycle_status === 'active' && item.labels?.catalog_purpose === 'validation_asset'
    ))
    const rows = await Promise.all(assets.map(async (asset: CatalogAsset) => {
      const versions = await api.listCatalogAssetVersions(asset.id)
      const latest = [...versions]
        .filter((version: CatalogAssetVersion) => version.status === 'ready')
        .sort((a: CatalogAssetVersion, b: CatalogAssetVersion) => b.version_number - a.version_number)[0]
      if (!latest) return null
      return {
        assetId: asset.id,
        versionId: latest.id,
        filename: asset.name,
        size: latest.byte_size,
        signature: latest.content_sha256,
        contentCategory: typeof latest.version_document?.profile === 'object'
          && latest.version_document.profile !== null
          && typeof (latest.version_document.profile as Record<string, unknown>).category === 'string'
          ? String((latest.version_document.profile as Record<string, unknown>).category)
          : undefined,
        createdAt: latest.created_at,
      } satisfies SavedAsset
    }))
    savedAssets.value = rows.filter((item): item is SavedAsset => item !== null)
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '验证资料库加载失败')
  } finally {
    libraryLoading.value = false
  }
}

function openLibrary() {
  libraryVisible.value = true
  void loadSavedAssets()
}

function isAttached(versionId: string) {
  return attachments.value.some((item) => item.assetVersionId === versionId)
}

function attachSaved(item: SavedAsset) {
  if (isAttached(item.versionId)) return
  attachments.value.push({
    uid: `saved-${item.versionId}`,
    assetId: item.assetId,
    filename: item.filename,
    size: item.size,
    progress: 100,
    status: 'ready',
    assetVersionId: item.versionId,
    expectedSignature: item.signature,
    contentCategory: item.contentCategory,
    persistent: true,
  })
}

async function deleteSaved(item: SavedAsset) {
  await ElMessageBox.confirm(
    `删除“${item.filename}”后，后续验证不能再使用它；已发布的场景能力不会受影响。`,
    '彻底删除验证资料',
    { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
  )
  try {
    await api.deleteCatalogAsset(item.assetId)
    attachments.value = attachments.value.filter((entry) => entry.assetVersionId !== item.versionId)
    savedAssets.value = savedAssets.value.filter((entry) => entry.assetId !== item.assetId)
    ElMessage.success('验证资料已删除')
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '验证资料删除失败')
  }
}

function removeAttachment(uid: string) {
  attachments.value = attachments.value.filter((item) => item.uid !== uid)
  if (!attachments.value.some((item) => item.status === 'error')) uploadError.value = ''
}

function preparationStorageKey(agentId = props.agentId) {
  return agentId ? `ontology.validation-preparation.v1:${agentId}` : ''
}

function savePendingPreparation(pending: PendingValidationPreparation) {
  const key = preparationStorageKey(pending.agentId)
  if (!key) return
  try {
    window.sessionStorage.setItem(key, JSON.stringify(pending))
  } catch {
    // Session recovery is a convenience hint; the PostgreSQL job remains authoritative.
  }
}

function clearPendingPreparation(agentId = props.agentId) {
  const key = preparationStorageKey(agentId)
  if (!key) return
  try {
    window.sessionStorage.removeItem(key)
  } catch {
    // Storage may be unavailable in hardened browsers.
  }
}

function loadPendingPreparation(agentId: string): PendingValidationPreparation | null {
  const key = preparationStorageKey(agentId)
  if (!key) return null
  try {
    const parsed = JSON.parse(window.sessionStorage.getItem(key) || 'null') as PendingValidationPreparation | null
    const createdAt = parsed?.createdAt ? Date.parse(parsed.createdAt) : 0
    if (
      !parsed
      || parsed.version !== 1
      || parsed.agentId !== agentId
      || !Array.isArray(parsed.tableAssetVersionIds)
      || !parsed.tableAssetVersionIds.length
      || !parsed.request?.idempotency_key
      || !createdAt
      || Date.now() - createdAt > 24 * 60 * 60 * 1000
    ) {
      clearPendingPreparation(agentId)
      return null
    }
    return parsed
  } catch {
    clearPendingPreparation(agentId)
    return null
  }
}

function invocationIdempotencyKey() {
  const random = typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`
  return `validation-${random}`
}

function dispatchPreparedRequest(
  pending: PendingValidationPreparation,
  dataset: ValidationDataset,
) {
  preparationJob.value = {
    ...(preparationJob.value || {
      id: pending.jobId,
      error: '',
      created_at: pending.createdAt,
      updated_at: new Date().toISOString(),
    }),
    status: 'succeeded',
    result: dataset,
  }
  materializing.value = false
  emit('submit', {
    ...pending.request,
    attachments: [
      {
        dataset_version_id: dataset.dataset_version_id,
        expected_signature: dataset.content_hash,
        filename: dataset.relation_names.join('、') || '验证数据包',
      },
      ...(pending.request.attachments || []),
    ],
  })
}

async function runPendingPreparation(
  pending: PendingValidationPreparation,
  createJob: boolean,
) {
  preparationController?.abort()
  const controller = new AbortController()
  preparationController = controller
  preparationTableCount.value = pending.tableCount
  materializing.value = true
  uploadError.value = ''
  try {
    const onStatus = (job: ValidationDatasetJob) => {
      preparationJob.value = job
      pending.jobId = job.id
      savePendingPreparation(pending)
    }
    const dataset = createJob
      ? await api.buildValidationDataset(
        pending.tableAssetVersionIds,
        '验证数据包',
        { signal: controller.signal, onStatus },
      )
      : await api.waitForValidationDatasetJob(
        pending.jobId,
        { signal: controller.signal, onStatus },
      )
    if (controller.signal.aborted) return
    dispatchPreparedRequest(pending, dataset)
  } catch (error: unknown) {
    const details = error && typeof error === 'object'
      ? error as { name?: unknown; status?: unknown; message?: unknown }
      : {}
    if (details.name === 'AbortError' || controller.signal.aborted) return
    const failed = preparationJob.value?.status === 'failed'
    if (failed || details.status === 404) clearPendingPreparation(pending.agentId)
    const message = typeof details.message === 'string' ? details.message : ''
    uploadError.value = failed
      ? message || '验证数据集准备失败'
      : `${message || '验证数据集进度连接中断'}；重新进入本 Agent 后会继续恢复。`
    materializing.value = false
  } finally {
    if (preparationController === controller) preparationController = null
  }
}

function resumePendingPreparation(agentId: string) {
  if (!agentId || recoveryAgentId === agentId) return
  recoveryAgentId = agentId
  const pending = loadPendingPreparation(agentId)
  if (pending) void runPendingPreparation(pending, !pending.jobId)
}

async function submitDraft() {
  if (props.disabled || props.busy || uploading.value) return
  const recoverable = loadPendingPreparation(props.agentId)
  if (recoverable) {
    void runPendingPreparation(recoverable, !recoverable.jobId)
    return
  }
  const text = message.value.trim()
  if (!text && !readyAttachments.value.length) return
  const tables = readyAttachments.value.filter((item) => (
    isTabularInvocationAsset(item.contentCategory, item.filename)
  ))
  const documents = readyAttachments.value.filter((item) => (
    !isTabularInvocationAsset(item.contentCategory, item.filename)
  ))
  uploadError.value = ''
  const request: AgentChatRequest = {
    message: text,
    conversation_id: props.conversationId || '',
    environment: 'dev',
    idempotency_key: invocationIdempotencyKey(),
    attachments: documents.map((item) => ({
      asset_version_id: item.assetVersionId!,
      expected_signature: item.expectedSignature,
      filename: item.filename,
    })),
  }
  if (!tables.length) {
    emit('submit', request)
    return
  }
  const pending: PendingValidationPreparation = {
    version: 1,
    agentId: props.agentId,
    jobId: '',
    tableAssetVersionIds: tables.map((item) => item.assetVersionId!),
    tableCount: tables.length,
    request,
    createdAt: new Date().toISOString(),
  }
  savePendingPreparation(pending)
  void runPendingPreparation(pending, true)
}

function clearAfterSuccess() {
  clearPendingPreparation()
  message.value = ''
  attachments.value = []
  uploadError.value = ''
  void nextTick(() => messageInputRef.value?.focus?.())
}

function acknowledgeQueued(idempotencyKey?: string) {
  if (!idempotencyKey) return
  const pending = loadPendingPreparation(props.agentId)
  if (pending?.request.idempotency_key === idempotencyKey) {
    clearPendingPreparation(props.agentId)
  }
}

function submitMessage(text: string) {
  if (props.disabled || props.busy) return
  message.value = text
  submitDraft()
}

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function formatDate(value: string) {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : ''
}

onMounted(() => {
  messageInputRef.value?.focus?.()
  void loadSavedAssets()
  resumePendingPreparation(props.agentId)
})
watch(() => props.agentId, (agentId, previousAgentId) => {
  if (agentId === previousAgentId) return
  preparationController?.abort()
  recoveryAgentId = ''
  resumePendingPreparation(agentId)
})
onUnmounted(() => {
  preparationController?.abort()
})
defineExpose({ acknowledgeQueued, clearAfterSuccess, submitMessage })
</script>

<style scoped>
.chat-composer { display: flex; flex-direction: column; gap: 9px; }
.attachment-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 8px; }
.attachment-item { display: flex; min-width: 0; align-items: center; gap: 9px; padding: 9px 10px; border: 1px solid var(--border); border-radius: 7px; background: var(--surface-2); }
.attachment-item > .el-icon { flex: 0 0 auto; color: var(--primary); }
.attachment-copy { display: flex; min-width: 0; flex: 1; flex-direction: column; gap: 2px; }
.attachment-copy strong { overflow: hidden; color: var(--text-1); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.attachment-copy small { color: var(--text-3); font-size: 10px; }
.attachment-copy .attachment-error, .composer-error { color: var(--danger); }
.attachment-copy :deep(.el-progress) { margin-top: 3px; }
.composer-actions, .composer-tools, .submit-actions { display: flex; align-items: center; }
.composer-actions { min-height: 40px; justify-content: space-between; gap: 12px; }
.composer-tools { min-width: 0; gap: 8px; }
.upload-mode { flex: 0 0 auto; }
.submit-actions { flex: 0 0 auto; gap: 8px; }
.attachment-button { position: relative; display: inline-flex; min-height: 36px; align-items: center; gap: 6px; padding: 0 12px; border-radius: 6px; color: var(--text-2); cursor: pointer; font-size: 13px; }
.attachment-button:hover { background: var(--surface-2); color: var(--primary); }
.attachment-button.disabled { cursor: not-allowed; opacity: .55; }
.attachment-button input { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0, 0, 0, 0); }
.keyboard-hint { color: var(--text-3); font-size: 11px; }
.composer-error { margin: 0; font-size: 11px; line-height: 1.45; }
.composer-status { margin: 0; color: var(--text-2); font-size: 11px; line-height: 1.45; }
.preparation-status { display: grid; gap: 7px; padding: 10px 12px; border: 1px solid color-mix(in srgb, var(--primary) 28%, var(--border)); border-radius: 8px; background: var(--primary-soft); }
.preparation-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.preparation-heading strong { color: var(--text-1); font-size: 13px; }
.preparation-heading span, .preparation-status small { color: var(--text-3); font-size: 11px; line-height: 1.5; }
.preparation-status p { margin: 0; color: var(--text-2); font-size: 12px; line-height: 1.5; }
.library-list { min-height: 140px; }
.library-row { display: flex; min-width: 0; align-items: center; gap: 10px; padding: 10px 0; border-bottom: 1px solid var(--border); }
.library-row:last-child { border-bottom: 0; }
.library-row > .el-icon { flex: 0 0 auto; color: var(--primary); }
.library-copy { display: flex; min-width: 0; flex: 1; flex-direction: column; gap: 3px; }
.library-copy strong { overflow: hidden; color: var(--text-1); font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.library-copy small { color: var(--text-3); font-size: 11px; }
@media (max-width: 680px) {
  .keyboard-hint { display: none; }
  .attachment-list { grid-template-columns: 1fr; }
  .composer-actions { align-items: flex-end; }
  .composer-tools { flex-wrap: wrap; }
}
</style>
