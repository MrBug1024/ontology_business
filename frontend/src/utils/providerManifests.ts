import type { FunctionProviderManifest } from '@/types'

type JsonObject = Record<string, unknown>

export interface FunctionContractSchemas {
  input_schema: JsonObject
  output_schema: JsonObject
}

function objectValue(value: unknown): JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as JsonObject
    : {}
}

function cloneObject(value: JsonObject): JsonObject {
  return JSON.parse(JSON.stringify(value)) as JsonObject
}

function emptyFunctionSchema(): JsonObject {
  return {
    type: 'object',
    properties: {},
    additionalProperties: false,
  }
}

export function captureFunctionContractSchemas(
  inputSchema: unknown,
  outputSchema: unknown,
): FunctionContractSchemas {
  return {
    input_schema: cloneObject(objectValue(inputSchema)),
    output_schema: cloneObject(objectValue(outputSchema)),
  }
}

export function restoreFunctionContractSchemas(
  snapshot: FunctionContractSchemas | null,
): FunctionContractSchemas {
  return snapshot
    ? captureFunctionContractSchemas(snapshot.input_schema, snapshot.output_schema)
    : {
        input_schema: emptyFunctionSchema(),
        output_schema: emptyFunctionSchema(),
      }
}

export function functionRuntimeConfigForSave(
  runtimeKind: unknown,
  runtimeConfig: unknown,
): JsonObject {
  return runtimeKind === 'contract'
    ? {}
    : cloneObject(objectValue(runtimeConfig))
}

export function providerManifestIdentity(
  providerKey: unknown,
  providerVersion: unknown,
): string {
  const key = String(providerKey || '').trim().toLowerCase()
  const version = String(providerVersion || '').trim()
  return key && version ? `${key}@${version}` : ''
}

export function runtimeProviderIdentity(runtimeConfig: unknown): string {
  const config = objectValue(runtimeConfig)
  return providerManifestIdentity(config.provider_key, config.provider_version)
}

export function createProviderRuntimeConfig(
  manifest: FunctionProviderManifest,
): JsonObject {
  return {
    provider_key: manifest.provider_key,
    provider_version: manifest.provider_version,
    provider_config: cloneObject(manifest.default_config),
  }
}

export function providerConfigValidationError(
  manifest: FunctionProviderManifest,
  runtimeConfig: unknown,
): string {
  const runtime = objectValue(runtimeConfig)
  if (runtimeProviderIdentity(runtime) !== providerManifestIdentity(
    manifest.provider_key,
    manifest.provider_version,
  )) return '受信 Provider 身份与当前选择不一致'

  const config = objectValue(runtime.provider_config)
  const schema = objectValue(manifest.config_schema)
  const properties = objectValue(schema.properties)
  const unknownFields = Object.keys(config).filter((key) => !(key in properties))
  if (unknownFields.length) return `Provider 配置包含不支持的字段：${unknownFields.join('、')}`

  const required = Array.isArray(schema.required)
    ? schema.required.filter((item): item is string => typeof item === 'string')
    : []
  for (const fieldName of required) {
    const value = config[fieldName]
    if (value === undefined || value === null || value === '') {
      return `请填写 ${manifest.ui_schema[fieldName]?.label || fieldName}`
    }
    if (Array.isArray(value) && value.length === 0) {
      return `请至少选择一项 ${manifest.ui_schema[fieldName]?.label || fieldName}`
    }
  }

  for (const [fieldName, rawFieldSchema] of Object.entries(properties)) {
    const fieldSchema = objectValue(rawFieldSchema)
    const value = config[fieldName]
    if (value === undefined) continue
    if ('const' in fieldSchema && value !== fieldSchema.const) {
      return `${manifest.ui_schema[fieldName]?.label || fieldName} 必须使用受信清单固定值`
    }
    if (Array.isArray(fieldSchema.enum) && !fieldSchema.enum.includes(value)) {
      return `${manifest.ui_schema[fieldName]?.label || fieldName} 不在允许范围内`
    }
    if (fieldSchema.type === 'array' && !Array.isArray(value)) {
      return `${manifest.ui_schema[fieldName]?.label || fieldName} 必须是列表`
    }
    if (fieldSchema.type === 'string' && typeof value !== 'string') {
      return `${manifest.ui_schema[fieldName]?.label || fieldName} 必须是文本`
    }
    if (fieldSchema.type === 'boolean' && typeof value !== 'boolean') {
      return `${manifest.ui_schema[fieldName]?.label || fieldName} 必须是开关值`
    }
    if (
      (fieldSchema.type === 'number' || fieldSchema.type === 'integer')
      && (typeof value !== 'number' || !Number.isFinite(value))
    ) return `${manifest.ui_schema[fieldName]?.label || fieldName} 必须是有效数字`
  }
  return ''
}
