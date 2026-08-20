// 领域类型定义

export interface User {
  id: string
  email: string
  display_name?: string
  tenant_id: string
  email_verified?: boolean
}

export interface AuthMessage {
  ok: boolean
  message: string
  email?: string
}

export interface Property {
  name: string
  data_type: string
  description?: string
  is_key?: boolean
  is_required?: boolean
  is_enum?: boolean
  enum_values?: string[]
  default_value?: string
}

export interface Entity {
  id?: string
  scenario_id?: string
  name: string
  description?: string
  icon?: string
  color?: string
  is_abstract?: boolean
  properties: Property[]
  created_at?: string
}

export interface Relation {
  id?: string
  scenario_id?: string
  name: string
  source_entity_id: string
  target_entity_id: string
  relation_type: string
  description?: string
  source_entity_name?: string
  target_entity_name?: string
}

export interface Scenario {
  id: string
  name: string
  description?: string
  industry?: string
  status?: string
  created_at?: string
  updated_at?: string
  entity_count?: number
  relation_count?: number
  data_source_count?: number
  action_count?: number
  rule_count?: number
  event_count?: number
  workflow_count?: number
}

export interface OntologyInstance {
  id?: string
  scenario_id?: string
  entity_id: string
  name: string
  attributes: Record<string, any>
  source?: string
  source_ref?: string
  entity_name?: string
  entity_color?: string
  created_at?: string
}

export interface RelationInstance {
  id?: string
  scenario_id?: string
  relation_id: string
  source_instance_id: string
  target_instance_id: string
  attributes?: Record<string, any>
  relation_name?: string
  source_instance_name?: string
  target_instance_name?: string
  created_at?: string
}

export interface ObjectProvenance {
  kind: string
  reference: string
  mapping_id?: string
  data_source_id?: string
  data_source_name?: string
  table_name?: string
  status?: string
}

export interface ObjectRelation {
  id: string
  direction: 'outgoing' | 'incoming'
  relation_id: string
  relation_name?: string
  relation_type?: string
  related_object_id: string
  related_object_name: string
  related_entity_id: string
  related_entity_name?: string
  attributes?: Record<string, any>
  created_at?: string
}

export interface ObjectSearchItem extends OntologyInstance {
  id: string
  scenario_id: string
  entity_name: string
  entity_color: string
  provenance: ObjectProvenance
  relation_count: number
}

export interface ObjectSearchResult {
  items: ObjectSearchItem[]
  total: number
  limit: number
  offset: number
  query: string
  entity_id?: string
}

export interface ObjectDetail extends ObjectSearchItem {
  relations: ObjectRelation[]
}

export interface DataMapping {
  id?: string
  scenario_id?: string
  entity_id: string
  data_source_id: string
  table_name: string
  column_map: Record<string, string>
  entity_name?: string
  data_source_name?: string
  data_source_type?: string
  status?: 'unknown' | 'ready' | 'ok' | 'error' | string
  last_error?: string
  last_checked_at?: string
  last_refreshed_at?: string
  last_row_count?: number
  last_imported_count?: number
  created_at?: string
}

export interface DataMappingFieldPreview {
  property_name: string
  data_type: string
  is_key: boolean
  is_required: boolean
  source_column: string
  source_exists: boolean
  status: 'mapped' | 'missing' | 'invalid'
}

export interface DataMappingPreview {
  mapping_id: string
  entity_name: string
  data_source_name: string
  table_name: string
  ok: boolean
  message: string
  columns: string[]
  sample_rows: any[][]
  row_count: number
  truncated: boolean
  fields: DataMappingFieldPreview[]
  missing_properties: string[]
  unmapped_columns: string[]
  warnings: string[]
  errors: string[]
  status?: string
  checked_at?: string
}

export interface DataMappingRefresh {
  mapping_id: string
  ok: boolean
  status: string
  message: string
  rows_scanned: number
  instances_created: number
  relations_created: number
  last_refreshed_at?: string
  last_error?: string
}

