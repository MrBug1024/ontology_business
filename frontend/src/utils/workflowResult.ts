type ResultRow = { path: string; value: string }

function objectValue(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

export function workflowResultSchema(config: unknown, nodeId: string): Record<string, unknown> {
  if (!objectValue(config) || !objectValue(config.ontology_contract)) return {}
  const contract = config.ontology_contract
  const declared = contract.output_node_id === nodeId || Array.isArray(contract.output_node_ids) && contract.output_node_ids.includes(nodeId)
  return declared && objectValue(contract.output_schema) ? contract.output_schema : {}
}

/** Only server-confirmed successful end nodes are final business results. */
export function workflowBusinessOutputs(status: string, result: unknown): { name: string; value: unknown }[] {
  if (status !== 'succeeded' || !objectValue(result) || !Array.isArray(result.steps)) return []
  return result.steps.filter((step): step is Record<string, unknown> =>
    objectValue(step) && step.type === 'end' && step.status === 'success'
    && Object.prototype.hasOwnProperty.call(step, 'result'),
  ).slice(0, 32).map(step => ({
    name: typeof step.name === 'string' ? step.name : '结束节点',
    value: step.result,
  }))
}

/** Bounded display only; the stored structure is never converted for saving. */
export function workflowResultRows(value: unknown): ResultRow[] {
  const rows: ResultRow[] = []
  function visit(item: unknown, path: string, depth: number) {
    if (rows.length >= 64) return
    if (depth > 12) {
      rows.push({ path, value: '嵌套内容，请通过顾问查看或调整' })
    } else if (Array.isArray(item) && item.length) {
      item.slice(0, 64).forEach((child, index) => visit(child, `${path}[${index}]`, depth + 1))
    } else if (objectValue(item) && Object.keys(item).length) {
      Object.entries(item).slice(0, 64).forEach(([key, child]) => visit(child, path ? `${path}.${key}` : key, depth + 1))
    } else {
      const text = item === null ? '空值' : Array.isArray(item) ? '空列表' : objectValue(item) ? '空对象' : String(item ?? '')
      rows.push({ path: path || '结果', value: text.length > 1000 ? `${text.slice(0, 1000)}…` : text })
    }
  }
  if (value !== undefined) visit(value, '', 0)
  return rows
}
