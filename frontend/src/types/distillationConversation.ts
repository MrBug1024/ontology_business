import type { DistillationDocument } from './businessDistillation'

export type ConversationStatus = 'queued' | 'running' | 'waiting' | 'succeeded' | 'cancelled' | 'failed'
export interface DistillationResourceSelection {
  llm_config_id?: string | null
  skill_ids?: string[]
  mcp_ids?: string[]
  investigation_tool_keys?: InvestigationToolKey[] | null
}
export type InvestigationToolKey = 'list_evidence' | 'read_evidence' | 'read_current_document' | 'review_business' | 'read_target_system' | 'read_database_sample' | 'compare_database_samples' | 'record_human_statement' | 'open_business_system' | 'inspect_business_page' | 'navigate_business_page' | 'fill_business_query' | 'click_business_control' | 'login_business_system' | 'list_library_sources' | 'list_library_files' | 'read_library_source' | 'read_attachment' | 'ask_human' | 'propose_document'
export interface DistillationResourceOption {
  id: string
  name: string
  model?: string
  source?: string
  transport?: string
  description?: string
  mode?: 'instructions' | 'resources'
}
export interface DistillationResourceOptions {
  models: DistillationResourceOption[]
  skills: DistillationResourceOption[]
  mcps: DistillationResourceOption[]
  investigation_tools: {
    default_tool_keys: InvestigationToolKey[]
    always_available_tool_keys: InvestigationToolKey[]
    tools: { key: InvestigationToolKey; title: string; description: string; selectable: boolean; always_available: boolean }[]
  }
}
export interface DistillationAttachment {
  id: string
  request_id: string
  project_id: string
  filename: string
  media_type: string
  byte_size: number
  content_sha256: string
  status: 'ready' | 'bound' | 'removed' | 'expired'
  created_at: string
  expires_at: string
}
export interface DistillationAttachmentDraft {
  key: string
  filename: string
  byte_size: number
  status: 'uploading' | 'ready' | 'bound' | 'failed' | 'removing'
  progress: number
  error: string
  attachment: DistillationAttachment | null
}
export interface DistillationWebsiteObservation {
  target_key: string
  url: string
  title: string
  status: 'observed' | 'login_required' | 'redirect_blocked' | 'javascript_required'
  text: string
  content_sha256: string
  retrieved_at: string
  limitations: string[]
  read_only: true
  visible_fields: string[]
  allowed_links: string[]
}
export interface DistillationToolStep {
  id: string
  tool_name: string
  title: string
  status: 'running' | 'succeeded' | 'failed'
  summary: string
  started_at: string
  completed_at: string | null
  source?: DistillationWebsiteObservation | null
  library?: { data_source_id: string; bucket_file_id: string | null; evidence_key: string; title: string; identity_sha256: string; retrieved_at: string } | null
  mcp?: { mcp_id: string; evidence_key: string; title: string; summary: string; content_sha256: string; identity_sha256: string; retrieved_at: string; read_only: true } | null
}
export interface DistillationQuestion { id: string; title: string; question: string; reason: string; options: string[] }
export interface DistillationTurn {
  id: string
  project_id: string
  turn_number: number
  request_id: string
  status: ConversationStatus
  base_revision: number
  message: string
  assistant_message: string
  steps: DistillationToolStep[]
  questions: DistillationQuestion[]
  proposal: DistillationDocument | null
  applied_revision: number | null
  error: string
  attachments: DistillationAttachment[]
  created_at: string
  updated_at: string
  completed_at: string | null
}
export interface DistillationConversationPage { turns: DistillationTurn[]; has_more: boolean }
