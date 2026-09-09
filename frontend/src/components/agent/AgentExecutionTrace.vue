<template>
  <details class="execution-trace" :open="open" @toggle="onToggle">
    <summary aria-label="展开或收起分析与执行记录">
      <el-icon aria-hidden="true"><Operation /></el-icon>
      <span>分析与执行</span>
      <span class="trace-summary" :class="{ attention: attentionCount }" :role="active ? 'status' : undefined" :aria-live="active ? 'polite' : undefined">{{ summary }}</span>
    </summary>
    <div class="trace-body" :aria-busy="loading">
      <p v-if="loading && !events.length" role="status">正在读取过程记录</p>
      <p v-if="error" role="alert">{{ error }} <el-button text :loading="loading" @click="load"><el-icon aria-hidden="true"><Refresh /></el-icon>重试</el-button></p>
      <ol v-if="phases.length" class="phase-list" aria-label="处理阶段">
        <li v-for="phase in phases" :key="phase.revision">
          <span>{{ phase.label }}</span><time :datetime="phase.created_at">{{ timeLabel(phase.created_at) }}</time>
          <code v-if="phase.error_code">{{ phase.error_code }}</code>
        </li>
      </ol>
      <section v-if="analysis.length" class="analysis-notes" aria-label="分析说明">
        <h4>分析说明</h4>
        <SafeMarkdown v-for="note in analysis" :key="note.revision" :content="note.text" />
      </section>
      <ol v-if="steps.length" class="step-list" aria-label="工具执行步骤">
        <li v-for="(step, index) in steps" :key="step.step_key">
          <div class="step-heading">
            <span class="step-index">{{ index + 1 }}</span>
            <strong>{{ receiptStates[step.invocation_id || '']?.name || executionToolLabel(step.name) }}</strong>
            <span class="step-status" :class="{ attention: executionNeedsAttention(step.status) }">{{ executionStatusLabel(step.status) }}</span>
          </div>
          <div class="step-time">
            <template v-if="step.started_at"><time :datetime="step.started_at">{{ timeLabel(step.started_at) }}</time><span v-if="step.finished_at"> 至 {{ timeLabel(step.finished_at) }}</span></template>
            <span v-else>{{ loading ? '正在读取步骤时间' : '历史记录未保存开始时间' }}</span>
          </div>
          <p v-if="step.error_code" class="step-error">{{ step.error_code === 'provider_execution_failed' ? '能力提供者执行失败，尚未取得有效业务结果。' : '此步骤未能正常完成。' }}</p>
          <AgentCapabilityReceipt
            v-if="step.invocation_id && receiptOwners.get(step.invocation_id) === messageId"
            :agent-id="agentId" :message-id="messageId" :invocation-id="step.invocation_id" :streaming="active"
            @observed="observeReceipt(step.invocation_id, $event)" @updating="$emit('updating')"
          />
          <details v-if="step.error_code || step.invocation_id" class="step-diagnostics">
            <summary>执行标识与错误码</summary>
            <dl><template v-if="step.error_code"><dt>错误码</dt><dd><code>{{ step.error_code }}</code></dd></template>
              <template v-if="step.invocation_id"><dt>调用标识</dt><dd><code>{{ step.invocation_id }}</code></dd></template></dl>
          </details>
        </li>
      </ol>
      <p v-if="!loading && !error && !events.length && !steps.length" class="trace-empty">{{ active ? statusLabel || '正在等待执行进度' : '这条历史消息没有保存过程记录' }}</p>
      <el-button v-if="hasMore" text :loading="loading" @click="load"><el-icon aria-hidden="true"><MoreFilled /></el-icon>加载更多记录</el-button>
    </div>
  </details>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { MoreFilled, Operation, Refresh } from '@element-plus/icons-vue'
import SafeMarkdown from '@/components/SafeMarkdown.vue'
import AgentCapabilityReceipt from './AgentCapabilityReceipt.vue'
import { useAgentExecutionTrace } from '@/composables/useAgentExecutionTrace'
import { executionAnalysis, executionNeedsAttention, executionPhases, executionStatusLabel, executionSteps, executionToolLabel } from '@/utils/agentExecutionTrace'

