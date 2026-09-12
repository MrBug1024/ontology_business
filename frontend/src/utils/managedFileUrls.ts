/**
 * Build same-origin URLs for managed BucketFile resources.
 *
 * ``agent_id`` is an authorization context, not a bearer credential.  It is
 * intentionally omitted for legacy modeling files and encoded whenever it is
 * present so a filename/identifier can never alter the request path.
 */
function scopedFilePath(fileId: string, suffix: 'text' | 'download', agentId?: string) {
  const path = `/data-sources/files/${encodeURIComponent(String(fileId))}/${suffix}`
  const scope = String(agentId || '').trim()
  return scope ? `${path}?agent_id=${encodeURIComponent(scope)}` : path
}

export function managedFileTextPath(fileId: string, agentId?: string) {
  return scopedFilePath(fileId, 'text', agentId)
}

export function managedFileDownloadPath(fileId: string, agentId?: string) {
  return scopedFilePath(fileId, 'download', agentId)
}

export function managedFileTextUrl(fileId: string, agentId?: string) {
  return `/api${managedFileTextPath(fileId, agentId)}`
}

export function managedFileDownloadUrl(fileId: string, agentId?: string) {
  return `/api${managedFileDownloadPath(fileId, agentId)}`
}
