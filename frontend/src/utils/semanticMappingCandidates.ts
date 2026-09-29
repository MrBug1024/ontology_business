import type { DatasetSchema, Entity, ScenarioModelDraftResource } from '@/types'

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

export function semanticMappingCandidateRows(candidates: ScenarioModelDraftResource[], entities: Entity[], schemas: DatasetSchema[]) {
  return candidates.slice(0, 25).map(candidate => {
    const payload = record(candidate.payload)
    const entity = entities.find(item => item.id === payload.entity_id)
    const schema = schemas.find(item => item.id === payload.dataset_schema_id)
    const relation = schema?.relations.find(item => item.id === payload.dataset_relation_id)
    const fields = (Array.isArray(payload.fields) ? payload.fields : []).slice(0, 500).map(value => {
      const field = record(value)
      return {
        property: entity?.properties.find(item => item.id === field.ontology_property_id)?.name || '待核对属性',
        source: relation?.fields.find(item => item.id === field.dataset_field_id)?.source_name || '待核对字段',
        required: field.is_required === true,
      }
    })
    return { id: candidate.id, title: candidate.title || '语义映射候选',
      entity: entity?.name || '待核对对象', source: relation?.display_name || '正在读取资料结构',
      operation: payload.existing_id ? '补齐已有映射' : '新增语义映射', fields,
      eligible: candidate.promotion_eligible === true }
  })
}
