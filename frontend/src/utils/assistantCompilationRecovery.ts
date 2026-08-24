import type { AssistantCompilationJobStatus } from '@/types'

const STORAGE_PREFIX = 'ontology-assistant-compilation:v1'

export interface CompilationRecoveryOwnerScope {
  tenantId: string
  userId: string
  scenarioId: string
}

export interface CompilationRecoveryThreadScope extends CompilationRecoveryOwnerScope {
  threadId: string
}

interface CompilationJobBookmark extends CompilationRecoveryThreadScope {
  version: 1
  jobId: string
  savedAt: string
}

interface PendingCompilationJobBookmark extends CompilationRecoveryOwnerScope {
  version: 1
  jobId: string
  savedAt: string
}

export interface RecoveryStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

function clean(value: unknown) {
  return typeof value === 'string' ? value.trim() : ''
}

function validOwner(scope: CompilationRecoveryOwnerScope) {
  return Boolean(clean(scope.tenantId) && clean(scope.userId) && clean(scope.scenarioId))
}

function ownerTokens(scope: CompilationRecoveryOwnerScope) {
  return [scope.tenantId, scope.userId, scope.scenarioId].map((value) => encodeURIComponent(clean(value)))
}

export function compilationJobBookmarkKey(scope: CompilationRecoveryThreadScope) {
  if (!validOwner(scope) || !clean(scope.threadId)) return ''
  return `${STORAGE_PREFIX}:job:${[...ownerTokens(scope), encodeURIComponent(clean(scope.threadId))].join(':')}`
}

export function pendingCompilationJobBookmarkKey(scope: CompilationRecoveryOwnerScope) {
  if (!validOwner(scope)) return ''
  return `${STORAGE_PREFIX}:pending:${ownerTokens(scope).join(':')}`
}

function readJson(storage: RecoveryStorage, key: string): Record<string, unknown> | null {
  if (!key) return null
  try {
    const value = JSON.parse(storage.getItem(key) || 'null')
    return value && typeof value === 'object' && !Array.isArray(value) ? value : null
  } catch {
    return null
  }
}

export function saveCompilationJobBookmark(
  storage: RecoveryStorage,
  scope: CompilationRecoveryThreadScope,
  jobId: string,
) {
  const key = compilationJobBookmarkKey(scope)
  const normalizedJobId = clean(jobId)
  if (!key || !normalizedJobId) return false
  const value: CompilationJobBookmark = {
    version: 1,
    tenantId: clean(scope.tenantId),
    userId: clean(scope.userId),
    scenarioId: clean(scope.scenarioId),
    threadId: clean(scope.threadId),
    jobId: normalizedJobId,
    savedAt: new Date().toISOString(),
  }
  try {
    storage.setItem(key, JSON.stringify(value))
    return true
  } catch {
    return false
  }
}

export function readCompilationJobBookmark(
  storage: RecoveryStorage,
  scope: CompilationRecoveryThreadScope,
) {
  const key = compilationJobBookmarkKey(scope)
  const value = readJson(storage, key)
  if (!value) return ''
  const matches = value.version === 1
    && clean(value.tenantId) === clean(scope.tenantId)
    && clean(value.userId) === clean(scope.userId)
    && clean(value.scenarioId) === clean(scope.scenarioId)
    && clean(value.threadId) === clean(scope.threadId)
  return matches ? clean(value.jobId) : ''
}

export function clearCompilationJobBookmark(
  storage: RecoveryStorage,
  scope: CompilationRecoveryThreadScope,
) {
  const key = compilationJobBookmarkKey(scope)
  if (!key) return
  try { storage.removeItem(key) } catch { /* Storage can be unavailable in privacy modes. */ }
}

export function savePendingCompilationJobBookmark(
  storage: RecoveryStorage,
  scope: CompilationRecoveryOwnerScope,
  jobId: string,
) {
  const key = pendingCompilationJobBookmarkKey(scope)
  const normalizedJobId = clean(jobId)
  if (!key || !normalizedJobId) return false
  const value: PendingCompilationJobBookmark = {
    version: 1,
    tenantId: clean(scope.tenantId),
    userId: clean(scope.userId),
    scenarioId: clean(scope.scenarioId),
    jobId: normalizedJobId,
    savedAt: new Date().toISOString(),
  }
  try {
    storage.setItem(key, JSON.stringify(value))
    return true
  } catch {
    return false
  }
}

export function readPendingCompilationJobBookmark(
  storage: RecoveryStorage,
  scope: CompilationRecoveryOwnerScope,
) {
  const key = pendingCompilationJobBookmarkKey(scope)
  const value = readJson(storage, key)
  if (!value) return ''
  const matches = value.version === 1
    && clean(value.tenantId) === clean(scope.tenantId)
    && clean(value.userId) === clean(scope.userId)
    && clean(value.scenarioId) === clean(scope.scenarioId)
  return matches ? clean(value.jobId) : ''
}

export function clearPendingCompilationJobBookmark(
  storage: RecoveryStorage,
  scope: CompilationRecoveryOwnerScope,
) {
  const key = pendingCompilationJobBookmarkKey(scope)
  if (!key) return
  try { storage.removeItem(key) } catch { /* Storage can be unavailable in privacy modes. */ }
}

export function compilationJobMatchesScenario(
  job: Pick<AssistantCompilationJobStatus, 'scenario_id'>,
  scenarioId: string,
) {
  const expected = clean(scenarioId)
  return Boolean(expected && clean(job.scenario_id) === expected)
}

/** Prefer a bookmarked terminal/running job; otherwise discover only a live job. */
export function selectCompilationJobForRecovery(
  jobs: AssistantCompilationJobStatus[],
  scenarioId: string,
  bookmarkedJobId = '',
) {
  const scoped = jobs.filter((job) => compilationJobMatchesScenario(job, scenarioId))
  const bookmark = clean(bookmarkedJobId)
  return (bookmark ? scoped.find((job) => job.id === bookmark) : undefined)
    || scoped.find((job) => job.status === 'running')
    || null
}

/** Hidden tabs poll much less often; transient GET errors also back off. */
export function compilationPollDelay(hidden: boolean, consecutiveErrors = 0) {
  const base = hidden ? 15_000 : 2_500
  const multiplier = 2 ** Math.min(Math.max(Math.trunc(consecutiveErrors), 0), 3)
  return Math.min(base * multiplier, hidden ? 60_000 : 20_000)
}
