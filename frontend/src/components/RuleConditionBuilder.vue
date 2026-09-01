<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import RuleConditionNodeEditor from '@/components/RuleConditionNodeEditor.vue'
import type { EditableRuleCondition, RuleCondition } from '@/types/ruleConditions'
import {
  countRuleConditionLeaves,
  editableRuleCondition,
  newEditableRuleGroup,
  parseRuleCondition,
  serializeRuleCondition,
} from '@/utils/ruleConditions'

type EmptyCondition = Record<string, never>

const props = withDefaults(defineProps<{
  modelValue?: RuleCondition | Record<string, unknown>
  fields?: string[]
}>(), {
  modelValue: () => ({}),
  fields: () => [],
})

const emit = defineEmits<{
  (event: 'update:modelValue', value: RuleCondition | EmptyCondition): void
}>()

const root = ref<EditableRuleCondition>(newEditableRuleGroup())
const invalidInput = ref(false)
let lastEmitted = '{}'

const leafCount = computed(() => countRuleConditionLeaves(root.value))
const summary = computed(() => leafCount.value
  ? `${leafCount.value} 个判断条件，可使用嵌套组合与取反`
  : '尚未配置判断条件')

function load(value: unknown) {
  const signature = JSON.stringify(value || {})
  if (signature === lastEmitted) return
  const parsed = parseRuleCondition(value)
  const empty = value !== null && typeof value === 'object' && Object.keys(value).length === 0
  invalidInput.value = !parsed && !empty
  if (invalidInput.value) return
  root.value = parsed ? editableRuleCondition(parsed) : newEditableRuleGroup()
  lastEmitted = signature
}

function resetInvalidCondition() {
  invalidInput.value = false
  root.value = newEditableRuleGroup()
  lastEmitted = '{}'
  emit('update:modelValue', {})
}

watch(() => props.modelValue, load, { immediate: true, deep: true })
watch(root, (value) => {
  if (invalidInput.value) return
  const condition = serializeRuleCondition(value) || {}
  const signature = JSON.stringify(condition)
  if (signature === lastEmitted) return
  lastEmitted = signature
  emit('update:modelValue', condition)
}, { deep: true })
</script>

<template>
  <div class="condition-builder">
    <div class="condition-toolbar">
      <span>{{ summary }}</span>
    </div>
    <el-alert
      v-if="invalidInput"
      type="error"
      :closable="false"
      title="现有条件格式无法安全编辑"
      description="原始条件尚未被覆盖。确认重建后可重新配置。"
      show-icon
    >
      <template #default>
        <el-button size="small" type="danger" plain @click="resetInvalidCondition">重建条件</el-button>
      </template>
    </el-alert>
    <RuleConditionNodeEditor v-else v-model="root" :fields="fields" />
  </div>
</template>

<style scoped>
.condition-builder { display: grid; width: 100%; min-width: 0; gap: 10px; }
.condition-toolbar { display: flex; min-height: 20px; align-items: center; justify-content: space-between; gap: 12px; }
.condition-toolbar > span { color: var(--text-3); font-size: 12px; }
</style>
