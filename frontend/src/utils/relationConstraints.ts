import type { RelationConstraints } from '@/types'

const BOOLEAN_KEYS = [
  'symmetric',
  'transitive',
  'irreflexive',
  'asymmetric',
  'antisymmetric',
  'acyclic',
] as const

const CARDINALITY_KEYS = [
  'source_min_cardinality',
  'source_max_cardinality',
  'target_min_cardinality',
  'target_max_cardinality',
] as const

export type RelationConstraintForm = Required<Pick<RelationConstraints, typeof BOOLEAN_KEYS[number]>>
  & Record<typeof CARDINALITY_KEYS[number], number | null>
  & { inverse_relation_id: string }

export function relationConstraintForm(value: RelationConstraints | undefined): RelationConstraintForm {
  const source = value || {}
  return {
    symmetric: source.symmetric === true,
    transitive: source.transitive === true,
    irreflexive: source.irreflexive === true,
    asymmetric: source.asymmetric === true,
    antisymmetric: source.antisymmetric === true,
    acyclic: source.acyclic === true,
    inverse_relation_id: String(source.inverse_relation_id || ''),
    source_min_cardinality: source.source_min_cardinality ?? null,
    source_max_cardinality: source.source_max_cardinality ?? null,
    target_min_cardinality: source.target_min_cardinality ?? null,
    target_max_cardinality: source.target_max_cardinality ?? null,
  }
}

export function buildRelationConstraints(
  value: RelationConstraintForm,
  options: { relationType: string; sourceEntityId: string; targetEntityId: string },
): RelationConstraints {
  if (value.symmetric && value.asymmetric) throw new Error('同一关系不能同时设置为对称和非对称')
  if (value.symmetric && value.antisymmetric) throw new Error('同一关系不能同时设置为对称和反对称')
  const sameTypeAxiom = BOOLEAN_KEYS.some((key) => key !== 'irreflexive' && value[key])
  if (sameTypeAxiom && options.sourceEntityId !== options.targetEntityId) {
    throw new Error('对称、传递、非对称、反对称和无环只适用于源/目标相同的对象类型')
  }

  const result: RelationConstraints = {}
  for (const key of BOOLEAN_KEYS) {
    if (value[key]) result[key] = true
  }
  if (value.asymmetric || value.acyclic) result.irreflexive = true
  for (const key of CARDINALITY_KEYS) {
    const cardinality = value[key]
    if (cardinality === null || cardinality === undefined) continue
    if (!Number.isInteger(cardinality) || cardinality < 0) throw new Error('关系最小/最大基数必须是大于等于 0 的整数')
    result[key] = cardinality
  }
  for (const side of ['source', 'target'] as const) {
    const minimum = result[`${side}_min_cardinality`]
    const maximum = result[`${side}_max_cardinality`]
    if (minimum !== undefined && maximum !== undefined && minimum > maximum) {
      throw new Error(`${side === 'source' ? '源对象' : '目标对象'}最小基数不能大于最大基数`)
    }
  }
  const implicitSourceMax = ['1:1', 'N:1'].includes(options.relationType) ? 1 : undefined
  const implicitTargetMax = ['1:1', '1:N'].includes(options.relationType) ? 1 : undefined
  if (implicitSourceMax !== undefined && (result.source_min_cardinality ?? 0) > implicitSourceMax) {
    throw new Error(`源对象最小基数与关系基数 ${options.relationType} 冲突`)
  }
  if (implicitTargetMax !== undefined && (result.target_min_cardinality ?? 0) > implicitTargetMax) {
    throw new Error(`目标对象最小基数与关系基数 ${options.relationType} 冲突`)
  }
  const inverseId = value.inverse_relation_id.trim()
  if (inverseId) result.inverse_relation_id = inverseId
  return result
}
