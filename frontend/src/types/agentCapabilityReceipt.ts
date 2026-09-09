import type { ChannelDelivery } from './channelDelivery'

export interface AgentCapabilityReceipt {
  invocation_id: string
  status: string
  name: string
  can_confirm: boolean
  message: string
  workflow_run_id: string | null
  artifact_file_id: string | null
  artifact_filename: string | null
  delivery: ChannelDelivery
}
