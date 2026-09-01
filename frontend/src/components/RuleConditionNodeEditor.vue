<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type {
  EditableRuleCondition,
  EditableRuleConditionGroup,
  EditableRuleConditionLeaf,
  RuleGroupOperator,
  RuleLeafOperator,
} from '@/types/ruleConditions'
import {
  RULE_CONDITION_MAX_CHILDREN,
  RULE_CONDITION_MAX_DEPTH,
  RULE_LIST_OPERATORS,
  RULE_NO_VALUE_OPERATORS,
  newEditableRuleGroup,
  newEditableRuleLeaf,
} from '@/utils/ruleConditions'

defineOptions({ name: 'RuleConditionNodeEditor' })

type NodeKind = 'leaf' | RuleGroupOperator
type LiteralKind = 'text' | 'number' | 'boolean' | 'json'

const props = withDefaults(defineProps<{
  modelValue: EditableRuleCondition
  fields?: string[]
  depth?: number
  removable?: boolean
}>(), {
  fields: () => [],
  depth: 0,
  removable: false,
})

const emit = defineEmits<{
  (event: 'update:modelValue', value: EditableRuleCondition): void
  (event: 'remove'): void
}>()

const operatorOptions: ReadonlyArray<{ label: string; value: RuleLeafOperator }> = [
  { label: '等于', value: '==' },
  { label: '不等于', value: '!=' },
  { label: '大于', value: '>' },
  { label: '大于等于', value: '>=' },
  { label: '小于', value: '<' },
  { label: '小于等于', value: '<=' },
  { label: '包含', value: 'contains' },
  { label: '不包含', value: 'not_contains' },
  { label: '属于列表', value: 'in' },
  { label: '不属于列表', value: 'not_in' },
  { label: '为空', value: 'is_null' },
  { label: '不为空', value: 'is_not_null' },
]

const jsonDraft = ref('')
const jsonError = ref('')

const isGroup = computed(() => 'conditions' in props.modelValue)
const leaf = computed<EditableRuleConditionLeaf | null>(() => (
  'conditions' in props.modelValue ? null : props.modelValue
))
const group = computed<EditableRuleConditionGroup | null>(() => (
  'conditions' in props.modelValue ? props.modelValue : null
))
const canNest = computed(() => props.depth < RULE_CONDITION_MAX_DEPTH)
const canAddChild = computed(() => Boolean(
  group.value
  && canNest.value
  && group.value.conditions.length < RULE_CONDITION_MAX_CHILDREN
  && (group.value.op !== 'not' || group.value.conditions.length === 0),
))

const nodeKind = computed<NodeKind>({
  get: () => 'conditions' in props.modelValue ? props.modelValue.op : 'leaf',
  set: (value) => {
    if (value === 'leaf') {
      emit('update:modelValue', newEditableRuleLeaf())
      return
    }
    const next = newEditableRuleGroup(value)
    next.conditions.push(newEditableRuleLeaf())
    emit('update:modelValue', next)
  },
})

const fieldModel = computed({
  get: () => leaf.value?.field || '',
  set: (field: string) => patchLeaf({ field }),
})
const operatorModel = computed<RuleLeafOperator>({
  get: () => leaf.value?.op || '==',
  set: (op) => {
    if (!leaf.value) return
    if (RULE_NO_VALUE_OPERATORS.has(op)) {
      emit('update:modelValue', { field: leaf.value.field, op })
      return
    }
    const source = Object.prototype.hasOwnProperty.call(leaf.value, 'value_field')
      ? { value_field: leaf.value.value_field || '' }
      : { value: leaf.value.value ?? '' }
    emit('update:modelValue', { field: leaf.value.field, op, ...source })
  },
})
const valueSource = computed<'literal' | 'field'>({
  get: () => leaf.value && Object.prototype.hasOwnProperty.call(leaf.value, 'value_field') ? 'field' : 'literal',
  set: (source) => {
    if (!leaf.value) return
    emit('update:modelValue', source === 'field'
      ? { field: leaf.value.field, op: leaf.value.op, value_field: '' }
      : { field: leaf.value.field, op: leaf.value.op, value: '' })
  },
})
const valueFieldModel = computed({
  get: () => leaf.value?.value_field || '',
  set: (value_field: string) => {
    if (!leaf.value) return
    emit('update:modelValue', { field: leaf.value.field, op: leaf.value.op, value_field })
  },
})
const literalValue = computed({
  get: () => leaf.value?.value,
  set: (value: unknown) => patchLiteral(value),
})
const literalKind = computed<LiteralKind>({
  get: () => inferLiteralKind(leaf.value?.value),
  set: (kind) => {
    if (kind === 'number') patchLiteral(RULE_LIST_OPERATORS.has(operatorModel.value) ? [] : 0)
    else if (kind === 'boolean') patchLiteral(RULE_LIST_OPERATORS.has(operatorModel.value) ? [] : false)
    else if (kind === 'json') patchLiteral(RULE_LIST_OPERATORS.has(operatorModel.value) ? [] : null)
    else patchLiteral(RULE_LIST_OPERATORS.has(operatorModel.value) ? [] : '')
  },
})

