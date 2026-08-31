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
        <el-button text circle size="small" :disabled="busy" :aria-label="`移除附件 ${item.filename}`" @click="removeAttachment(item.uid)">
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
          :disabled="disabled || busy"
          size="small"
          aria-label="附件保存方式"
        />
        <label class="attachment-button" :class="{ disabled: disabled || busy }" :title="uploadMode === 'validation_asset' ? '上传并保存到验证资料库' : '上传仅供本次对话使用'">
          <el-icon aria-hidden="true"><Paperclip /></el-icon>
          <span>上传</span>
          <input
            type="file"
            multiple
            :accept="AGENT_INVOCATION_FILE_ACCEPT"
            :disabled="disabled || busy"
            aria-label="上传验证附件"
            @change="onFilesPicked"
          />
        </label>
        <el-button text :disabled="disabled || busy" title="选择已上传的验证资料" @click="openLibrary">
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
    <p v-if="materializing" class="composer-status" role="status">
      正在准备 {{ readyAttachments.filter((item) => isTableFile(item.filename)).length }} 个表格的数据集，可继续浏览其他页面
    </p>
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
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import type { AgentChatRequest, CatalogAsset, CatalogAssetVersion } from '@/types'
import { AGENT_INVOCATION_FILE_ACCEPT, isSupportedInvocationFile } from '@/utils/agentInvocation'

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
  persistent?: boolean
  error?: string
}

type SavedAsset = {
  assetId: string
  versionId: string
  filename: string
  size: number
  signature: string
  createdAt: string
}

const props = withDefaults(defineProps<{
  disabled?: boolean
  busy?: boolean
  placeholder?: string
  acceptedAttachmentKinds?: string[]
}>(), {
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

const readyAttachments = computed(() => attachments.value.filter((item) => item.status === 'ready' && item.assetVersionId))
const uploading = computed(() => materializing.value || attachments.value.some((item) => item.status === 'uploading'))

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

function isTableFile(filename: string) {
  return /\.(csv|tsv|xls|xlsx|xlsm)$/i.test(filename)
}

async function submitDraft() {
  if (props.disabled || props.busy || uploading.value) return
  const text = message.value.trim()
  if (!text && !readyAttachments.value.length) return
  const tables = readyAttachments.value.filter((item) => isTableFile(item.filename))
  const documents = readyAttachments.value.filter((item) => !isTableFile(item.filename))
  materializing.value = tables.length > 0
  uploadError.value = ''
  try {
    const tableDataset = tables.length
      ? await api.buildValidationDataset(tables.map((item) => item.assetVersionId!))
      : null
    emit('submit', {
      message: text,
      environment: 'dev',
      attachments: [
        ...(tableDataset ? [{
          dataset_version_id: tableDataset.dataset_version_id,
          expected_signature: tableDataset.content_hash,
          filename: tableDataset.relation_names.join('、') || '验证数据包',
        }] : []),
        ...documents.map((item) => ({
          asset_version_id: item.assetVersionId!,
          expected_signature: item.expectedSignature,
          filename: item.filename,
        })),
      ],
    })
  } catch (error: any) {
    uploadError.value = error?.response?.data?.detail || error?.message || '验证数据集准备失败'
  } finally {
    materializing.value = false
  }
}

function clearAfterSuccess() {
  message.value = ''
  attachments.value = []
  uploadError.value = ''
  void nextTick(() => messageInputRef.value?.focus?.())
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
})
defineExpose({ clearAfterSuccess, submitMessage })
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
