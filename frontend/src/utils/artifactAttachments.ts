export type ArtifactAttachment = {
  id: string
  filename: string
  url: string
  format?: string
  mime?: string
  size?: number
  sha256?: string
}

const FILE_ID_RE = /^[a-f0-9]{32}$/i
const SHA256_RE = /^[a-f0-9]{64}$/i
const FORMAT_BY_SUFFIX: Record<string, { format: string; mime: string }> = {
  docx: {
    format: 'docx',
    mime: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  },
  xlsx: {
    format: 'xlsx',
    mime: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  },
  md: { format: 'markdown', mime: 'text/markdown; charset=utf-8' },
}

function parsedResult(value: unknown): any {
  if (typeof value !== 'string') return value
  try { return JSON.parse(value) } catch { return value }
}

function safeFilename(value: unknown) {
  const filename = String(value || '').trim()
  if (
    !filename || filename.length > 240
    || filename === '.' || filename === '..'
    || filename.includes('/') || filename.includes('\\')
    || /[\u0000-\u001f<>:"|?*]/.test(filename)
    || /[ .]$/.test(filename)
  ) return ''
  return filename
}

/**
 * Accept only a server-shaped successful execute_action result. Tool content,
 * model text and caller-provided download_url values are untrusted; the URL is
 * always rebuilt from the validated BucketFile id on the current origin.
 */
export function actionArtifactAttachment(toolCall: any): ArtifactAttachment | null {
  const name = String(toolCall?.name || toolCall?.function?.name || '')
  if (name !== 'execute_action') return null
  const response = parsedResult(toolCall?.result)
  if (!response || typeof response !== 'object') return null
  const successful = response.status === 'success'
    || (response.status === 'idempotent_replay' && response.original_status === 'success')
  if (!successful) return null
  const artifact = response.result?.artifact
  if (!artifact || typeof artifact !== 'object') return null
  const id = String(artifact.id || '')
  const filename = safeFilename(artifact.filename)
  if (!FILE_ID_RE.test(id) || !filename) return null
  const suffix = filename.split('.').pop()?.toLowerCase() || ''
  const spec = FORMAT_BY_SUFFIX[suffix]
  if (!spec) return null
  const declaredFormat = String(artifact.format || '').toLowerCase()
  const declaredMime = String(artifact.mime || '').split(';', 1)[0].trim().toLowerCase()
  const expectedMime = spec.mime.split(';', 1)[0].toLowerCase()
  if (declaredFormat !== spec.format || declaredMime !== expectedMime) return null
  const size = Number(artifact.size)
  const sha256 = String(artifact.sha256 || '')
  if (!Number.isSafeInteger(size) || size <= 0 || !SHA256_RE.test(sha256)) return null
  return {
    id: id.toLowerCase(),
    filename,
    format: spec.format,
    mime: spec.mime,
    size,
    sha256: sha256.toLowerCase(),
    url: `/api/data-sources/files/${id.toLowerCase()}/download`,
  }
}
