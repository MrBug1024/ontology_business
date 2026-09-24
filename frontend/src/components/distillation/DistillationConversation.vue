<template>
  <section class="discovery-conversation" aria-label="业务蒸馏对话">
    <header v-if="workspaceActions" class="discovery-conversation-toolbar">
      <div>
        <strong>业务蒸馏 AI</strong>
        <small>会话可删除，场景产物保留</small>
      </div>
      <div class="distill-actions">
        <el-button text :disabled="disabled" @click="$emit('new')">新建会话</el-button>
        <el-button text @click="$emit('history')">会话记录</el-button>
        <el-button text :disabled="disabled" @click="$emit('systems')">业务系统</el-button>
      </div>
    </header>
    <div ref="scrollArea" class="discovery-messages" :class="{ 'is-empty': !turns.length }" @scroll="trackScroll">
      <div v-if="!turns.length && !loading" class="discovery-welcome" :class="{ 'is-compact': compact }">
        <div class="discovery-kicker">业务蒸馏</div>
        <h1>从一个真实问题开始</h1>
        <div class="discovery-starters">
          <button v-for="starter in starters" :key="starter.title" :disabled="disabled" @click="choose(starter.message)"><strong>{{ starter.title }}</strong><span v-if="!compact">{{ starter.caption }}</span></button>
        </div>
      </div>
      <p v-if="loading" class="discovery-muted" role="status">正在恢复对话…</p>
      <el-button v-if="hasMore" text :loading="loading" @click="$emit('older')">加载更早的对话</el-button>
      <article v-for="turn in turns" :key="turn.id" class="discovery-turn">
        <div class="discovery-user-message">{{ turn.message }}</div>
        <ul v-if="turn.attachments?.length" class="discovery-sent-attachments"><li v-for="attachment in turn.attachments" :key="attachment.id"><el-icon aria-hidden="true"><Paperclip /></el-icon>{{ attachment.filename }}<small>{{ attachment.status === 'expired' ? '已到期' : attachment.status === 'removed' ? '已移除' : `保留至 ${new Date(attachment.expires_at).toLocaleString()}` }}</small><el-button v-if="!disabled && !['expired', 'removed'].includes(attachment.status)" text :disabled="!!removingAttachment" :loading="removingAttachment === attachment.id" :aria-label="`从当前对话移除附件 ${attachment.filename}`" @click="$emit('remove-submitted', attachment.id)">移除</el-button></li></ul>
        <div class="discovery-assistant-message">
          <div class="discovery-speaker"><span class="discovery-agent-mark" aria-hidden="true">蒸</span><strong>业务蒸馏 AI</strong><span class="discovery-muted">{{ TURN_STATUS_LABELS[turn.status] }}</span></div>
          <DistillationLiveActivity :turn="turn" />
          <details v-if="turn.steps.length" class="discovery-tool-steps">
            <summary>查证过程 · {{ turn.steps.length }} 项</summary>
            <ol><li v-for="step in turn.steps" :key="step.id"><div><strong>{{ step.title }}</strong><span>{{ stepStatus[step.status] }}</span></div><p v-if="step.summary">{{ step.summary }}</p><p v-for="library in (step.libraries?.length ? step.libraries : step.library ? [step.library] : [])" :key="library.evidence_key">资料库依据：{{ library.title }} · {{ new Date(library.retrieved_at).toLocaleString() }}</p><p v-if="step.capability">Jev 决策能力回执：{{ step.capability.model }} · {{ step.capability.result_count }} 项 · {{ step.capability.results.length ? `最低置信度 ${Math.min(...step.capability.results.map(result => result.confidence)).toFixed(2)}` : '未形成可验证结果' }}</p><p v-if="step.mcp">历史 MCP 资料回执（兼容旧会话）：{{ step.mcp.title }} · {{ new Date(step.mcp.retrieved_at).toLocaleString() }}</p><p v-if="step.mcp?.summary">{{ step.mcp.summary }}</p><DistillationSourceObservation v-if="step.source" :source="step.source" /></li></ol>
          </details>
          <template v-for="(part, index) in splitAssistantMessage(turn.assistant_message)" :key="`${turn.id}:${index}`">
            <details v-if="part.kind === 'thinking'" class="discovery-thinking" :open="part.streaming && isWorking(turn)">
              <summary>AI 思考过程<span v-if="part.streaming && isWorking(turn)">生成中…</span></summary>
              <SafeMarkdown :content="part.content" />
            </details>
            <div v-else class="discovery-answer">
              <SafeMarkdown :content="part.content" />
            </div>
          </template>
          <p v-if="isWorking(turn) && !turn.assistant_message" class="discovery-muted" role="status">{{ turn.steps[turn.steps.length - 1]?.title || '正在梳理问题与可用依据…' }}</p>
          <div v-if="turn.error" class="discovery-error-recovery" role="alert">
            <strong>本轮调查未完成</strong>
            <p>{{ visibleTurnError(turn) }}</p>
            <small>已提交的问题和已取得的调查回执都会保留；可直接重试，或先补充模型、资料与调查范围。</small>
            <el-button v-if="['failed', 'cancelled'].includes(turn.status) && turn.id === latestId" text type="primary" :disabled="disabled || working" @click="choose(turn.message)">编辑后重试</el-button>
          </div>
          <div v-if="turn.questions.length" class="discovery-questions">
            <section v-for="question in turn.questions" :key="question.id" class="discovery-question">
              <strong>{{ question.title }}</strong><p>{{ question.question }}</p><small v-if="question.reason">{{ question.reason }}</small>
              <div class="discovery-answer-options"><button v-for="option in question.options" :key="option" :aria-pressed="selectedAnswers.get(`${turn.id}:${question.id}`) === `${question.question}\n${option}`" :disabled="disabled || working || turn.id !== latestId" @click="answerQuestion(turn.id, question, option)">{{ option }}</button></div>
            </section>
            <p v-if="turn.status === 'waiting' && turn.id === latestId" class="discovery-muted">等待你的补充。选择一个方向，或在下方自由回答。</p>
          </div>
          <div v-if="turn.proposal" class="discovery-proposal-card">
            <div><strong>{{ turn.applied_revision ? '已采用的阶段结论' : isWorking(turn) ? '阶段建议生成中' : '阶段建议已整理' }}</strong><p>{{ turn.proposal.assertions.length }} 项事实与推断 · {{ turn.proposal.to_be.nodes.length }} 个目标流程节点 · {{ turn.proposal.open_questions.length }} 个待确认问题</p></div>
            <div class="distill-actions"><el-button @click="$emit('preview', turn)">查看阶段建议</el-button><el-button v-if="!turn.applied_revision" type="primary" plain :loading="applying === turn.id" :disabled="!canApply || working || !!applying" @click="$emit('apply', turn)">确认采用</el-button><span v-else class="discovery-muted">已保存 · 版本 {{ turn.applied_revision }}</span></div>
          </div>
        </div>
      </article>
    </div>
    <div class="discovery-composer-area">
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon><el-button text @click="$emit('reload')">重新连接</el-button></el-alert>
      <p v-if="connectionNotice" class="discovery-connection-state" role="status">{{ connectionNotice }}</p>
      <p v-if="blockedReason" class="discovery-muted" role="status">{{ blockedReason }}</p>
      <slot name="attachments" />
      <form class="discovery-composer" @submit.prevent="submit">
        <label class="discovery-visually-hidden" for="distillation-message">给业务蒸馏 AI 的消息</label>
        <textarea id="distillation-message" ref="textarea" v-model="input" :disabled="disabled" maxlength="12000" rows="3" placeholder="描述业务困境、补充一段事实，或告诉我你希望查证什么…" @keydown="sendOnShortcut" />
        <input ref="filePicker" class="discovery-visually-hidden" type="file" multiple tabindex="-1" aria-label="选择临时附件" :disabled="disabled || working || uploadBusy" @change="filesSelected" />
        <div class="discovery-composer-footer">
          <div class="distill-actions">
            <DistillationResourceSettings v-model="resourceSelection" :disabled="disabled || working || sending" :scope-key="scopeKey" :scenario-id="scenarioId" />
            <el-button text circle :disabled="disabled || working || uploadBusy" aria-label="添加临时附件" title="临时附件" @click="filePicker?.click()"><el-icon aria-hidden="true"><Paperclip /></el-icon></el-button>
            <el-button text :disabled="disabled || working || uploadBusy" @click="$emit('sources')">引用资料库</el-button>
          </div>
          <el-button v-if="working" :loading="cancelling" @click="$emit('cancel')">停止本轮</el-button>
          <el-button v-else native-type="submit" type="primary" :disabled="disabled || !!blockedReason || uploadBusy || (!input.trim() && !hasAttachments)" :loading="sending">发送<el-icon class="discovery-send-icon"><Top /></el-icon></el-button>
        </div>
      </form>
      <p v-if="!compact" class="discovery-composer-note">Enter 发送 · Shift + Enter 换行 · 临时附件不入资料库 · 阶段建议经核对后保存</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { Paperclip, Top } from '@element-plus/icons-vue'