function patchLeaf(patch: Partial<{ field: string; op: RuleLeafOperator }>) {
  if (!leaf.value) return
  emit('update:modelValue', { ...leaf.value, ...patch })
}

function patchLiteral(value: unknown) {
  if (!leaf.value) return
  emit('update:modelValue', { field: leaf.value.field, op: leaf.value.op, value })
}

function inferLiteralKind(value: unknown): LiteralKind {
  if (typeof value === 'boolean') return 'boolean'
  if (typeof value === 'number') return 'number'
  if (typeof value === 'string') return 'text'
  if (Array.isArray(value) && value.every((item) => typeof item === 'string')) return 'text'
  if (Array.isArray(value) && value.every((item) => typeof item === 'number')) return 'number'
  if (Array.isArray(value) && value.every((item) => typeof item === 'boolean')) return 'boolean'
  return 'json'
}

function listDraft(value: unknown): string {
  if (!Array.isArray(value)) return ''
  return value.map((item) => String(item)).join(', ')
}

function updateListDraft(value: string) {
  const items = value.split(/[,，\n]/).map((item) => item.trim()).filter(Boolean)
  jsonError.value = ''
  if (literalKind.value === 'number') {
    const numbers = items.map(Number)
    if (numbers.some((item) => !Number.isFinite(item))) {
      jsonError.value = '列表中包含无效数值，已保留上次有效内容。'
      return
    }
    patchLiteral(numbers)
    return
  }
  if (literalKind.value === 'boolean') {
    const normalized = items.map((item) => item.toLowerCase())
    if (normalized.some((item) => !['true', 'false', '是', '否'].includes(item))) {
      jsonError.value = '布尔列表仅接受 true、false、是或否。'
      return
    }
    patchLiteral(normalized.map((item) => item === 'true' || item === '是'))
    return
  }
  patchLiteral(items)
}

function syncJsonDraft() {
  if (literalKind.value !== 'json') return
  jsonDraft.value = JSON.stringify(leaf.value?.value ?? null, null, 2)
  jsonError.value = ''
}

function applyJsonDraft() {
  try {
    patchLiteral(JSON.parse(jsonDraft.value))
    jsonError.value = ''
  } catch {
    jsonError.value = 'JSON 格式无效，已保留上次有效内容。'
  }
}

function updateChild(index: number, child: EditableRuleCondition) {
  if (!group.value) return
  const conditions = [...group.value.conditions]
  conditions[index] = child
  emit('update:modelValue', { op: group.value.op, conditions })
}

function removeChild(index: number) {
  if (!group.value) return
  emit('update:modelValue', {
    op: group.value.op,
    conditions: group.value.conditions.filter((_, childIndex) => childIndex !== index),
  })
}

function addChild(kind: NodeKind) {
  if (!group.value || !canAddChild.value) return
  const child = kind === 'leaf' ? newEditableRuleLeaf() : newEditableRuleGroup(kind)
  if ('conditions' in child) child.conditions.push(newEditableRuleLeaf())
  emit('update:modelValue', { op: group.value.op, conditions: [...group.value.conditions, child] })
}

watch(() => leaf.value?.value, syncJsonDraft, { immediate: true, deep: true })
</script>

