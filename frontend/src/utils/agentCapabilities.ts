import type { AgentCapabilityCategory, AgentCapabilityScope } from '@/types'

export const AGENT_CAPABILITY_CATEGORIES: readonly AgentCapabilityCategory[] = [
  'functions',
  'actions',
  'rules',
  'events',
  'workflows',
]

/** New Agents and scenario switches are fail-closed by construction. */
export function emptyAgentCapabilityScope(): AgentCapabilityScope {
  return {
    functions: { mode: 'explicit', selected_ids: [] },
    actions: { mode: 'explicit', selected_ids: [] },
    rules: { mode: 'explicit', selected_ids: [] },
    events: { mode: 'explicit', selected_ids: [] },
    workflows: { mode: 'explicit', selected_ids: [] },
  }
}

/** A new Agent can use the whole bound business scenario unless explicitly narrowed. */
export function allAgentCapabilityScope(): AgentCapabilityScope {
  return {
    functions: { mode: 'all', selected_ids: [] },
    actions: { mode: 'all', selected_ids: [] },
    rules: { mode: 'all', selected_ids: [] },
    events: { mode: 'all', selected_ids: [] },
    workflows: { mode: 'all', selected_ids: [] },
  }
}

/** Clone API state without sharing arrays; malformed/missing entries stay empty. */
export function cloneAgentCapabilityScope(
  scope?: Partial<AgentCapabilityScope> | null,
): AgentCapabilityScope {
  const next = emptyAgentCapabilityScope()
  if (!scope) return next
  for (const category of AGENT_CAPABILITY_CATEGORIES) {
    const entry = scope[category]
    if (!entry) continue
    next[category] = {
      mode: entry.mode === 'all' ? 'all' : 'explicit',
      selected_ids: entry.mode === 'all'
        ? []
        : [...new Set((entry.selected_ids || []).filter((id) => typeof id === 'string' && id))],
    }
  }
  return next
}
