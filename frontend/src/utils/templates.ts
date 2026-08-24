import type { ArtifactTemplate, ArtifactTemplateFormat } from '../types/index.ts'

export const TEMPLATE_FILE_ACCEPT = '.docx,.xlsx,.md,.markdown'

export function isSupportedTemplateFilename(filename: string) {
  return /\.(docx|xlsx|md|markdown)$/i.test(String(filename || '').trim())
}

export function templateFormatLabel(format?: string) {
  return ({ docx: 'Word', xlsx: 'Excel', markdown: 'Markdown' } as Record<string, string>)[String(format || '').toLowerCase()] || '未知格式'
}

export function templateFormatTagType(format?: ArtifactTemplateFormat | string) {
  if (format === 'docx') return 'primary'
  if (format === 'xlsx') return 'success'
  return 'info'
}

export function templateUnavailableReason(template?: ArtifactTemplate | null) {
  if (!template) return '模板不存在或当前无权访问'
  if (template.status !== 'active') return '模板已停用，不能建立新的操作绑定'
  if (!template.current_version) return '模板没有可用版本'
  return ''
}

export function isTemplateBucketInScope(bucketScenarioId?: string | null, templateScenarioId?: string | null) {
  if (!templateScenarioId) return !bucketScenarioId
  return !bucketScenarioId || bucketScenarioId === templateScenarioId
}

export function templatePathsToSchema(paths: string[]) {
  const unsafeSegments = new Set(['__proto__', 'prototype', 'constructor'])
  const createProperties = () => Object.create(null) as Record<string, Record<string, any>>
  const createNode = () => ({} as Record<string, any>)
  const hasOwn = (value: object, key: string) => Object.prototype.hasOwnProperty.call(value, key)
  const validatedParts = (path: string) => {
    const normalized = String(path || '').trim()
    const parts = normalized.split('.').map((part) => part.trim())
    if (
      !normalized
      || normalized.length > 500
      || parts.length > 16
      || parts.some((part) => !part || part.length > 200 || [...part].some((character) => character.charCodeAt(0) < 32))
      || parts.some((part) => unsafeSegments.has(part.toLowerCase()))
    ) throw new Error(`模板变量路径无效或不安全：${normalized.slice(0, 120)}`)
    return parts
  }
  const ensureObject = (node: Record<string, any>, path: string) => {
    if (node.type !== undefined && node.type !== 'object') throw new Error(`模板变量 ${path} 与输入参数类型冲突，应为对象`)
    node.type = 'object'
    node.properties ||= createProperties()
    node.required ||= []
    if (!node.properties || typeof node.properties !== 'object' || Array.isArray(node.properties) || !Array.isArray(node.required)) {
      throw new Error(`模板变量 ${path} 的输入参数 Schema 无效`)
    }
    node.additionalProperties ??= false
    return { properties: node.properties as Record<string, Record<string, any>>, required: node.required as string[] }
  }
  const ensureArray = (node: Record<string, any>, path: string, minimum: number) => {
    if (node.type !== undefined && node.type !== 'array') throw new Error(`模板变量 ${path} 与输入参数类型冲突，应为列表`)
    node.type = 'array'
    node.minItems = Math.max(Number(node.minItems || 0), minimum)
    node.items ||= createNode()
    if (!node.items || typeof node.items !== 'object' || Array.isArray(node.items)) throw new Error(`模板变量 ${path} 的列表项 Schema 无效`)
    return node.items as Record<string, any>
  }
  const addPath = (node: Record<string, any>, parts: string[], fullPath: string): void => {
    const { properties, required } = ensureObject(node, fullPath)
    const name = parts[0]
    if (!name || /^\d+$/.test(name)) throw new Error(`模板变量路径无效：${fullPath}`)
    if (!hasOwn(properties, name)) properties[name] = createNode()
    const child = properties[name]
    if (!child || typeof child !== 'object' || Array.isArray(child)) throw new Error(`模板变量 ${fullPath} 的字段 Schema 无效`)
    if (!required.includes(name)) required.push(name)
    if (parts.length === 1) {
      child.description ??= `模板变量：${fullPath}`
      return
    }
    if (/^\d+$/.test(parts[1])) {
      const index = Number(parts[1])
      const items = ensureArray(child, fullPath, index + 1)
      if (parts.length === 2) {
        items.description ??= `模板变量：${fullPath}`
        return
      }
      addPath(items, parts.slice(2), fullPath)
      return
    }
    addPath(child, parts.slice(1), fullPath)
  }

  const root: Record<string, any> = {
    type: 'object', properties: createProperties(), required: [], additionalProperties: false,
  }
  const normalized = [...new Set((paths || []).map((path) => String(path || '').trim()).filter(Boolean))].sort()
  for (const path of normalized) addPath(root, validatedParts(path), path)
  // Return ordinary JSON objects only after every user-controlled key was
  // validated and constructed inside null-prototype property dictionaries.
  return JSON.parse(JSON.stringify(root)) as Record<string, any>
}

export function cleanTemplateExecutorConfig(config: Record<string, any>, templateId: string, version?: number | '') {
  const next: Record<string, any> = {
    template_id: templateId,
    target_data_source_id: String(config?.target_data_source_id || ''),
    output_filename: String(config?.output_filename || ''),
  }
  if (typeof version === 'number' && Number.isInteger(version) && version > 0) next.template_version = version
  return next
}
