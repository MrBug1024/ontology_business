type ClientCrypto = {
  randomUUID?: () => string
  getRandomValues?: (array: Uint8Array) => Uint8Array
}

let fallbackCounter = 0

function formatBytes(bytes: Uint8Array): string {
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export function createClientRequestId(source: ClientCrypto | undefined = globalThis.crypto): string {
  if (typeof source?.randomUUID === 'function') return source.randomUUID()
  if (typeof source?.getRandomValues === 'function') {
    const bytes = new Uint8Array(16)
    source.getRandomValues(bytes)
    return formatBytes(bytes)
  }
  fallbackCounter = (fallbackCounter + 1) % 1_000_000
  return `request-${Date.now().toString(36)}-${fallbackCounter.toString(36)}`
}
