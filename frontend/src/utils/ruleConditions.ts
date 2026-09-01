import type {
  EditableRuleCondition,
  EditableRuleConditionGroup,
  RuleCondition,
  RuleConditionGroup,
  RuleConditionLeaf,
  RuleGroupOperator,
  RuleLeafOperator,
} from '../types/ruleConditions.ts'

export const RULE_CONDITION_MAX_DEPTH = 8
export const RULE_CONDITION_MAX_CHILDREN = 50

export const RULE_LEAF_OPERATORS: readonly RuleLeafOperator[] = [
  '==',
  '!=',
  '>',
  '>=',
  '<',
  '<=',
  'contains',
  'not_contains',
  'in',
  'not_in',
  'is_null',
  'is_not_null',
]

export const RULE_GROUP_OPERATORS: readonly RuleGroupOperator[] = ['and', 'or', 'not']
export const RULE_NO_VALUE_OPERATORS = new Set<RuleLeafOperator>(['is_null', 'is_not_null'])
export const RULE_LIST_OPERATORS = new Set<RuleLeafOperator>(['in', 'not_in'])

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function owns(value: object, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(value, key)
}

function cloneJsonValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(cloneJsonValue)
  if (!isRecord(value)) return value
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, cloneJsonValue(item)]))
}

function isLeafOperator(value: unknown): value is RuleLeafOperator {
  return typeof value === 'string' && RULE_LEAF_OPERATORS.includes(value as RuleLeafOperator)
}

function isGroupOperator(value: unknown): value is RuleGroupOperator {
  return typeof value === 'string' && RULE_GROUP_OPERATORS.includes(value as RuleGroupOperator)
}

export function parseRuleCondition(value: unknown, depth = 0): RuleCondition | null {
  if (depth > RULE_CONDITION_MAX_DEPTH || !isRecord(value)) return null
  const op = value.op
  if (isGroupOperator(op)) {
    if (
      !Array.isArray(value.conditions)
      || value.conditions.length < 1
      || value.conditions.length > RULE_CONDITION_MAX_CHILDREN
      || (op === 'not' && value.conditions.length !== 1)
    ) return null
    const conditions = value.conditions.map((item) => parseRuleCondition(item, depth + 1))
    if (conditions.some((item) => item === null)) return null
    return { op, conditions: conditions as RuleCondition[] }
  }
  if (!isLeafOperator(op) || typeof value.field !== 'string' || !value.field.trim()) return null
  const leaf: RuleConditionLeaf = { field: value.field.trim(), op }
  if (RULE_NO_VALUE_OPERATORS.has(op)) return leaf
  const hasValue = owns(value, 'value')
  const hasValueField = owns(value, 'value_field')
  if (hasValue === hasValueField) return null
  if (hasValueField) {
    if (typeof value.value_field !== 'string' || !value.value_field.trim()) return null
    leaf.value_field = value.value_field.trim()
  } else {
    leaf.value = cloneJsonValue(value.value)
  }
  return leaf
}

export function newEditableRuleLeaf(): EditableRuleCondition {
  return { field: '', op: '==', value: '' }
}

export function newEditableRuleGroup(op: RuleGroupOperator = 'and'): EditableRuleConditionGroup {
  return { op, conditions: [] }
}

export function editableRuleCondition(value: RuleCondition): EditableRuleCondition {
  if ('conditions' in value) {
    return {
      op: value.op,
      conditions: value.conditions.map(editableRuleCondition),
    }
  }
  return {
    field: value.field,
    op: value.op,
    ...(owns(value, 'value_field') ? { value_field: value.value_field } : {}),
    ...(owns(value, 'value') ? { value: cloneJsonValue(value.value) } : {}),
  }
}

export function serializeRuleCondition(
  value: EditableRuleCondition,
  depth = 0,
): RuleCondition | null {
  if (depth > RULE_CONDITION_MAX_DEPTH) return null
  if ('conditions' in value) {
    const conditions = value.conditions
      .slice(0, RULE_CONDITION_MAX_CHILDREN)
      .map((item) => serializeRuleCondition(item, depth + 1))
      .filter((item): item is RuleCondition => item !== null)
    if (!conditions.length || (value.op === 'not' && conditions.length !== 1)) return null
    const group: RuleConditionGroup = { op: value.op, conditions }
    return group
  }
  if (!value.field.trim() || !isLeafOperator(value.op)) return null
  const leaf: RuleConditionLeaf = { field: value.field.trim(), op: value.op }
  if (RULE_NO_VALUE_OPERATORS.has(value.op)) return leaf
  if (owns(value, 'value_field')) {
    if (typeof value.value_field !== 'string' || !value.value_field.trim()) return null
    leaf.value_field = value.value_field.trim()
    return leaf
  }
  if (!owns(value, 'value')) return null
  leaf.value = cloneJsonValue(value.value)
  return leaf
}

export function countRuleConditionLeaves(value: EditableRuleCondition): number {
  if ('conditions' in value) {
    return value.conditions.reduce((total, child) => total + countRuleConditionLeaves(child), 0)
  }
  return value.field.trim() ? 1 : 0
}

