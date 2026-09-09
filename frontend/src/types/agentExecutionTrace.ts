export interface AgentToolProgress {
  call_id: string
  name: string
  phase: 'started' | 'finished'
  status: string
  invocation_id?: string
  error_code?: string
}

export interface AgentExecutionStep {
  step_key: string
  call_id: string
  name: string
  status: string
  started_at?: string
  finished_at?: string
  invocation_id?: string
  error_code?: string
}
