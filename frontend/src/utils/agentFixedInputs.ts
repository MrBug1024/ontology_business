export type FixedInputResult = { inputs?: Record<string, unknown>; error: string }
export const MAX_FIXED_INPUT_BYTES = 65536

export function parseFixedBusinessInputs(enabled: boolean, source: string): FixedInputResult {
  if (!enabled) return { error: '' }
  if (!source.trim()) return { error: '请填写本次输入对象，或关闭固定输入' }
  if (new TextEncoder().encode(source).length > MAX_FIXED_INPUT_BYTES) return { error: '本次输入对象超过 64 KB' }
  let value: unknown
  try { value = JSON.parse(source) } catch { return { error: '请输入有效的 JSON 对象' } }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return { error: '本次输入必须是对象' }
  let count = 0
  function bounded(item: unknown, depth: number): boolean {
    if (++count > 10000 || depth > 16) return false
    if (typeof item === 'number') return Number.isFinite(item)
    if (Array.isArray(item)) return item.length <= 1000 && item.every(child => bounded(child, depth + 1))
    if (item && typeof item === 'object') return Object.entries(item).every(([key, child]) =>
      !['__proto__', 'constructor', 'prototype'].includes(key) && bounded(child, depth + 1))
    return true
  }
  if (!bounded(value, 0)) return { error: '输入对象含无效数值、字段或超过结构上限' }
  return { inputs: value as Record<string, unknown>, error: '' }
}