export interface GraphNode {
  id: string
  label: string
  /** 'entity' | 'instance' */
  type: string
  color?: string
  /** 实例节点半径 */
  size?: number
  /** 附加信息（属性数、描述、所属实体等） */
  meta?: Record<string, any>
}

export interface GraphEdge {
  id: string
  source: string
  target: string
  label?: string
  /** '1:1' | '1:N' | 'N:M' | 'belongs' | 'rel' */
  type?: string
  relation_type?: string
}

export interface GraphData {
  nodes: GraphNode[]
  edges: GraphEdge[]
}

export interface OntologyAction {
  id?: string
  scenario_id?: string
  entity_id: string
  name: string
  description?: string
  input_schema?: Record<string, any>
  executor_type?: string // sql / skill / mcp / http / script
  executor_config?: Record<string, any>
  precondition?: string
  postcondition?: string
  enabled?: boolean
  requires_confirmation?: boolean
  idempotency_required?: boolean
  permission_scope?: 'scenario' | string
  entity_name?: string
  created_at?: string
}

export interface OntologyRule {
  id?: string
  scenario_id?: string
  entity_id?: string
  name: string
  description?: string
  condition?: Record<string, any>
  action_on_match?: string
  trigger_action_ids?: string[]
  severity?: string // info / warning / critical
  enabled?: boolean
  entity_name?: string
  created_at?: string
}

export interface OntologyEvent {
  id?: string
  scenario_id?: string
  name: string
  description?: string
  payload_schema?: Record<string, any>
  trigger_source?: string
  enabled?: boolean
  created_at?: string
}

export interface WorkflowStep {
  step?: number
  type: string // action / rule / event
  action_id?: string
  rule_id?: string
  event_id?: string
  params?: Record<string, any>
  record?: Record<string, any>
  payload?: Record<string, any>
}

/** 可视化工作流节点（VueFlow 格式） */
export interface WorkflowNode {
  id: string
  type: string // start / end / action / rule / llm / event / http / script
  name?: string
  position?: { x: number; y: number }
  data?: Record<string, any>
}

/** 可视化工作流连线（label: true / false / 空） */
export interface WorkflowEdge {
  id: string
  source: string
  target: string
  label?: string
}

export interface OntologyWorkflow {
  id?: string
  scenario_id?: string
  name: string
  description?: string
  trigger_type?: 'manual' | 'scheduled' | 'event' | string
  trigger_config?: Record<string, any>
  steps?: WorkflowStep[] // 旧版线性步骤（兼容）
  nodes?: WorkflowNode[] // 可视化 DAG 节点
  edges?: WorkflowEdge[] // 可视化 DAG 连线
  status?: 'draft' | 'active' | 'disabled' | string
  enabled?: boolean
  created_at?: string
}

export interface ActionExecutionLog {
  id: string
  scenario_id: string
  target_type: string
  target_id: string
  target_name: string
  input_params?: Record<string, any>
  status: string
  mode?: string
  idempotency_key?: string
  result?: Record<string, any>
  error?: string
  duration_ms?: number
  created_at?: string
}

export interface ScenarioDetail extends Scenario {
  entities: Entity[]
  relations: Relation[]
  data_sources: DataSource[]
  instances: OntologyInstance[]
  relation_instances: RelationInstance[]
  mappings: DataMapping[]
  actions: OntologyAction[]
  rules: OntologyRule[]
  events: OntologyEvent[]
  workflows: OntologyWorkflow[]
}

export interface DataSource {
  id?: string
  scenario_id?: string
  name: string
  type: string // mysql / postgres / sqlite / file_bucket
  config: Record<string, any>
  status?: string
  last_error?: string
  created_at?: string
  file_count?: number
  can_write?: boolean
}

export interface BucketFile {
  id: string
  data_source_id: string
  filename: string
  size: number
  mime: string
  status: string
  error?: string
  index_status?: 'pending' | 'indexed' | 'partial' | 'error' | string
  index_error?: string
  index_version?: string
  indexed_at?: string | null
  chunk_count?: number
  created_at?: string
}

