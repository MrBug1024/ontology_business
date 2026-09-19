import type { LocationQuery, LocationQueryRaw } from 'vue-router'

export const PLATFORM_SETTINGS_TABS = ['general', 'llm', 'tools', 'skills', 'mcp'] as const
export type PlatformSettingsTab = (typeof PLATFORM_SETTINGS_TABS)[number]

export function platformSettingsTabFromQuery(value: unknown): PlatformSettingsTab | null {
  const candidate = Array.isArray(value) ? value[0] : value
  return typeof candidate === 'string' && PLATFORM_SETTINGS_TABS.some((tab) => tab === candidate)
    ? candidate as PlatformSettingsTab
    : null
}

export function platformSettingsQuery(query: LocationQuery, tab: PlatformSettingsTab | null): LocationQueryRaw {
  const next: LocationQueryRaw = { ...query }
  if (tab) next.platform_settings = tab
  else delete next.platform_settings
  return next
}
