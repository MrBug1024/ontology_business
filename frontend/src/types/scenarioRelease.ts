export interface ScenarioRelease {
  id: string
  scenario_id: string
  scenario_name: string
  name: string
  notes: string
  enabled: boolean
  status: string
  revision: number
  created_at: string
  created_by_user_id: string | null
  created_by_name: string
  retired_at: string | null
  deleted_at: string | null
  can_manage: boolean
}

export interface ScenarioReleasePage {
  items: ScenarioRelease[]
  limit: number
  offset: number
  has_more: boolean
}

export type ReleaseAction = 'enable' | 'disable' | 'retire' | 'delete'
