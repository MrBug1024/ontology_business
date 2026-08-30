<template>
  <div class="chat-composer">
    <div v-if="attachments.length" class="attachment-list" aria-label="本次对话附件">
      <article v-for="item in attachments" :key="item.uid" class="attachment-item">
        <el-icon aria-hidden="true"><Document /></el-icon>
        <div class="attachment-copy">
          <strong>{{ item.filename }}</strong>
          <small v-if="item.status === 'uploading'">正在上传 {{ item.progress }}%</small>
          <small v-else-if="item.status === 'ready'">已就绪 · {{ formatSize(item.size) }}</small>
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
        <label class="attachment-button" :class="{ disabled: disabled || busy }" title="上传本次对话使用的文件">
          <el-icon aria-hidden="true"><Paperclip /></el-icon>
          <span>附件</span>
          <input
            type="file"
            multiple
            :accept="AGENT_INVOCATION_FILE_ACCEPT"
            :disabled="disabled || busy"
            aria-label="上传本次对话附件"
            @change="onFilesPicked"
          />
        </label>
        <span class="keyboard-hint">Enter 发送 · Shift + Enter 换行</span>
      </div>
      <div class="submit-actions">
        <el-button v-if="busy" @click="$emit('stop')"><el-icon><VideoPause /></el-icon>停止</el-button>
        <el-button v-else type="primary" :disabled="disabled || uploading || (!message.trim() && !readyAttachments.length)" @click="submitDraft">
          <el-icon><Promotion /></el-icon>发送
        </el-button>
      </div>
    </div>
    <p v-if="uploadError" class="composer-error" role="alert">{{ uploadError }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api'
import type { AgentChatRequest } from '@/types'
import { AGENT_INVOCATION_FILE_ACCEPT, isSupportedInvocationFile } from '@/utils/agentInvocation'

type AttachmentStatus = 'uploading' | 'ready' | 'error'
type ChatAttachmentDraft = {
  uid: string
  file: File
  filename: string
  size: number
  progress: number
  status: AttachmentStatus
  assetVersionId?: string
  expectedSignature?: string
  error?: string
}

const props = withDefaults(defineProps<{
  disabled?: boolean
  busy?: boolean
  placeholder?: string
}>(), {
  disabled: false,
  busy: false,
  placeholder: '输入业务问题或需求，也可以上传本次处理所需的文件',
})

const emit = defineEmits<{
  submit: [payload: AgentChatRequest]
  stop: []
}>()

const message = ref('')
const attachments = ref<ChatAttachmentDraft[]>([])
const uploadError = ref('')
const messageInputRef = ref()

const readyAttachments = computed(() => attachments.value.filter((item) => item.status === 'ready' && item.assetVersionId))
const uploading = computed(() => attachments.value.some((item) => item.status === 'uploading'))

async function uploadOne(item: ChatAttachmentDraft) {
  item.status = 'uploading'
  item.progress = 0
  item.error = ''
  uploadError.value = ''
  try {
    const uploaded = await api.uploadCatalogAttachment({
      file: item.file,
      onProgress: (percent) => { item.progress = percent },
    })
    item.assetVersionId = uploaded.version.id
    item.expectedSignature = uploaded.version.content_sha256
    item.progress = 100
    item.status = 'ready'
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
    const draft: ChatAttachmentDraft = {
      uid: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      file,
      filename: file.name,
      size: file.size,
      progress: 0,
      status: 'uploading',
    }
    attachments.value.push(draft)
    void uploadOne(draft)
  }
}

function removeAttachment(uid: string) {
  attachments.value = attachments.value.filter((item) => item.uid !== uid)
  if (!attachments.value.some((item) => item.status === 'error')) uploadError.value = ''
}

function submitDraft() {
  if (props.disabled || props.busy || uploading.value) return
  const text = message.value.trim()
  if (!text && !readyAttachments.value.length) return
  emit('submit', {
    message: text,
    environment: 'dev',
    attachments: readyAttachments.value.map((item) => ({
      asset_version_id: item.assetVersionId!,
      expected_signature: item.expectedSignature,
      filename: item.filename,
    })),
  })
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

onMounted(() => messageInputRef.value?.focus?.())
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
.submit-actions { flex: 0 0 auto; gap: 8px; }
.attachment-button { position: relative; display: inline-flex; min-height: 36px; align-items: center; gap: 6px; padding: 0 12px; border-radius: 6px; color: var(--text-2); cursor: pointer; font-size: 13px; }
.attachment-button:hover { background: var(--surface-2); color: var(--primary); }
.attachment-button.disabled { cursor: not-allowed; opacity: .55; }
.attachment-button input { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0, 0, 0, 0); }
.keyboard-hint { color: var(--text-3); font-size: 11px; }
.composer-error { margin: 0; font-size: 11px; line-height: 1.45; }
@media (max-width: 680px) {
  .keyboard-hint { display: none; }
  .attachment-list { grid-template-columns: 1fr; }
}
</style>
