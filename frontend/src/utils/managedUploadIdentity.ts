export function migrateManagedUploadRunEntry<T extends object>(
  entries: Map<string, T>,
  previousRunId: string,
  nextRunId: string,
) {
  if (!previousRunId || previousRunId === nextRunId) return
  const value = entries.get(previousRunId)
  if (value === undefined) return
  entries.delete(previousRunId)
  entries.set(nextRunId, value)
}