const props = defineProps<{
  agentId: string; messageId: string; runId?: string; revision?: number; active?: boolean
  statusLabel?: string; toolCalls: unknown[]; receiptOwners: Map<string, string>
}>()
defineEmits<{ (event: 'updating'): void }>()
const open = ref(false)
const receiptStates = ref<Record<string, { name: string; status: string }>>({})
const { events, loading, error, hasMore, load } = useAgentExecutionTrace(() => ({ runId: props.runId, revision: props.revision, open: open.value }))
const steps = computed(() => executionSteps(events.value, props.toolCalls, Boolean(props.active)).map(step => ({
  ...step, status: receiptStates.value[step.invocation_id || '']?.status || step.status,
})))
const phases = computed(() => executionPhases(events.value))
const analysis = computed(() => executionAnalysis(events.value))
const attentionCount = computed(() => steps.value.filter(step => executionNeedsAttention(step.status)).length)
const summary = computed(() => {
  if (props.active) return props.statusLabel || '处理中'
  const count = steps.value.length
  const attention = attentionCount.value ? `，${attentionCount.value} 项需核对` : ''
  const waiting = steps.value.some(step => ['awaiting_confirmation', 'awaiting_approval'].includes(step.status)) ? '，等待处理' : ''
  return count ? `${props.statusLabel || '执行记录'} · ${count} 次工具调用${attention}${waiting}` : props.statusLabel || '查看过程'
})

function onToggle(event: Event) {
  if (event.target instanceof HTMLDetailsElement) open.value = event.target.open
}
function timeLabel(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间未记录' : date.toLocaleTimeString('zh-CN', { hour12: false })
}
function observeReceipt(id: string, state: { name: string; status: string }) {
  const previous = receiptStates.value[id]
  receiptStates.value[id] = state
  if (previous?.status !== state.status && ['awaiting_confirmation', 'awaiting_approval'].includes(state.status)) open.value = true
}
watch(() => props.messageId, () => { open.value = false; receiptStates.value = {} })
</script>

<style scoped>
.execution-trace { min-width: 0; margin-bottom: 12px; color: var(--el-text-color-regular); }
.execution-trace > summary { cursor: pointer; padding: 8px 0; min-height: 36px; line-height: 1.6; overflow-wrap: anywhere; }
summary:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 3px; }
.execution-trace > summary .el-icon { vertical-align: middle; margin-right: 6px; }
.trace-summary { margin-left: 12px; color: var(--el-text-color-regular); font-size: 12px; }
.trace-body { padding: 4px 0 8px 14px; border-left: 2px solid var(--el-border-color); }
.phase-list, .step-list { list-style: none; padding: 0; margin: 0; }
.phase-list { display: flex; flex-wrap: wrap; gap: 8px 16px; font-size: 12px; margin-bottom: 14px; }
.phase-list li { display: flex; flex-wrap: wrap; gap: 6px; }
time, .step-time, .trace-empty { color: var(--el-text-color-regular); }
.step-list > li { padding: 12px 0; border-top: 1px solid var(--el-border-color-lighter); min-width: 0; }
.step-heading { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: baseline; }
.step-heading strong { font-size: 13px; overflow-wrap: anywhere; }
.step-index { font-size: 12px; font-variant-numeric: tabular-nums; min-width: 16px; }
.step-status, .step-time { font-size: 12px; }
.step-time { margin-top: 4px; }
.attention, .trace-summary.attention, .step-error { color: var(--el-color-danger); }
.step-error { font-size: 13px; margin: 8px 0; }
.analysis-notes { margin: 12px 0; }
h4 { margin: 0 0 8px; font-size: 13px; }
.step-diagnostics { font-size: 12px; margin-top: 8px; overflow-wrap: anywhere; }
.step-diagnostics summary { cursor: pointer; padding: 4px 0; }
dl { margin: 8px 0; } dd { margin: 2px 0 8px; }
</style>
