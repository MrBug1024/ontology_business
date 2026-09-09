function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null
}

export function capabilityInvocationId(tool: unknown): string {
  const call = record(tool)
  if (call?.name !== 'invoke_capability') return ''
  let value = call.result
  if (typeof value === 'string') {
    try { value = JSON.parse(value) as unknown } catch { return '' }
  }
  const result = record(value)
  return typeof result?.invocation_id === 'string' && /^[a-f0-9]{32}$/i.test(result.invocation_id)
    ? result.invocation_id : ''
}

export function receiptResourceId(value: unknown): string {
  return typeof value === 'string' && /^[a-f0-9]{32}$/i.test(value) ? value : ''
}
