import { toRaw } from 'vue'

/** Clone form data without passing Vue reactive proxies to structuredClone. */
export function cloneForForm<T>(value: T, fallback?: T): T {
  if (value === null || value === undefined) return value

  const raw = toRaw(value as any) as T
  try {
    return structuredClone(raw)
  } catch {
    try {
      return JSON.parse(JSON.stringify(raw)) as T
    } catch {
      if (fallback !== undefined) return fallback
      if (Array.isArray(raw)) return [] as T
      if (typeof raw === 'object') return {} as T
      return raw
    }
  }
}
