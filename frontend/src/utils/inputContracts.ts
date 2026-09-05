export const INPUT_CONTRACT_KEY = 'x-platform-input-contract'
export const INPUT_CONTRACT_VERSION = 'tabular-content/v1'
export const MAX_INPUT_CONTRACT_RELATIONS = 64
export const MAX_INPUT_CONTRACT_FIELDS = 512

export type InputContractSource = 'dataset_schema' | 'manual'
export type InputContractLogicalType =
  | 'boolean'
  | 'date'
  | 'datetime'
  | 'integer'
  | 'null'
  | 'number'
  | 'object'
  | 'string'
  | 'unknown'

const INPUT_CONTRACT_LOGICAL_TYPES = new Set<InputContractLogicalType>([
  'boolean',
  'date',
  'datetime',
  'integer',
  'null',
  'number',
  'object',
  'string',
  'unknown',
])

export interface InputContractFieldDraft {
  clientId: string
  name: string
  logicalType: InputContractLogicalType
  required: boolean
}

export interface InputContractRelationDraft {
  clientId: string
  minimumDataRows: number
  allowAdditionalFields: boolean
  fields: InputContractFieldDraft[]
}

export interface InputContractDraft {
  enabled: boolean
  source: InputContractSource
  datasetId: string
  datasetSchemaId: string
  allowAdditionalRelations: boolean
  relations: InputContractRelationDraft[]
}

export interface InputContractSubmission {
  dataset_id: string | null
  dataset_schema_id: string | null
  schema_document: Record<string, unknown>
}

let clientSequence = 0

function nextClientId(prefix: string) {
  clientSequence += 1
  return `${prefix}-${clientSequence}`
}

export function createInputContractField(): InputContractFieldDraft {
  return {
    clientId: nextClientId('field'),
    name: '',
    logicalType: 'string',
    required: true,
  }
}

export function createInputContractRelation(): InputContractRelationDraft {
  return {
    clientId: nextClientId('relation'),
    minimumDataRows: 1,
    allowAdditionalFields: true,
    fields: [createInputContractField()],
  }
}

export function createInputContractDraft(
  source: InputContractSource = 'manual',
): InputContractDraft {
  return {
    enabled: false,
    source,
    datasetId: '',
    datasetSchemaId: '',
    allowAdditionalRelations: true,
    relations: [createInputContractRelation()],
  }
}

function normalizedFieldName(value: string) {
  return value.normalize('NFKC').trim().toLowerCase().replace(/\s+/g, ' ')
}

export function buildInputContractSubmission(
  draft: InputContractDraft,
): InputContractSubmission {
  if (!draft.enabled) {
    return { dataset_id: null, dataset_schema_id: null, schema_document: {} }
  }
  if (draft.source === 'dataset_schema') {
    const datasetId = draft.datasetId.trim()
    const datasetSchemaId = draft.datasetSchemaId.trim()
    if (!datasetId || !datasetSchemaId) {
      throw new Error('请选择建模资料中的逻辑数据集和 Dataset Schema。')
    }
    return {
      dataset_id: datasetId,
      dataset_schema_id: datasetSchemaId,
      schema_document: {},
    }
  }
  if (
    draft.relations.length < 1
    || draft.relations.length > MAX_INPUT_CONTRACT_RELATIONS
  ) {
    throw new Error(`手工契约必须包含 1-${MAX_INPUT_CONTRACT_RELATIONS} 个关系。`)
  }
  const totalFields = draft.relations.reduce(
    (total, relation) => total + relation.fields.length,
    0,
  )
  if (totalFields < 1 || totalFields > MAX_INPUT_CONTRACT_FIELDS) {
    throw new Error(`手工契约最多可定义 ${MAX_INPUT_CONTRACT_FIELDS} 个字段。`)
  }

  const relations = draft.relations.map((relation, relationIndex) => {
    if (!Number.isInteger(relation.minimumDataRows)
      || relation.minimumDataRows < 0
      || relation.minimumDataRows > 1_000_000_000) {
      throw new Error(`关系 ${relationIndex + 1} 的最小数据行数无效。`)
    }
    if (!relation.fields.length) {
      throw new Error(`关系 ${relationIndex + 1} 至少需要一个字段。`)
    }
    const claimedNames = new Set<string>()
    const fields = relation.fields.map((field, fieldIndex) => {
      const name = field.name.trim()
      const normalized = normalizedFieldName(name)
      if (!name || name.length > 180) {
        throw new Error(`关系 ${relationIndex + 1} 的字段 ${fieldIndex + 1} 名称无效。`)
      }
      if (claimedNames.has(normalized)) {
        throw new Error(`关系 ${relationIndex + 1} 包含重复字段“${name}”。`)
      }
      if (!INPUT_CONTRACT_LOGICAL_TYPES.has(field.logicalType)) {
        throw new Error(`关系 ${relationIndex + 1} 的字段“${name}”类型无效。`)
      }
      claimedNames.add(normalized)
      return {
        name,
        logical_types: [field.logicalType],
        required: field.required,
      }
    })
    return {
      fields,
      minimum_data_rows: relation.minimumDataRows,
      allow_additional_fields: relation.allowAdditionalFields,
    }
  })

  return {
    dataset_id: null,
    dataset_schema_id: null,
    schema_document: {
      [INPUT_CONTRACT_KEY]: {
        version: INPUT_CONTRACT_VERSION,
        relations,
        allow_additional_relations: draft.allowAdditionalRelations,
      },
    },
  }
}
