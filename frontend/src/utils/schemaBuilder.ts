export type SchemaObject = Record<string, any>

export type EditableSchemaField = {
  name: string
  type: string
  description: string
  required: boolean
  enumText: string
  extras?: SchemaObject
}

function isObject(value: unknown): value is SchemaObject {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function editableType(schema: SchemaObject) {
  if (schema.type === 'string' && schema.format === 'date') return 'date'
  if (schema.type === 'string' && schema.format === 'date-time') return 'datetime'
  return String(schema.type || 'any')
}

function fieldExtras(schema: SchemaObject): SchemaObject {
  const extras: SchemaObject = {}
  for (const [key, value] of Object.entries(schema)) {
    if (['type', 'format', 'description', 'enum', 'properties', 'required', 'items'].includes(key)) continue
    extras[key] = structuredClone(value)
  }
  // Scalar arrays cannot be expressed as a separate child row, so retain the
  // item schema as an opaque constraint. Object-array children are rebuilt
  // from paths such as `lines.0.product_name`.
  if (schema.type === 'array' && isObject(schema.items)) {
    const itemProperties = isObject(schema.items.properties) ? schema.items.properties : {}
    if (schema.items.type !== 'object' || !Object.keys(itemProperties).length) {
      extras.items = structuredClone(schema.items)
    }
  }
  return extras
}

/** Flatten JSON Schema without discarding object/array nesting. */
export function flattenSchemaFields(value: SchemaObject): EditableSchemaField[] {
  const fields: EditableSchemaField[] = []
  const root = isObject(value?.properties)
    ? value
    : { type: 'object', properties: isObject(value) ? value : {}, required: [] }

  function visit(schema: SchemaObject, path: string, required: boolean) {
    const type = String(schema.type || (isObject(schema.properties) ? 'object' : 'string'))
    if (path) {
      fields.push({
        name: path,
        type: editableType({ ...schema, type }),
        description: String(schema.description || ''),
        required,
        enumText: Array.isArray(schema.enum) ? schema.enum.join(', ') : '',
        extras: fieldExtras({ ...schema, type }),
      })
    }
    if (type === 'object' && isObject(schema.properties)) {
      const requiredNames = new Set(Array.isArray(schema.required) ? schema.required.map(String) : [])
      for (const [name, child] of Object.entries(schema.properties)) {
        visit(isObject(child) ? child : { type: 'string' }, path ? `${path}.${name}` : name, requiredNames.has(name))
      }
    }
    if (type === 'array' && isObject(schema.items)) {
      const items = schema.items
      if (String(items.type || '') === 'object' && isObject(items.properties)) {
        const requiredNames = new Set(Array.isArray(items.required) ? items.required.map(String) : [])
        for (const [name, child] of Object.entries(items.properties)) {
          visit(isObject(child) ? child : { type: 'string' }, `${path}.0.${name}`, requiredNames.has(name))
        }
      }
    }
  }

  const rootRequired = new Set(Array.isArray(root.required) ? root.required.map(String) : [])
  for (const [name, child] of Object.entries(root.properties || {})) {
    visit(isObject(child) ? child : { type: 'string' }, name, rootRequired.has(name))
  }
  return fields
}

function castEnumValue(value: string, type: string) {
  const trimmed = value.trim()
  if (type === 'integer') return Number.parseInt(trimmed, 10)
  if (type === 'number') return Number(trimmed)
  if (type === 'boolean') return trimmed === 'true' ? true : trimmed === 'false' ? false : trimmed
  return trimmed
}

function normalizeRequired(schema: SchemaObject) {
  if (!Array.isArray(schema.required)) schema.required = []
  if (!isObject(schema.properties)) schema.properties = {}
  if (schema.additionalProperties === undefined) schema.additionalProperties = false
}

function setRequired(parent: SchemaObject, name: string, required: boolean) {
  normalizeRequired(parent)
  const names = new Set(parent.required.map(String))
  if (required) names.add(name)
  else names.delete(name)
  parent.required = [...names]
}

function fieldSchema(field: EditableSchemaField, existing: SchemaObject): SchemaObject {
  const type = field.type === 'date' || field.type === 'datetime' ? 'string' : field.type
  const schema: SchemaObject = { ...(field.extras || {}) }
  if (type !== 'any') schema.type = type
  if (field.type === 'date') schema.format = 'date'
  if (field.type === 'datetime') schema.format = 'date-time'
  if (field.description.trim()) schema.description = field.description.trim()
  const enumValues = field.enumText.split(/[,，\n]/).map((item) => item.trim()).filter(Boolean)
  if (enumValues.length) schema.enum = enumValues.map((item) => castEnumValue(item, field.type))
  if (type === 'object') {
    schema.properties = isObject(existing.properties) ? existing.properties : {}
    schema.required = Array.isArray(existing.required) ? existing.required : []
    if (schema.additionalProperties === undefined) schema.additionalProperties = false
  }
  if (type === 'array') {
    schema.items = isObject(existing.items)
      ? existing.items
      : isObject(schema.items) ? schema.items : { type: 'string' }
  }
  return schema
}

/** Rebuild JSON Schema from dotted paths; numeric segments address array items. */
export function buildSchemaFromFields(fields: EditableSchemaField[]): SchemaObject {
  const root: SchemaObject = { type: 'object', properties: {}, required: [], additionalProperties: false }
  const ordered = [...fields]
    .filter((field) => field.name.trim())
    .sort((left, right) => left.name.split('.').length - right.name.split('.').length)

  for (const field of ordered) {
    const tokens = field.name.trim().split('.').map((token) => token.trim()).filter(Boolean)
    if (!tokens.length || /^\d+$/.test(tokens[0])) continue
    let current = root
    for (let index = 0; index < tokens.length; index += 1) {
      const token = tokens[index]
      const last = index === tokens.length - 1
      if (/^\d+$/.test(token)) {
        if (current.type !== 'array') current.type = 'array'
        if (!isObject(current.items)) current.items = { type: 'object', properties: {}, required: [], additionalProperties: false }
        current = current.items
        continue
      }

      if (current.type !== 'object') current.type = 'object'
      normalizeRequired(current)
      const existing = isObject(current.properties[token]) ? current.properties[token] : {}
      if (last) {
        current.properties[token] = fieldSchema(field, existing)
        setRequired(current, token, field.required)
        continue
      }

      const nextIsIndex = /^\d+$/.test(tokens[index + 1])
      if (!Object.keys(existing).length) {
        current.properties[token] = nextIsIndex
          ? { type: 'array', items: { type: 'object', properties: {}, required: [], additionalProperties: false } }
          : { type: 'object', properties: {}, required: [], additionalProperties: false }
      } else if (nextIsIndex) {
        existing.type = 'array'
        if (!isObject(existing.items)) existing.items = { type: 'object', properties: {}, required: [], additionalProperties: false }
      } else {
        existing.type = 'object'
        normalizeRequired(existing)
      }
      current = current.properties[token]
    }
  }
  return root
}
