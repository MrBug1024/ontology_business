export interface ChannelInteraction {
  kind: 'workflow_approval' | 'capability_confirmation'
  id: string
  code: string
  revision: number
  status: string
  title: string
  text: string
  reply_texts: string[]
  expires_at: string | null
  recipient_user_ids: string[]
  recipient_roles: string[]
  requires_evidence: boolean
}

export interface ChannelDelivery {
  revision: string
  contract: 'channel-delivery/v1'
  format: 'text/plain'
  text: string
  interactions: ChannelInteraction[]
  attachments: Array<{ id: string; filename: string }>
}

export interface ChannelReplyRequest {
  text: string
  message_id: string
  expected_revision: number
  evidence: Array<{ asset_version_id?: string; dataset_version_id?: string; expected_signature?: string }>
}

export interface ChannelReplyResult {
  interaction_id: string
  status: string
  text: string
  invocation_id?: string | null
  workflow_run_id?: string | null
}
