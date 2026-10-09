<template>
  <section v-if="delivery" class="construction-delivery" aria-label="建设交付与补充">
    <strong>本轮建设结果</strong>
    <p>可采用定义 {{ delivery.validated_definition_count }} 项 · 阻塞 {{ delivery.blocked_definition_count }} 项 · 缺少实现 {{ delivery.implementation_required_count }} 项 · 未覆盖要求 {{ delivery.missing_requirement_count }} 项</p>
    <p v-if="delivery.repair_summary">内部修复 {{ delivery.repair_summary.attempt_count }} 轮，接受有效改进 {{ delivery.repair_summary.accepted_count }} 次；剩余阻塞 {{ delivery.repair_summary.remaining_blocker_count }} 项。</p>
    <p>{{ delivery.next_step }}</p>
    <el-form v-if="questions.length" label-position="top" @submit.prevent="clarify">
      <el-form-item v-for="question in questions" :key="question.question_id" :label="question.message">
        <el-input v-if="question.question_id" v-model="answers[question.question_id]" type="textarea" :rows="2" maxlength="1500" :aria-label="question.message" placeholder="补充必要事实、依据或明确的修正口径" />
        <small>{{ question.completion_condition }}</small>
      </el-form-item>
      <el-button type="primary" :disabled="busy || !answered.length" native-type="submit">补充后继续建设并校验</el-button>
    </el-form>
    <details>
      <summary>原方案不成立或反复受阻</summary>
      <p>说明限制和可接受取舍，顾问将提出替代方案及要求差异。改变业务要求仍需人工审阅。</p>
      <el-input v-model="rationale" type="textarea" :rows="3" maxlength="4000" aria-label="重新规划原因及取舍" placeholder="例如：缺少哪些依据、哪些对象不应成立、允许合并或替代哪些环节" />
      <el-button :disabled="busy || !rationale.trim()" @click="replan">请求重新规划</el-button>
    </details>
  </section>
</template>
<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import type { ConstructionDelivery, ConstructionResolution } from '@/types'
const props = defineProps<{ delivery?: ConstructionDelivery; proposalId: string; revision: number; busy: boolean }>()
const emit = defineEmits<{ resolve: [instruction: ConstructionResolution] }>()
const answers = reactive<Record<string, string>>({})
const rationale = ref('')
const questions = computed(() => props.delivery?.questions.filter(item => item.question_id).slice(0, 3) || [])
const answered = computed(() => questions.value.flatMap(question => question.question_id && answers[question.question_id]?.trim()
  ? [{ question_id: question.question_id, answer: answers[question.question_id].trim() }] : []))
function submit(action: ConstructionResolution['action']) {
  emit('resolve', { proposal_id: props.proposalId, expected_revision: Math.max(props.revision, 1),
    action, answers: answered.value, rationale: rationale.value.trim() })
}
function clarify() { if (!props.busy && answered.value.length) submit('clarify') }
function replan() { if (!props.busy && rationale.value.trim()) submit('replan') }
</script>
<style scoped>
.construction-delivery { padding: 14px; margin: 12px 0; border: 1px solid var(--border); border-radius: 8px; }
p, small { color: var(--text-secondary); line-height: 1.6; }
small { display: block; margin-top: 6px; }
details { margin-top: 16px; }
summary { cursor: pointer; padding: 8px 0; }
details .el-button { margin-top: 12px; }
</style>
