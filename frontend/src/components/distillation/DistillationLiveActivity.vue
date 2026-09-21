<template>
  <section class="discovery-live-activity" :class="{ active: working }" :aria-busy="working" aria-live="polite">
    <header>
      <span class="discovery-live-state" :class="{ active: working }">
        <el-icon v-if="working" class="is-loading" aria-hidden="true"><Loading /></el-icon>
        <el-icon v-else aria-hidden="true"><CircleCheck /></el-icon>
        {{ working ? '调查进行中' : '调查状态' }}
      </span>
      <strong>{{ headline }}</strong>
    </header>
    <ol aria-label="本轮业务蒸馏状态">
      <li v-for="item in activityItems" :key="item.key" :class="item.state">
        <span class="discovery-activity-dot" aria-hidden="true"></span>
        <div>
          <strong>{{ item.title }}</strong>
          <p>{{ item.detail }}</p>
        </div>
        <em>{{ item.label }}</em>
      </li>
    </ol>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { CircleCheck, Loading } from '@element-plus/icons-vue'
import type { DistillationTurn } from '@/types/distillationConversation'
import { isWorking } from '@/utils/distillationConversation'

const props = defineProps<{ turn: DistillationTurn }>()
const working = computed(() => isWorking(props.turn))
const runningStep = computed(() => props.turn.steps.find(step => step.status === 'running'))
const completedSteps = computed(() => props.turn.steps.filter(step => step.status === 'succeeded').length)
const evidenceSteps = computed(() => props.turn.steps.filter(step => step.source || step.library || step.mcp).length)
const latestStep = computed(() => runningStep.value || props.turn.steps[props.turn.steps.length - 1])
const headline = computed(() => {
  if (props.turn.status === 'waiting') return '等待你补充关键业务信息'
  if (props.turn.proposal) return working.value ? '阶段建议已产生，正在完成说明' : '阶段建议已产生'
  if (latestStep.value) return runningStep.value ? `正在：${runningStep.value.title}` : `最近：${latestStep.value.title}`
  return working.value ? '正在梳理问题、目标与可用依据' : '本轮调查已结束'
})
const activityItems = computed(() => [
  {
    key: 'alignment',
    title: '问题对齐',
    detail: props.turn.questions.length ? `${props.turn.questions.length} 个问题等待确认` : '围绕本轮输入与业务目标对齐',
    state: props.turn.questions.length ? 'attention' : working.value ? 'active' : 'done',
    label: props.turn.questions.length ? '待补充' : working.value ? '进行中' : '已记录',
  },
  {
    key: 'evidence',
    title: '证据调查',
    detail: props.turn.steps.length
      ? `${completedSteps.value}/${props.turn.steps.length} 项完成 · ${evidenceSteps.value} 项带回执`
      : '尚无工具回执，AI 不会把未读资料写成事实',
    state: runningStep.value ? 'active' : props.turn.steps.length ? 'done' : working.value ? 'active' : 'idle',
    label: runningStep.value ? '执行中' : props.turn.steps.length ? '有回执' : working.value ? '准备中' : '未开始',
  },
  {
    key: 'proposal',
    title: '阶段产物',
    detail: props.turn.proposal
      ? `${props.turn.proposal.assertions.length} 项事实与推断 · ${props.turn.proposal.open_questions.length} 个待确认问题`
      : '形成建议前会先列出证据、推断与未决问题',
    state: props.turn.proposal ? 'done' : working.value ? 'active' : 'idle',
    label: props.turn.proposal ? '已产生' : working.value ? '酝酿中' : '未产生',
  },
])
</script>
