export type RuleLeafOperator =
  | '=='
  | '!='
  | '>'
  | '>='
  | '<'
  | '<='
  | 'contains'
  | 'not_contains'
  | 'in'
  | 'not_in'
  | 'is_null'
  | 'is_not_null'

export type RuleGroupOperator = 'and' | 'or' | 'not'

export interface RuleConditionLeaf {
  field: string
  op: RuleLeafOperator
  value?: unknown
  value_field?: string
}

export interface RuleConditionGroup {
  op: RuleGroupOperator
  conditions: RuleCondition[]
}

export type RuleCondition = RuleConditionLeaf | RuleConditionGroup

export interface EditableRuleConditionLeaf {
  field: string
  op: RuleLeafOperator
  value?: unknown
  value_field?: string
}

export interface EditableRuleConditionGroup {
  op: RuleGroupOperator
  conditions: EditableRuleCondition[]
}

export type EditableRuleCondition = EditableRuleConditionLeaf | EditableRuleConditionGroup

