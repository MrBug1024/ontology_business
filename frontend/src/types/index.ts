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
  created_at?: string
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
  trigger_type?: string // manual / scheduled / event
  trigger_config?: Record<string, any>
  steps?: WorkflowStep[] // 旧版线性步骤（兼容）
  nodes?: WorkflowNode[] // 可视化 DAG 节点
  edges?: WorkflowEdge[] // 可视化 DAG 连线
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
}

export interface BucketFile {
  id: string
  data_source_id: string
  filename: string
  size: number
  mime: string
  status: string
  error?: string
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
  created_at?: string
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
  created_at?: string
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

export interface AssistantProposal {
  kind: 'ontology' | 'workflow'
  title: string
  summary?: string
  payload: Record<string, any>
  requires_confirmation?: boolean
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