<template>
  <div class="condition-node" :class="{ 'condition-node--group': isGroup }">
    <div class="node-head">
      <el-select v-model="nodeKind" class="node-kind" aria-label="条件节点类型">
        <el-option label="判断条件" value="leaf" />
        <el-option label="全部满足" value="and" />
        <el-option label="任一满足" value="or" />
        <el-option label="条件取反" value="not" />
      </el-select>
      <span v-if="isGroup" class="group-summary">
        {{ group?.op === 'and' ? '全部子条件满足' : group?.op === 'or' ? '任一子条件满足' : '对子条件取反' }}
      </span>
      <el-button v-if="removable" text type="danger" circle aria-label="删除该条件节点" @click="emit('remove')">
        <el-icon><Delete /></el-icon>
      </el-button>
    </div>

    <template v-if="leaf">
      <div class="leaf-fields">
        <el-select v-model="fieldModel" filterable allow-create default-first-option placeholder="选择或输入属性" aria-label="规则属性">
          <el-option v-for="field in fields" :key="field" :label="field" :value="field" />
        </el-select>
        <el-select v-model="operatorModel" aria-label="比较方式">
          <el-option v-for="operator in operatorOptions" :key="operator.value" :label="operator.label" :value="operator.value" />
        </el-select>
        <template v-if="!RULE_NO_VALUE_OPERATORS.has(operatorModel)">
          <el-select v-model="valueSource" aria-label="比较值来源">
            <el-option label="固定值" value="literal" />
            <el-option label="另一个属性" value="field" />
          </el-select>
          <el-select v-if="valueSource === 'field'" v-model="valueFieldModel" filterable allow-create default-first-option placeholder="用于比较的属性" aria-label="用于比较的另一个属性">
            <el-option v-for="field in fields.filter((item) => item !== fieldModel)" :key="field" :label="field" :value="field" />
          </el-select>
          <template v-else>
            <el-select v-model="literalKind" aria-label="固定值类型">
              <el-option label="文本" value="text" />
              <el-option label="数值" value="number" />
              <el-option label="是 / 否" value="boolean" />
              <el-option label="JSON" value="json" />
            </el-select>
            <el-input
              v-if="RULE_LIST_OPERATORS.has(operatorModel) && literalKind !== 'json'"
              :model-value="listDraft(literalValue)"
              placeholder="多个值用逗号分隔"
              aria-label="比较值列表"
              @input="updateListDraft"
            />
            <el-switch v-else-if="literalKind === 'boolean'" v-model="literalValue" inline-prompt active-text="是" inactive-text="否" aria-label="比较值" />
            <el-input-number v-else-if="literalKind === 'number'" v-model="literalValue" controls-position="right" aria-label="比较数值" />
            <el-input v-else-if="literalKind === 'text'" v-model="literalValue" placeholder="比较值" aria-label="比较值" />
            <el-input v-else v-model="jsonDraft" type="textarea" :rows="3" aria-label="JSON 比较值" @blur="applyJsonDraft" />
          </template>
        </template>
        <span v-else class="no-value">该判断无需比较值</span>
      </div>
      <div v-if="jsonError" class="condition-error" role="alert">{{ jsonError }}</div>
    </template>

    <template v-else-if="group">
      <div v-if="group.conditions.length" class="group-children">
        <RuleConditionNodeEditor
          v-for="(child, index) in group.conditions"
          :key="index"
          :model-value="child"
          :fields="fields"
          :depth="depth + 1"
          removable
          @update:model-value="updateChild(index, $event)"
          @remove="removeChild(index)"
        />
      </div>
      <div v-else class="condition-empty">该组合还没有子条件。</div>
      <div class="group-actions">
        <el-button plain :disabled="!canAddChild" @click="addChild('leaf')"><el-icon><Plus /></el-icon>添加条件</el-button>
        <el-button plain :disabled="!canAddChild" @click="addChild('and')"><el-icon><FolderAdd /></el-icon>添加组合</el-button>
        <el-button plain :disabled="!canAddChild" @click="addChild('not')"><el-icon><Switch /></el-icon>添加取反</el-button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.condition-node { display: grid; min-width: 0; gap: 8px; }
.condition-node--group { padding: 8px 0 8px 10px; border-left: 2px solid var(--border-strong); }
.node-head { display: flex; min-height: 32px; align-items: center; gap: 8px; }
.node-kind { width: 126px; flex: 0 0 126px; }
.group-summary { min-width: 0; flex: 1; color: var(--text-3); font-size: 12px; }
.node-head > .el-button { flex: 0 0 32px; }
.leaf-fields { display: grid; grid-template-columns: minmax(128px, 1fr) minmax(112px, .8fr) 104px 88px minmax(128px, 1fr); gap: 8px; align-items: center; }
.leaf-fields > * { min-width: 0; }
.no-value { grid-column: 3 / 6; color: var(--text-3); font-size: 12px; }
.group-children { display: grid; gap: 10px; }
.group-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.condition-empty { padding: 12px; border: 1px dashed var(--border-strong); border-radius: 6px; color: var(--text-3); font-size: 12px; text-align: center; }
.condition-error { color: var(--danger, #c45656); font-size: 12px; line-height: 1.5; }
@media (max-width: 760px) {
  .condition-node--group { padding-left: 8px; }
  .node-head { align-items: flex-start; }
  .node-kind { width: min(150px, calc(100% - 40px)); flex-basis: min(150px, calc(100% - 40px)); }
  .group-summary { display: none; }
  .leaf-fields { grid-template-columns: minmax(0, 1fr); }
  .no-value { grid-column: 1; }
  .group-actions :deep(.el-button) { min-height: 40px; margin-left: 0; }
}
</style>
