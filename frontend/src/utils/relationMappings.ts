import type { RelationDataMappingInput, RelationDataMappingMode } from '../types/index.ts'

export const RELATION_MAPPING_MODES: ReadonlyArray<{
  value: RelationDataMappingMode
  label: string
  description: string
}> = [
  {
    value: 'source_fk',
    label: '源对象表保存目标主键',
    description: '从源对象表选择一个外键列，用它连接目标对象的主键。',
  },
  {
    value: 'target_fk',
    label: '目标对象表保存源主键',
    description: '从目标对象表选择一个外键列，用它连接源对象的主键。',
  },
  {
    value: 'join_table',
    label: '中间表连接两端主键',
    description: '从中间表分别选择指向源对象和目标对象主键的列。',
  },
]

export function relationMappingModeLabel(mode: RelationDataMappingMode | string): string {
  return RELATION_MAPPING_MODES.find((item) => item.value === mode)?.label || '未知映射方式'
}

export function relationMappingModeDescription(mode: RelationDataMappingMode | string): string {
  return RELATION_MAPPING_MODES.find((item) => item.value === mode)?.description || ''
}

function clean(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

/** Build the backend's closed payload and remove fields belonging to another mode. */
export function buildRelationMappingPayload(
  value: Partial<RelationDataMappingInput>,
): RelationDataMappingInput {
  if (!RELATION_MAPPING_MODES.some((item) => item.value === value.mode)) {
    throw new Error('请选择关系映射方式')
  }
  const mode = value.mode as RelationDataMappingMode
  const common = {
    relation_id: clean(value.relation_id),
    source_mapping_id: clean(value.source_mapping_id),
    target_mapping_id: clean(value.target_mapping_id),
    mode,
  }
  if (mode === 'join_table') {
    return {
      ...common,
      foreign_key_column: '',
      join_data_source_id: clean(value.join_data_source_id),
      join_table_name: clean(value.join_table_name),
      source_key_column: clean(value.source_key_column),
      target_key_column: clean(value.target_key_column),
    }
  }
  return {
    ...common,
    foreign_key_column: clean(value.foreign_key_column),
    join_data_source_id: '',
    join_table_name: '',
    source_key_column: '',
    target_key_column: '',
  }
}

export function relationMappingPayloadFingerprint(value: Partial<RelationDataMappingInput>): string {
  return JSON.stringify(buildRelationMappingPayload(value))
}

export function missingRelationMappingFields(value: RelationDataMappingInput): string[] {
  const missing: string[] = []
  if (!value.relation_id) missing.push('关系类型')
  if (!value.source_mapping_id) missing.push('来源对象映射')
  if (!value.target_mapping_id) missing.push('目标对象映射')
  if (value.mode === 'join_table') {
    if (!value.join_data_source_id) missing.push('中间表数据源')
    if (!value.join_table_name) missing.push('中间表')
    if (!value.source_key_column) missing.push('来源主键列')
    if (!value.target_key_column) missing.push('目标主键列')
  } else if (!value.foreign_key_column) {
    missing.push('外键列')
  }
  return missing
}