import SafeMarkdown from '@/components/SafeMarkdown.vue'
import DistillationLiveActivity from './DistillationLiveActivity.vue'
import DistillationSourceObservation from './DistillationSourceObservation.vue'
import DistillationResourceSettings from './DistillationResourceSettings.vue'
import type { DistillationQuestion, DistillationResourceSelection, DistillationTurn } from '@/types/distillationConversation'
import { composeClarificationAnswer, isWorking, splitAssistantMessage, TURN_STATUS_LABELS, visibleTurnError } from '@/utils/distillationConversation'
const input = defineModel<string>({ required: true })
const props = withDefaults(defineProps<{ turns: DistillationTurn[]; loading: boolean; hasMore: boolean; working: boolean; sending: boolean; cancelling: boolean; applying: string; disabled: boolean; canApply: boolean; error: string; blockedReason: string; uploadBusy: boolean; hasAttachments: boolean; removingAttachment: string; scopeKey: string; scenarioId?: string; compact?: boolean; workspaceActions?: boolean; streaming?: boolean; reconnecting?: boolean }>(), { compact: false, scenarioId: '', workspaceActions: false, streaming: false, reconnecting: false })
const emit = defineEmits<{ send: [selection: DistillationResourceSelection]; cancel: []; reload: []; older: []; sources: []; files: [files: File[]]; 'remove-submitted': [id: string]; preview: [turn: DistillationTurn]; apply: [turn: DistillationTurn]; new: []; history: []; systems: [] }>()
const scrollArea = ref<HTMLElement>(), textarea = ref<HTMLTextAreaElement>()
const filePicker = ref<HTMLInputElement>()
const nearBottom = ref(true)
const selectedAnswers = ref(new Map<string, string>())
const resourceSelection = ref<DistillationResourceSelection>({ llm_config_id: null, skill_ids: [], mcp_ids: [] })
const latestId = computed(() => props.turns[props.turns.length - 1]?.id)
const connectionNotice = computed(() => {
  if (props.reconnecting) return '实时连接中断，正在自动恢复最新调查状态…'
  if (props.streaming && props.working) return '实时输出中'
  return ''
})
const stepStatus = { running: '进行中', succeeded: '已完成', failed: '未完成' }
const starters = [
  { title: '梳理一个业务困境', caption: '从真正受益的人开始', message: '我想先弄清楚一个项目真正应该解决的问题。' },
  { title: '逆向现有业务流程', caption: '从资料和事实还原过程', message: '我有一个现有系统，希望从证据还原业务流程，再分析哪些环节值得保留。' },
  { title: '审视需求与价值', caption: '找出必要与多余的环节', message: '我想审查当前需求是否真的能解决核心痛点，并探索更有效的实现路径。' },
]
function choose(value: string) { input.value = value; void nextTick(() => textarea.value?.focus()) }
function answerQuestion(turnId: string, question: DistillationQuestion, option: string) {
  const key = `${turnId}:${question.id}`, answer = `${question.question}\n${option}`
  input.value = composeClarificationAnswer(input.value, answer, selectedAnswers.value.get(key))
  selectedAnswers.value.set(key, answer)
  void nextTick(() => textarea.value?.focus())
}
function selectedResourceSelection(): DistillationResourceSelection {
  return {
    llm_config_id: resourceSelection.value.llm_config_id || null,
    skill_ids: [...new Set(resourceSelection.value.skill_ids || [])].sort(),
    mcp_ids: [...new Set(resourceSelection.value.mcp_ids || [])].sort(),
    investigation_tool_keys: resourceSelection.value.investigation_tool_keys == null ? null : [...resourceSelection.value.investigation_tool_keys],
  }
}
function submit() { emit('send', selectedResourceSelection()) }
function filesSelected(event: Event) { const target = event.target; if (target instanceof HTMLInputElement) { emit('files', Array.from(target.files || [])); target.value = '' } }
function sendOnShortcut(event: KeyboardEvent) {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return
  if (props.working || props.sending || props.disabled || props.blockedReason || props.uploadBusy) return
  if (!input.value.trim() && !props.hasAttachments) return
  event.preventDefault()
  submit()
}
function trackScroll() { const element = scrollArea.value; if (element) nearBottom.value = element.scrollHeight - element.scrollTop - element.clientHeight < 100 }
watch(() => props.turns.map(turn => `${turn.id}:${turn.updated_at}:${turn.status}`).join('|'), async () => { if (!nearBottom.value) return; await nextTick(); const element = scrollArea.value; if (element) element.scrollTop = element.scrollHeight })
</script>