export interface TableInfo {
  name: string
  columns: { name: string; type: string; pk?: boolean }[]
  row_count: number
}

export interface LLMConfig {
  id?: string
  name: string
  provider: string
  base_url: string
  api_key: string
  model: string
  temperature: number
  max_tokens: number
  is_default?: boolean
  capabilities?: Array<'chat' | 'embedding' | 'vision' | 'tool' | string>
  enabled?: boolean
  routing_priority?: number
  input_cost_per_million?: number
  output_cost_per_million?: number
  budget_limit?: number
  cost_currency?: string
  created_at?: string
  updated_at?: string
}

export interface LLMTrace {
  id: string
  llm_config_id?: string | null
  provider: string
  model: string
  capability: string
  operation: string
  status: 'succeeded' | 'failed' | 'cancelled' | string
  latency_ms: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  estimated_cost: number
  currency: string
  tool_count: number
  error?: string
  created_at?: string
}

export interface LLMUsageSummary {
  llm_config_id: string
  invocation_count: number
  succeeded_count: number
  failed_count: number
  cancelled_count: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  estimated_cost: number
  budget_limit: number
  budget_remaining?: number | null
  currency: string
  average_latency_ms: number
  by_capability: Record<string, { invocation_count: number; input_tokens: number; output_tokens: number; estimated_cost: number }>
}

export interface LLMEvaluation {
  id?: string
  llm_config_id?: string | null
  name: string
  capability: 'chat' | 'embedding' | 'vision' | 'tool' | string
  passed: boolean
  score: number
  latency_ms: number
  input_tokens: number
  output_tokens: number
  estimated_cost: number
  currency?: string
  notes?: string
  metrics?: Record<string, unknown>
  created_at?: string
}

export interface LLMEvaluationSummary {
  llm_config_id: string
  total: number
  passed: number
  failed: number
  average_score: number
  average_latency_ms: number
  input_tokens: number
  output_tokens: number
  estimated_cost: number
  latest_at?: string | null
}

export interface LineageNode {
  id: string
  kind: 'data_source' | 'mapping' | 'object' | 'document' | 'document_chunk' | 'ai_answer' | 'action' | 'action_execution' | 'external_result' | 'workflow_run' | string
  label: string
  meta?: Record<string, unknown>
}

export interface LineageEdge {
  id: string
  source: string
  target: string
  kind: string
  label?: string
  meta?: Record<string, unknown>
}

export interface LineageGraph {
  scenario_id: string
  nodes: LineageNode[]
  edges: LineageEdge[]
  truncated: boolean
  summary: {
    data_sources: number
    objects: number
    ai_answers: number
    action_executions: number
  }
}

export interface Skill {
  id: string
  name: string
  description: string
  path: string
  source: string
  enabled: boolean
  metadata: Record<string, any>
  created_at?: string
}

export interface MCPConfig {
  id?: string
  name: string
  transport: string
  command?: string
  args?: string[]
  url?: string
  env?: Record<string, string>
  headers?: Record<string, string>
  enabled?: boolean
  created_at?: string
}

export interface MCPTool {
  name: string
  description?: string
  input_schema?: Record<string, any>
}

export interface Agent {
  id?: string
  name: string
  description?: string
  scenario_id?: string | null
  llm_config_id?: string | null
  system_prompt?: string
  skill_ids: string[]
  mcp_ids: string[]
  data_source_ids: string[]
  temperature?: number
  max_tokens?: number
  created_at?: string
  updated_at?: string
  scenario_name?: string
  llm_name?: string
  skill_names?: string[]
  mcp_names?: string[]
  data_source_names?: string[]
}

export interface Conversation {
  id: string
  agent_id: string
  title: string
  created_at?: string
}

