export interface ModelingAdvisorResourceSelection {
  llm_config_id: string | null
  skill_ids: string[]
  mcp_ids: string[]
}

export interface ModelingAdvisorResourceOption { id: string; name: string; description: string }
export interface ModelingAdvisorResourceOptions {
  models: ModelingAdvisorResourceOption[]
  skills: ModelingAdvisorResourceOption[]
  mcps: ModelingAdvisorResourceOption[]
}
