<template>
  <section class="capability-receipt" aria-label="业务处理结果" :aria-busy="loading">
    <strong v-if="receipt">{{ receipt.name }}</strong>
    <div v-if="receipt" class="receipt-text" role="status">{{ receipt.delivery.text || receipt.message }}</div>
    <span v-else-if="loading" role="status">正在读取处理结果</span>
    <p v-if="error" class="receipt-error" role="alert">{{ error }}</p>
    <div class="receipt-files">
      <a v-for="file in files" :key="file.id" :href="agentCapabilityReceiptApi.artifactDownloadUrl(file.id)" target="_blank" rel="noopener">
        <el-icon aria-hidden="true"><Document /></el-icon>{{ file.filename }}
      </a>
      <el-button v-if="error" :loading="loading" aria-label="重新读取处理结果" title="重新读取处理结果" @click="refresh"><el-icon aria-hidden="true"><Refresh /></el-icon></el-button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Document, Refresh } from '@element-plus/icons-vue'
import { agentCapabilityReceiptApi } from '@/api/agentCapabilityReceipt'
import type { AgentCapabilityReceipt } from '@/types/agentCapabilityReceipt'
import { receiptResourceId } from '@/utils/agentCapabilityReceipt'

const props = defineProps<{ agentId: string; messageId: string; invocationId: string; scenarioId?: string; streaming?: boolean }>()
const emit = defineEmits<{ (event: 'updating'): void }>()
const receipt = ref<AgentCapabilityReceipt | null>(null)
const loading = ref(false)
const error = ref('')
const files = computed(() => (receipt.value?.delivery.attachments || []).filter(file => receiptResourceId(file.id)))
let generation = 0
let controller: AbortController | undefined
let timer: ReturnType<typeof setTimeout> | undefined

function stop() {
  generation++
  controller?.abort()
  if (timer) clearTimeout(timer)
  timer = undefined
}

async function refresh() {
  stop()
  const current = generation
  const request = new AbortController()
  controller = request
  loading.value = !receipt.value
  error.value = ''
  try {
    const result = await agentCapabilityReceiptApi.get(props.agentId, props.invocationId, props.messageId, request.signal)
    if (current !== generation || request.signal.aborted) return
    if (receipt.value?.delivery.revision !== result.delivery.revision) emit('updating')
    receipt.value = result
    if (['pending', 'running', 'awaiting_confirmation', 'awaiting_approval'].includes(result.status)) {
      timer = setTimeout(() => { void refresh() }, 2000)
    }
  } catch (failure: unknown) {
    if (current === generation && !request.signal.aborted) error.value = failure instanceof Error ? failure.message : '处理结果读取失败'
  } finally {
    if (current === generation) loading.value = false
  }
}

watch(() => [props.agentId, props.messageId, props.invocationId, props.streaming], () => {
  stop()
  receipt.value = null
  if (props.messageId && !props.streaming) void refresh()
}, { immediate: true })
onBeforeUnmount(stop)
</script>

<style scoped>
.capability-receipt { border-top: 1px solid var(--el-border-color); margin-top: 8px; padding: 12px 0; min-width: 0; }
.capability-receipt strong { font-size: 14px; }
.receipt-text { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.65; margin-top: 6px; }
.receipt-files { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 8px; }
.receipt-files a { display: inline-flex; gap: 5px; align-items: center; max-width: 100%; overflow-wrap: anywhere; }
.receipt-error { color: var(--el-color-danger); margin: 8px 0; overflow-wrap: anywhere; }
</style>
