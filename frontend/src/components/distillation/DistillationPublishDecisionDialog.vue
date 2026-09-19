<template>
  <el-dialog :model-value="modelValue" title="这份阶段结论接下来怎么用？" width="min(560px, 94vw)" :close-on-click-modal="false" :close-on-press-escape="!busy" :show-close="!busy" @update:model-value="emit('update:modelValue', $event)">
    <p class="decision-intro">保存前，请确定当前方向。资料库会保留这次决定与结论，供后续场景建设参考。</p>
    <el-radio-group :model-value="choice" class="decision-options" aria-label="阶段结论的下一步" :disabled="busy" @update:model-value="chooseDirection">
      <el-radio v-for="item in choices" :key="item.value" :value="item.value" border><strong>{{ item.label }}</strong><span>{{ item.reason }}</span></el-radio>
    </el-radio-group>
    <label class="decision-label" for="distillation-decision-reason">补充决定依据（可选）</label>
    <el-input id="distillation-decision-reason" v-model="reason" type="textarea" :rows="2" :maxlength="4000" :disabled="busy" placeholder="可以说明尚待解决的问题，或继续建设的关键依据" />
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <template #footer><el-button :disabled="busy" @click="emit('update:modelValue', false)">继续对话</el-button><el-button type="primary" :disabled="choice === 'undecided'" :loading="busy" @click="submit">确认并保存到资料库</el-button></template>
  </el-dialog>
</template>
<script setup lang="ts">
import { ref, watch } from 'vue'
import type { DistillationDocument } from '@/types/businessDistillation'
import { HANDOFF_CHOICES, handoffDecision, handoffReasonAfterChoice } from '@/utils/distillationHandoff'
const props = defineProps<{ modelValue: boolean; document: DistillationDocument; busy: boolean; error: string }>()
const emit = defineEmits<{ 'update:modelValue': [open: boolean]; confirm: [decision: ReturnType<typeof handoffDecision>] }>()
const choice = ref<DistillationDocument['decision']>('undecided'), reason = ref('')
const choices = HANDOFF_CHOICES
watch(() => props.modelValue, open => { if (open) { choice.value = props.document.decision; reason.value = props.document.decision_reason } })
function chooseDirection(value: string | number | boolean) {
  if (props.busy || (value !== 'continue' && value !== 'adjust' && value !== 'stop')) return
  reason.value = handoffReasonAfterChoice(choice.value, value, reason.value)
  choice.value = value
}
function submit() { if (choice.value !== 'undecided' && !props.busy) emit('confirm', handoffDecision(choice.value, reason.value)) }
</script>
<style scoped>
.decision-intro { margin-top: 0; color: var(--text-2); line-height: 1.7; }
.decision-options { display: grid; gap: 12px; }
.decision-options :deep(.el-radio) { height: auto; min-height: 68px; margin: 0; padding: 14px; white-space: normal; }
.decision-options :deep(.el-radio__label) { display: grid; gap: 6px; line-height: 1.5; }
.decision-options span { color: var(--text-2); font-size: 12px; }
.decision-label { display: block; margin: 20px 0 8px; font-size: 13px; }
</style>
