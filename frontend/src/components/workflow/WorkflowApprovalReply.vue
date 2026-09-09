<template>
  <div class="workflow-approval-reply">
    <p v-if="approval.requires_evidence" class="evidence-required">本次同意需附佐证文件</p>
    <AgentInvocationComposer ref="composer" require-ready-attachments :busy="busy" placeholder="同意或驳回" @submit="submit" @stop="cancel" />
    <p v-if="error" role="alert">{{ error }}</p>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import AgentInvocationComposer from '@/components/AgentInvocationComposer.vue'
import { workflowApprovals } from '@/api/workflowApprovals'
import type { AgentChatRequest, WorkflowApproval } from '@/types'
import type { ChannelReplyRequest } from '@/types/channelDelivery'

const props = defineProps<{ approval: WorkflowApproval }>()
const emit = defineEmits<{ (event: 'completed', workflowRunId: string): void }>()
const composer = ref<InstanceType<typeof AgentInvocationComposer>>()
const busy = ref(false)
const error = ref('')
let controller: AbortController | undefined
let pendingReply: { fingerprint: string; messageId: string } | undefined

function cancel() {
  if (busy.value) error.value = '已停止等待回复结果，请核对当前待办状态'
  controller?.abort()
  busy.value = false
}
async function submit(draft: AgentChatRequest) {
  if (busy.value) return
  if (!draft.message.trim()) { error.value = '请填写本次审批决定'; return }
  const evidence: ChannelReplyRequest['evidence'] = []
  for (const attachment of draft.attachments || []) {
    if (!attachment.asset_version_id && !attachment.dataset_version_id) {
      error.value = '佐证文件尚未准备完成'
      return
    }
    evidence.push({ asset_version_id: attachment.asset_version_id, dataset_version_id: attachment.dataset_version_id, expected_signature: attachment.expected_signature })
  }
  const approval = props.approval
  const fingerprint = JSON.stringify([approval.id, approval.revision, draft.message, evidence])
  if (pendingReply?.fingerprint !== fingerprint) pendingReply = { fingerprint, messageId: draft.idempotency_key || crypto.randomUUID() }
  const request = new AbortController()
  controller = request
  busy.value = true
  error.value = ''
  try {
    const result = await workflowApprovals.reply(approval.id, {
      text: draft.message, message_id: pendingReply.messageId,
      expected_revision: approval.revision, evidence,
    }, request.signal)
    if (request.signal.aborted || props.approval.id !== approval.id) return
    composer.value?.clearAfterAccepted()
    pendingReply = undefined
    emit('completed', result.workflow_run_id || approval.workflow_run_id)
  } catch (failure: unknown) {
    if (!request.signal.aborted) error.value = failure instanceof Error ? failure.message : '审批回复未完成'
  } finally {
    if (controller === request) busy.value = false
  }
}
watch(() => props.approval.id, () => { cancel(); error.value = ''; pendingReply = undefined })
onBeforeUnmount(cancel)
</script>

<style scoped>
.workflow-approval-reply { min-width: 0; }
.workflow-approval-reply > p { font-size: 13px; color: var(--el-color-danger); overflow-wrap: anywhere; }
.workflow-approval-reply .evidence-required { color: var(--text-2); }
</style>