export interface ChatMessage {
  id?: string
  role: string
  content: string
  tool_calls?: any[]
  tool_results?: any[]
  citations?: RagCitation[]
  created_at?: string
}

/** P1 运行时任务状态：由队列、重试、超时与审批共同驱动。 */
export type WorkflowRunStatus =
  | 'queued'
  | 'running'
  | 'awaiting_approval'
  | 'retry_waiting'
  | 'succeeded'
  | 'failed'
  | 'timed_out'
  | 'rejected'
  | 'cancelled'

export interface WorkflowApproval {
  id: string
  workflow_run_id: string
  scenario_id: string
  workflow_id: string
  workflow_name: string
  node_id: string
  node_name: string
  instructions?: string
  status: string
  requested_at?: string
  expires_at?: string | null
  resolved_at?: string | null
  comment?: string
}

export interface WorkflowRun {
  id: string
  scenario_id: string
  workflow_id: string
  workflow_name: string
  trigger_source: string
  status: WorkflowRunStatus | string
  input_params: Record<string, any>
  attempt: number
  max_attempts: number
  timeout_seconds: number
  available_at?: string | null
  scheduled_for?: string | null
  started_at?: string | null
  completed_at?: string | null
  next_retry_at?: string | null
  error?: string
  result?: Record<string, any>
  pending_approval?: WorkflowApproval | boolean | null
  created_at?: string
  updated_at?: string
}

export interface AssistantThread {
  id: string
  scenario_id?: string | null
  scope_key?: string
  title: string
  created_at?: string
  updated_at?: string
}

export interface AssistantAttachment {
  id: string
  filename: string
  mime?: string
  size: number
  status: string
  error?: string
  created_at?: string
}

/** P1 检索命中：可直接跳转至原文的稳定引用。 */
export interface RagCitation {
  citation_id: string
  chunk_id: string
  file_id: string
  filename: string
  data_source_id: string
  data_source_name: string
  char_start: number
  char_end: number
  chunk_ordinal: number
  content_hash: string
  embedding_model: string
  index_version: string
  score: number
  vector_score: number
  keyword_score: number
  text: string
}

export interface DocumentSearchResult {
  query: string
  results: RagCitation[]
  searched_data_source_ids: string[]
  excluded_data_source_ids: string[]
  permission_message: string
  retrieval_mode: string
}

export interface DocumentReindexResult {
  data_source_id: string
  files_total: number
  files_indexed: number
  chunks_total: number
  jobs_queued: number
  jobs_existing: number
  items: Array<{ file_id: string; status: string; indexed: boolean; chunk_count: number; error?: string }>
}

export interface AssistantProposal {
  kind: 'ontology' | 'workflow'
  proposal_id: string
  title: string
  summary?: string
  payload: Record<string, any>
  changes?: {
    operation: 'add' | 'update' | 'delete' | 'skip' | string
    resource: string
    name: string
    summary?: string
  }[]
  base_snapshot?: Record<string, any>
  requires_confirmation?: boolean
  status?: 'pending' | 'applied' | string
  applied_at?: string
  apply_result?: Record<string, any>
}

export interface AssistantThought {
  id: string
  title: string
  detail?: string
  status?: 'running' | 'done' | 'error'
}

export interface AssistantMessage {
  id?: string
  thread_id?: string
  role: 'user' | 'assistant' | 'system'
  content: string
  context?: Record<string, any>
  attachments?: AssistantAttachment[] | Record<string, any>[]
  proposal?: AssistantProposal | Record<string, any>
  questions?: { id: string; title: string; message: string }[]
  sources?: { id: string; filename: string; status?: string }[]
  thinking?: AssistantThought[]
  streaming?: boolean
  created_at?: string
}

export interface AssistantReply {
  thread_id: string
  reply: string
  proposal?: AssistantProposal | Record<string, any>
  questions?: { id: string; title: string; message: string }[]
  suggestions?: string[]
  sources?: { id: string; filename: string; status?: string }[]
}

export interface ChatEvent {
  type: string
  data: any
}
