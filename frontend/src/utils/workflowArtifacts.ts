import { actionArtifactAttachment } from './artifactAttachments.ts'
import type { ArtifactAttachment } from './artifactAttachments.ts'

export function workflowArtifacts(value: unknown): ArtifactAttachment[] {
  if (!value || typeof value !== 'object' || !('steps' in value) || !Array.isArray(value.steps)) return []
  const files = new Map<string, ArtifactAttachment>()
  for (const step of value.steps) {
    if (!step || typeof step !== 'object' || step.type !== 'action' || step.status !== 'success') continue
    const artifact = actionArtifactAttachment({ name: 'execute_action', result: { status: 'success', result: step.result?.result } })
    if (artifact) files.set(artifact.id, artifact)
  }
  return [...files.values()]
}
