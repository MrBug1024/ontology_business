const INTERNAL_ORIGIN = 'https://codex.local'

/**
 * Accept only a same-origin router path beginning with exactly one slash.
 * Query values are untrusted input: encoded slashes, backslashes and control
 * characters are rejected before Vue Router sees them.
 */
export function safeInternalReturnPath(value: unknown, fallback = '/scenarios'): string {
  const raw = Array.isArray(value) ? value[0] : value
  if (typeof raw !== 'string') return fallback
  const candidate = raw.trim()
  if (!candidate.startsWith('/') || candidate.startsWith('//')) return fallback
  if (candidate.includes('\\') || /[\u0000-\u001f\u007f]/.test(candidate)) return fallback

  let decoded = candidate
  try {
    decoded = decodeURIComponent(candidate)
  } catch {
    return fallback
  }
  if (decoded.startsWith('//') || decoded.includes('\\') || /[\u0000-\u001f\u007f]/.test(decoded)) return fallback

  try {
    const parsed = new URL(candidate, INTERNAL_ORIGIN)
    if (parsed.origin !== INTERNAL_ORIGIN || !parsed.pathname.startsWith('/') || parsed.pathname.startsWith('//')) return fallback
    return `${parsed.pathname}${parsed.search}${parsed.hash}`
  } catch {
    return fallback
  }
}
