// 领域类型定义

export interface User {
  id: string
  email: string
  display_name?: string
  tenant_id: string
  email_verified?: boolean
  can_manage?: boolean
}

export interface AuthMessage {
  ok: boolean
  message: string
  email?: string
}

export interface Property {
  id?: string
  name: string
  api_name?: string
  data_type: string
  description?: string
  is_key?: boolean
  /** Human-readable label property used by graphs and Agent answers. */
  is_title?: boolean
  is_required?: boolean
  is_enum?: boolean
  enum_values?: string[]
  default_value?: string
  /** Server-validated declarative constraints (for example minimum or pattern). */
  constraints?: Record<string, unknown>
  is_sensitive?: boolean
}

export interface Entity {
  id?: string
  scenario_id?: string
  name: string
  api_name?: string
  namespace?: string
  description?: string
  icon?: string
  color?: string
  is_abstract?: boolean
  /** Name of the enum property used as this entity's lifecycle state. */
  state_property?: string
  properties: Property[]
  /** Server-computed ontology readiness; concrete types require one key and one title. */
  model_ready?: boolean
  model_issues?: string[]
  created_at?: string
}

export interface RelationConstraints {
  symmetric?: boolean
  transitive?: boolean
  irreflexive?: boolean
  asymmetric?: boolean
  antisymmetric?: boolean
  acyclic?: boolean
  inverse_relation_id?: string
  source_min_cardinality?: number
  source_max_cardinality?: number
  target_min_cardinality?: number
  target_max_cardinality?: number
}

export interface Relation {
  id?: string
  scenario_id?: string
  name: string
  namespace?: string
  source_entity_id: string
  target_entity_id: string
  relation_type: string
  constraints?: RelationConstraints
  description?: string
  source_entity_name?: string
  target_entity_name?: string
}

export interface Scenario {
  id: string
  name: string
  description?: string
  industry?: string
  namespace?: string
  status?: string
  /** Whether the current user may mutate resources in this scenario. */
  can_write?: boolean
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

export interface ScenarioPurgePlan {
  scenario_id: string
  scenario_name: string
  status: string
  can_purge: boolean
  blockers: string[]
  counts: Record<string, number>
  retained: Record<string, number>
  requires_audit_confirmation: boolean
}

export interface ScenarioPurgeResult {
  scenario_id: string
  deleted: boolean
  deletion_jobs: number
  retained: Record<string, number>
}

export interface OntologyInstance {
  id?: string
  scenario_id?: string
  entity_id: string
  name: string
  attributes: Record<string, any>
  source?: string
  source_ref?: string
  state?: string
  valid_from?: string | null
  valid_to?: string | null
  quality?: {
    score?: number
    status?: 'unknown' | 'valid' | 'warning' | 'invalid' | string
    issues?: string[]
    checked_at?: string
    source?: string
    [key: string]: unknown
  }
  access_scope?: 'tenant' | 'restricted'
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
  has_more?: boolean
  next_offset?: number | null
  total_is_exact?: boolean
}

export interface RelationInstanceSearchResult {
  items: RelationInstance[]
  total: number
  limit: number
  offset: number
  has_more?: boolean
  next_offset?: number | null
  total_is_exact?: boolean
}

export interface ObjectDetail extends ObjectSearchItem {
  relations: ObjectRelation[]
}

export interface DataMapping {
  id?: string
  scenario_id?: string
  entity_id: string
  data_source_id: string
  data_source_binding_key?: string
  data_source_binding_ref?: Record<string, any>
  table_name: string
  column_map: Record<string, string>
  transform_rules?: Record<string, Array<{
    op: 'trim' | 'lower' | 'upper' | 'default' | 'replace' | 'to_string' | 'to_integer' | 'to_float' | 'to_boolean' | string
    value?: unknown
    old?: string
    new?: string
  }>>
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
  is_title: boolean
  is_required: boolean
  source_column: string
  source_exists: boolean
  status: 'mapped' | 'missing' | 'invalid'
  transform_rules?: Array<{
    op: string
    old?: string
    new?: string
    value?: unknown
  }>
}

export type RelationDataMappingMode = 'source_fk' | 'target_fk' | 'join_table'

export interface RelationDataMappingInput {
  relation_id: string
  source_mapping_id: string
  target_mapping_id: string
  mode: RelationDataMappingMode
  foreign_key_column: string
  join_data_source_id: string
  join_table_name: string
  source_key_column: string
  target_key_column: string
}

export interface RelationDataMapping extends Omit<RelationDataMappingInput, 'join_data_source_id' | 'join_table_name'> {
  id: string
  scenario_id: string
  relation_name: string
  source_entity_name: string
  target_entity_name: string
  data_source_id: string
  data_source_name: string
  table_name: string
  status: string
  last_error?: string
  last_checked_at?: string
  last_refreshed_at?: string
  last_link_count: number
  created_at?: string
}

export interface RelationDataMappingPreview {
  ok: boolean
  message: string
  mode: RelationDataMappingMode
  relation_name: string
  source_entity_name: string
  target_entity_name: string
  data_source_id: string
  data_source_name: string
  table_name: string
  available_columns: string[]
  errors: string[]
  warnings: string[]
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
  transformed_rows?: Record<string, unknown>[]
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

export interface DataMappingRefreshJob {
  id: string
  mapping_id: string
  scenario_id: string
  environment: 'dev' | 'staging' | 'prod' | string
  status: 'queued' | 'running' | 'retry_waiting' | 'succeeded' | 'failed' | 'timed_out' | 'cancelled' | string
  limit: number
  attempt: number
  max_attempts: number
  timeout_seconds: number
  available_at?: string
  started_at?: string
  completed_at?: string
  next_retry_at?: string
  rows_scanned: number
  instances_created: number
  instances_updated: number
  relations_created: number
  connector_audit: Array<Record<string, any>>
  error?: string
  created_at?: string
  updated_at?: string
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
  /** '1:1' | '1:N' | 'N:1' | 'N:M' | 'belongs' | 'rel' */
  type?: string
  relation_type?: string
  meta?: Record<string, any>
}

export interface GraphData {
  nodes: GraphNode[]
  edges: GraphEdge[]
}

/**
 * 受治理函数只描述输入/输出契约与展示元数据；它不是可执行器，也不承载代码。
 * 实际访问权始终由所属场景的 ACL 决定，不能由 visibility 推断。
 */
export interface FunctionDefinition {
  id?: string
  scenario_id?: string
  name: string
  description?: string
  input_schema?: Record<string, unknown>
  output_schema?: Record<string, unknown>
  tags?: string[]
  visibility?: 'scenario' | 'tenant'
  runtime_kind?: 'contract' | 'weighted_score' | 'threshold' | 'geo_distance' | 'timeseries_aggregate' | string
  runtime_config?: Record<string, any>
  created_at?: string
  updated_at?: string
}

export interface FunctionRun {
  id: string
  tenant_id: string
  scenario_id: string
  function_id?: string
  run_type: string
  status: string
  input_payload?: Record<string, any>
  output_payload?: Record<string, any>
  error?: string
  started_at?: string
  completed_at?: string
  created_by_user_id?: string
  created_at?: string
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
  environment?: 'dev' | 'staging' | 'prod' | string
  definition_snapshot_id?: string | null
  release_id?: string | null
  definition_hash?: string
  definition_source?: 'live' | 'release' | string
  result?: Record<string, any>
  connector_audit?: Array<{
    kind: string
    environment: 'dev' | 'staging' | 'prod' | string
    managed?: boolean
    binding_key?: string | null
    binding_id?: string | null
    connector_id?: string
    connector_name?: string
    adapter_type?: string
  }>
  /** Verified decision-chain facts; legacy/context-less rows stay `unknown`. */
  actor_type?: 'user' | 'agent' | 'unknown'
  actor_user_id?: string | null
  agent_id?: string | null
  llm_config_id?: string | null
  model_name?: string | null
  permission_decision?: Record<string, unknown>
  data_context?: Record<string, unknown>
  correlation_id?: string
  parent_action_log_id?: string | null
  agent_message_id?: string | null
  assistant_message_id?: string | null
  error?: string
  duration_ms?: number
  created_at?: string
}

export interface ScenarioDetail extends Scenario {
  can_write?: boolean
  entities: Entity[]
  relations: Relation[]
  data_sources: DataSource[]
  instances: OntologyInstance[]
  relation_instances: RelationInstance[]
  runtime_instance_count?: number
  runtime_relation_count?: number
  runtime_facts_truncated?: boolean
  mappings: DataMapping[]
  relation_mappings: RelationDataMapping[]
  functions: FunctionDefinition[]
  actions: OntologyAction[]
  rules: OntologyRule[]
  events: OntologyEvent[]
  workflows: OntologyWorkflow[]
}

export interface ScenarioCapabilityPort {
  id: string
  tenant_id: string
  scenario_id: string
  capability_kind: 'function' | 'action' | 'workflow'
  capability_key: string
  port_key: string
  name: string
  description: string
  direction: 'input' | 'output'
  role: 'modeling_evidence' | 'test_fixture' | 'invocation_input' | 'reference' | 'rules' | 'output'
  media_kind: 'message' | 'structured' | 'document' | 'dataset' | 'connector' | 'artifact'
  dataset_id?: string | null
  dataset_schema_id?: string | null
  dataset_schema_hash?: string
  schema_document: Record<string, unknown>
  is_required: boolean
  cardinality: 'one' | 'many'
  binding_policy: 'per_invocation' | 'scenario_default' | 'release_pinned' | 'none'
  status: 'draft' | 'active' | 'retired'
  config: Record<string, unknown>
  created_by_user_id?: string | null
  created_at: string
  updated_at: string
}

export type ScenarioCapabilityPortWrite = Omit<
  ScenarioCapabilityPort,
  'id' | 'tenant_id' | 'scenario_id' | 'dataset_schema_hash' | 'created_by_user_id' | 'created_at' | 'updated_at'
>

export type ScenarioModelDraftStatus =
  | 'generated'
  | 'needs_revision'
  | 'needs_attention'
  | 'needs_binding'
  | 'needs_validation'
  | 'blocked'
  | 'ready_for_review'
  | 'deferred'
  | 'applied'
  | 'promoted'
  | 'partially_promoted'
  | 'resolved'
  | 'superseded'
  | 'discarded'
  | string

export type ScenarioModelDraftResourceKind =
  | 'entity'
  | 'property'
  | 'relation'
  | 'instance'
  | 'mapping'
  | 'conceptual_mapping'
  | 'relation_mapping'
  | 'function'
  | 'action'
  | 'rule'
  | 'event'
  | 'workflow'
  | 'capability_port'
  | string

export interface ScenarioModelDraftIssue {
  code?: string
  message: string
  field?: string
  path?: string
  blocking?: boolean
  resolution_hint?: string
  source_refs?: string[]
}

export type ScenarioModelCandidateOrigin = 'assistant' | 'manual' | 'imported' | 'unknown'
export type ScenarioModelCandidateValidationStatus = 'not_validated' | 'valid' | 'invalid'
export type ScenarioModelCandidateLifecycleStatus = 'candidate' | 'deferred' | 'formalized' | 'resolved' | 'superseded'
export type ScenarioModelCandidateActivationStatus = 'inactive' | 'active' | 'not_applicable'

export interface ScenarioModelCandidateBlocker extends ScenarioModelDraftIssue {
  field_path?: string[]
  draft_ids?: string[]
  resource_keys?: string[]
}

/** A provenance-neutral, inert candidate governed by the server quality state machine. */
export interface ScenarioModelDraftResource {
  id: string
  revision: number
  scenario_id?: string
  proposal_id: string
  task_id: string
  resource_kind: ScenarioModelDraftResourceKind
  resource_key: string
  title?: string
  payload: Record<string, any>
  validation_issues: ScenarioModelDraftIssue[]
  issues_count?: number
  blocking_issue_count?: number
  draft_status: ScenarioModelDraftStatus
  source?: 'assistant' | 'ai' | string
  materialization_source: string
  source_origin: ScenarioModelCandidateOrigin
  validation_status: ScenarioModelCandidateValidationStatus
  lifecycle_status: ScenarioModelCandidateLifecycleStatus
  promotion_eligible: boolean
  promotion_blockers: ScenarioModelCandidateBlocker[]
  activation_status: ScenarioModelCandidateActivationStatus
  quality_fingerprint: string
  source_thread_id?: string | null
  source_message_id?: string | null
  compilation_job_id?: string | null
  source_refs?: string[]
  resolved_resource_id?: string
  enabled: false
  publishable: false
  created_at?: string
  updated_at?: string
}

export interface ScenarioModelDraftListResponse {
  items?: ScenarioModelDraftResource[]
  drafts?: ScenarioModelDraftResource[]
  total?: number
  has_more?: boolean
  next_offset?: number | null
  issues_count?: number
  blocking_issue_count?: number
  summary?: ScenarioModelCandidateSummary
  page_summary?: ScenarioModelCandidateSummary
}

export interface ScenarioModelCandidateSummary {
  candidate_count?: number
  formalized_count?: number
  promotion_eligible_count?: number
  promotion_blocked_count?: number
  by_origin?: Record<string, number>
  by_validation?: Record<string, number>
  [key: string]: unknown
}

export interface ScenarioModelCandidateRevisionRequest {
  expected_revision: number
}

export interface ScenarioModelCandidatePromotionItem {
  draft_id: string
  expected_revision: number
}

export interface ScenarioModelCandidateBatchPromotionRequest {
  items: ScenarioModelCandidatePromotionItem[]
}

export interface ScenarioModelCandidateBatchRevalidationResult {
  ok: boolean
  revalidated_count: number
  eligible_count: number
  blocked_count: number
  eligible_draft_ids: string[]
}

export interface ScenarioModelCandidatePromotionResultItem {
  draft_id?: string
  resource_kind?: string
  formal_resource_id?: string
  activation_status?: ScenarioModelCandidateActivationStatus
}

export interface ScenarioModelCandidatePromotionResult {
  ok: boolean
  atomic: boolean
  promoted: ScenarioModelCandidatePromotionResultItem[]
  counts: Record<string, number>
  quality_fingerprint: string
}

export interface ScenarioModelDraftUpdate {
  expected_revision: number
  payload: Record<string, any>
}

export interface ScenarioModelDraftResolve {
  expected_revision: number
  resolved_resource_id: string
}

export type CatalogEnvironment = 'dev' | 'staging' | 'prod'

export type CatalogBindingRole =
  | 'modeling_evidence'
  | 'test_fixture'
  | 'invocation_input'
  | 'reference'
  | 'rules'
  | 'output'
  | 'input'

export type CatalogCanonicalBindingRole = Exclude<CatalogBindingRole, 'input'>

export interface CatalogAsset {
  id: string
  tenant_id: string
  key: string
  name: string
  description?: string
  kind: 'file' | 'stream' | 'api' | 'database' | 'generated' | 'other'
  media_type?: string
  labels: Record<string, unknown>
  lifecycle_status: 'active' | 'retired'
  created_by_user_id?: string | null
  created_at: string
  updated_at: string
  retired_at?: string | null
  version_count: number
}

export interface CatalogAssetVersion {
  id: string
  tenant_id: string
  asset_id: string
  version_number: number
  provenance_kind: string
  status: string
  content_sha256: string
  byte_size: number
  version_document: Record<string, unknown>
  created_by_user_id?: string | null
  created_at: string
}

export interface CatalogManagedUpload {
  purpose: 'managed_asset' | 'validation_asset' | 'invocation_attachment'
  temporary: boolean
  expires_at?: string | null
  created: boolean
  asset: Pick<CatalogAsset, 'id' | 'key' | 'name' | 'kind' | 'media_type' | 'lifecycle_status'>
  version: {
    id: string
    asset_id: string
    version_number: number
    provenance_kind: string
    status: string
    content_sha256: string
    byte_size: number
    profile: Record<string, unknown>
    lifecycle: Record<string, unknown>
    created_at: string
  }
}

export interface ValidationDataset {
  dataset_id: string
  dataset_version_id: string
  content_hash: string
  schema_hash: string
  record_count: number
  byte_size: number
  relation_names: string[]
  source_asset_version_ids: string[]
  reused: boolean
}

export interface ValidationDatasetJob {
  id: string
  status: 'queued' | 'running' | 'succeeded' | 'failed'
  error: string
  created_at: string
  updated_at: string
  result?: ValidationDataset | null
}

export interface LogicalDataset {
  id: string
  tenant_id: string
  key: string
  name: string
  description?: string
  labels: Record<string, unknown>
  lifecycle_status: 'active' | 'retired'
  created_by_user_id?: string | null
  created_at: string
  updated_at: string
  retired_at?: string | null
  schema_count: number
  version_count: number
  heads: Record<string, string>
}

export interface DatasetVersion {
  id: string
  tenant_id: string
  dataset_id: string
  schema_id: string
  version_number: number
  parent_version_id?: string | null
  status: string
  record_count: number
  fragment_count: number
  byte_size: number
  content_hash: string
  manifest: Record<string, unknown>
  created_by_user_id?: string | null
  created_at: string
  ready_at?: string | null
}

export interface DatasetHead {
  id: string
  tenant_id: string
  dataset_id: string
  environment: CatalogEnvironment
  dataset_version_id: string
  updated_by_user_id?: string | null
  updated_at: string
}

export interface ConnectorBindingOption {
  binding_key: string
  label: string
  connector_kind: 'data_source' | 'mcp' | 'llm'
  environment: CatalogEnvironment
  ready: boolean
  blocking_reason: string
  capabilities: string[]
  updated_at?: string | null
}

export interface ScenarioDatasetBindingCreate {
  dataset_id: string
  binding_key: string
  environment: CatalogEnvironment
  role: CatalogCanonicalBindingRole
  binding_mode: 'head' | 'pinned'
  dataset_head_id?: string | null
  dataset_version_id?: string | null
  is_required: boolean
  status: 'active' | 'disabled' | 'error'
  config: Record<string, unknown>
}

export interface ScenarioDatasetBinding extends Omit<ScenarioDatasetBindingCreate, 'role'> {
  id: string
  tenant_id: string
  scenario_id: string
  role: CatalogBindingRole
  resolved_dataset_version_id?: string | null
  created_at: string
  updated_at: string
}

export interface DataSource {
  id?: string
  scenario_id?: string
  name: string
  type: string // postgres / dataset / file_bucket
  config: Record<string, any>
  status?: string
  last_error?: string
  created_at?: string
  file_count?: number
  can_write?: boolean
  can_delete?: boolean
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

export type ArtifactTemplateFormat = 'docx' | 'xlsx' | 'markdown'

export interface ArtifactTemplateVersion {
  id: string
  version: number
  bucket_file_id: string
  data_source_id: string
  filename: string
  artifact_format: ArtifactTemplateFormat
  mime: string
  size: number
  sha256: string
  placeholder_paths: string[]
  metadata?: Record<string, any>
  version_note?: string
  created_at?: string
}

export interface ArtifactTemplateReference {
  action_id: string
  action_name: string
  scenario_id: string
  scenario_name: string
  entity_name?: string
  uses_current: boolean
  pinned_version?: number | null
}

export interface ArtifactTemplate {
  id: string
  key: string
  scenario_id?: string | null
  name: string
  purpose?: string
  description?: string
  status: 'active' | 'deprecated' | string
  current_version_id?: string | null
  current_version?: ArtifactTemplateVersion | null
  version_count: number
  reference_count: number
  deletable: boolean
  created_at?: string
  updated_at?: string
}

export interface ArtifactTemplateDetail extends ArtifactTemplate {
  versions: ArtifactTemplateVersion[]
  references: ArtifactTemplateReference[]
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

/** P1 持久化事件信封：发布后异步订阅工作流会进入任务队列。 */
export interface EventEnvelope {
  id: string
  scenario_id: string
  event_id: string
  name: string
  payload: Record<string, any>
  source: string
  source_run_id?: string | null
  environment?: 'dev' | 'staging' | 'prod' | string
  definition_snapshot_id?: string | null
  release_id?: string | null
  definition_hash?: string
  definition_source?: 'live' | 'release' | string
  created_at?: string
  queued_workflow_run_ids: string[]
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

export interface Skill {
  id: string
  name: string
  description: string
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

export interface MCPImportItem {
  name: string
  transport: 'stdio' | 'sse' | 'streamable_http'
  endpoint: string
  env_keys: string[]
  header_keys: string[]
  enabled: boolean
  action: 'create' | 'replace' | 'skip'
}

export interface MCPImportResult {
  dry_run: boolean
  created: number
  replaced: number
  skipped: number
  items: MCPImportItem[]
  configs: MCPConfig[]
}

export interface MCPTool {
  name: string
  description?: string
  input_schema?: Record<string, any>
}

export type AgentCapabilityCategory = 'functions' | 'actions' | 'rules' | 'events' | 'workflows'

export interface AgentCapabilitySelection {
  mode: 'all' | 'explicit'
  selected_ids: string[]
}

export type AgentCapabilityScope = Record<AgentCapabilityCategory, AgentCapabilitySelection>

export interface AgentCapabilityReadinessItem {
  id: string
  key?: string
  kind?: 'function' | 'action' | 'rule' | 'event' | 'workflow'
  name: string
  description?: string
  executable: boolean
  blocked_reasons: string[]
  input_schema?: Record<string, unknown>
  side_effect?: boolean
  requires_confirmation?: boolean
  idempotency_required?: boolean
  ports?: AgentCapabilityDataPort[]
  data_ports?: AgentCapabilityDataPort[]
}

export type AgentManagedBindingKind =
  | 'dataset_version'
  | 'dataset_head'
  | 'asset_version'
  | 'connector_binding'

export interface AgentCapabilityDataPort {
  key?: string
  port_key?: string
  name?: string
  description?: string
  direction?: 'input' | 'output'
  role?: string
  media_kind?: string
  required?: boolean
  cardinality?: string
  binding_policy?: string
  binding_kinds?: AgentManagedBindingKind[]
  allow_override?: boolean
  schema_document?: Record<string, unknown>
  schema_hash?: string | null
  schema_signature?: string | null
}

export interface AgentRuntimeCapability {
  kind: 'function' | 'action' | 'rule' | 'workflow'
  key: string
  name: string
  description?: string
  input_schema: Record<string, unknown>
  output_schema: Record<string, unknown>
  side_effect: boolean
  requires_confirmation: boolean
  idempotency_required: boolean
  data_ports: AgentCapabilityDataPort[]
  readiness: {
    ready: boolean
    issues?: Array<Record<string, unknown>>
  }
  definition_hash: string
  deployment_fingerprint: string
}

export interface AgentCapabilitySummary {
  mode: 'all' | 'explicit'
  available_count: number
  selected_count: number
  executable_count: number
  blocked_count: number
  blocked_reasons: string[]
  items: AgentCapabilityReadinessItem[]
}

export interface AgentCapabilityCatalog {
  scenario_id: string
  environment: 'dev' | 'staging' | 'prod'
  definition_hash: string
  categories: Record<AgentCapabilityCategory, AgentCapabilityReadinessItem[]>
}

export type AgentReadinessAxisKey = 'definition' | 'validation' | 'release' | 'runtime'

export interface AgentReadinessIssue {
  code: string
  label: string
  target?: string
  blocking?: boolean
}

export interface AgentReadinessAxis {
  ready: boolean
  missing: AgentReadinessIssue[]
}

/** Transitional server payload accepted while the four-axis contract rolls out. */
export interface AgentReadinessPayload {
  source?: 'server' | 'legacy'
  definition?: AgentReadinessAxis | boolean | null
  validation?: AgentReadinessAxis | boolean | null
  release?: AgentReadinessAxis | boolean | null
  runtime?: AgentReadinessAxis | boolean | null
  definition_valid?: boolean
  validation_ready?: boolean
  release_ready?: boolean
  runtime_ready?: boolean
  missing?: Partial<Record<AgentReadinessAxisKey, Array<AgentReadinessIssue | string>>>
  issues?: Partial<Record<AgentReadinessAxisKey, Array<AgentReadinessIssue | string>>>
}

export interface AgentReadiness extends AgentReadinessPayload {
  source: 'server' | 'legacy'
  definition: AgentReadinessAxis
  validation: AgentReadinessAxis
  release: AgentReadinessAxis
  runtime: AgentReadinessAxis
}

export type AgentRuntimeBindingMode =
  | 'legacy'
  | 'shadow'
  | 'prefer_capability'
  | 'capability_only'

export interface AgentRuntimeConnection {
  id?: string
  name: string
  type: 'postgres'
  config: Record<string, any>
  status?: string
  last_error?: string
}

export interface Agent {
  id?: string
  name: string
  description?: string
  scenario_id?: string | null
  llm_config_id?: string | null
  system_prompt?: string
  data_source_ids: string[]
  runtime_connections?: AgentRuntimeConnection[]
  capability_scope?: AgentCapabilityScope | null
  capability_scope_legacy?: boolean
  capability_summary?: Partial<Record<AgentCapabilityCategory, AgentCapabilitySummary>>
  runtime_binding_mode?: AgentRuntimeBindingMode
  readiness?: AgentReadiness | AgentReadinessPayload | null
  /** Flat fields are accepted for compatibility with early four-axis responses. */
  definition_valid?: boolean
  validation_ready?: boolean
  release_ready?: boolean
  runtime_ready?: boolean
  temperature?: number
  max_tokens?: number
  created_at?: string
  updated_at?: string
  scenario_name?: string
  llm_name?: string
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
  input_snapshot?: Record<string, unknown>
  evidence_refs?: Array<Record<string, unknown>>
  created_at?: string
}

export interface AgentManagedInput {
  port_key: string
  dataset_version_id?: string
  dataset_head_id?: string
  asset_version_id?: string
  artifact_id?: string
  binding_key?: string
  expected_signature?: string
}

export interface AgentCapabilityTarget {
  kind: 'function' | 'action' | 'rule' | 'workflow'
  key: string
}

export interface AgentChatAttachment {
  asset_version_id?: string
  dataset_version_id?: string
  expected_signature?: string
  filename?: string
}

export interface AgentChatRequest {
  message: string
  conversation_id?: string
  environment?: 'dev' | 'staging' | 'prod'
  inputs?: Record<string, unknown>
  managed_inputs?: AgentManagedInput[]
  capability?: AgentCapabilityTarget
  idempotency_key?: string
  attachments?: AgentChatAttachment[]
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
  environment?: 'dev' | 'staging' | 'prod' | string
  definition_snapshot_id?: string | null
  release_id?: string | null
  definition_hash?: string
  definition_source?: 'live' | 'release' | string
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
  /** 由服务端根据当前账号与工作流 ACL 计算；缺失时前端按无权处理。 */
  can_execute: boolean
  /** 由服务端根据当前账号与工作流 ACL 计算；缺失时前端按无权处理。 */
  can_approve: boolean
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

export type AssistantCompilationJobState = 'running' | 'succeeded' | 'failed'

export interface AssistantCompilationStep {
  id: string
  title: string
  detail: string
  status: 'pending' | 'running' | 'done' | 'error'
}

export interface AssistantCompilationStageResult {
  step_id: string
  summary: string
}

export interface AssistantCompilationActivity {
  id: string
  kind: 'stage' | 'model' | 'tool' | string
  step_id?: string
  title: string
  detail: string
  status: 'pending' | 'running' | 'done' | 'error' | string
  created_at?: string
}

export interface AssistantCompilationGuidance {
  id: string
  summary: string
  attachment_names: string[]
  status: 'queued' | 'applied'
  created_at?: string
}

export interface AssistantCompilationLiveness {
  job_id: string
  thread_id?: string | null
  scenario_id?: string | null
  status: 'running'
  stream_state: 'connected' | 'disconnected'
  emitted_at: string
  elapsed_seconds: number
  phase?: string
  current_step?: string
  stage_title: string
  message: string
  calls_used: number
  call_budget: number
  draft_checkpoint_revision: number
  draft_resource_count: number
  guidance_pending_count: number
}

/** Public, owner-scoped state for recovering a long-running scenario compilation. */
export interface AssistantCompilationJobStatus {
  id: string
  thread_id?: string | null
  scenario_id?: string | null
  status: AssistantCompilationJobState
  progress: {
    phase?: string
    detail?: string
    calls_used?: number
    call_budget?: number
    error_code?: string
    current_step?: string
    steps?: AssistantCompilationStep[]
    results?: AssistantCompilationStageResult[]
    activities?: AssistantCompilationActivity[]
    guidance?: AssistantCompilationGuidance[]
    guidance_pending_count?: number
    accepting_guidance?: boolean
    draft_checkpoint_revision?: number
    draft_resource_count?: number
    draft_resource_kinds?: string[]
  }
  llm_calls_used: number
  llm_call_budget: number
  result_ready: boolean
  error_code: string
  error_message: string
  started_at: string
  completed_at?: string | null
  updated_at: string
}

export interface AssistantCompilationGuidanceResult {
  accepted: boolean
  guidance_id: string
  job: AssistantCompilationJobStatus
  message?: AssistantMessage | null
}

/** Server-owned proposal locator returned only after a compilation succeeds. */
export interface AssistantCompilationJobResult {
  job_id: string
  thread_id?: string | null
  scenario_id?: string | null
  status: 'succeeded'
  proposal: AssistantProposal | Record<string, any>
  proposal_thread_id?: string | null
  proposal_message_id?: string | null
  proposal_scope_key?: string | null
  apply_ready: boolean
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

export interface AssistantQuestion {
  id: string
  title: string
  message: string
  options?: Array<{
    label: string
    value?: string
    impact: string
    recommended?: boolean
    prompt?: string
  }>
}

export interface AssistantEvidence {
  rules_used: Array<{ id?: string; name: string; result?: string }>
  tools_called: Array<{ name: string; status?: string; purpose?: string }>
  confidence: number
  uncertainties: string[]
}

export interface AssistantActionPreview {
  target?: { id?: string; name?: string; entity_id?: string }
  parameter_schema?: Record<string, unknown>
  parameters?: Record<string, unknown>
  missing_parameters?: string[]
  impact?: { precondition?: string; postcondition?: string; executor_type?: string; side_effects_skipped?: boolean }
  permission?: Record<string, unknown>
  preview?: Record<string, any>
  requires_approval?: boolean
  execution_boundary?: string
}

/** A re-authorized source card attached to an assistant answer. */
export interface AssistantSource {
  id: string
  kind?: 'rag' | 'attachment' | string
  filename: string
  status?: string
  citation_id?: string
  data_source_id?: string
  data_source_name?: string
  file_id?: string
  chunk_id?: string
  char_start?: number
  char_end?: number
  content_hash?: string
  file_content_hash?: string
  index_version?: string
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
  kind: 'scenario' | 'ontology' | 'mapping' | 'workflow' | 'scenario_model'
  proposal_id: string
  title: string
  summary?: string
  payload: Record<string, any>
  changes?: {
    operation: 'add' | 'update' | 'delete' | 'skip' | string
    resource: string
    name: string
    summary?: string
    change_id?: string
    depends_on?: string[]
    evidence_refs?: string[]
    confidence?: number
  }[]
  base_snapshot?: Record<string, any>
  requires_confirmation?: boolean
  status?: 'pending' | 'in_progress' | 'completed_no_changes' | 'completed_with_gaps' | 'partially_applied' | 'applied' | string
  run_revision?: number
  applied_at?: string
  apply_result?: Record<string, any>
}

export interface AssistantModelTask {
  id: string
  order: number
  title: string
  description: string
  sections: string[]
  depends_on: string[]
  status: 'empty' | 'ready' | 'blocked' | 'waiting' | 'awaiting_generation' | 'applied' | 'partially_applied' | 'deferred' | 'drafted_with_gaps' | 'skipped' | string
  generation_status?: 'generated' | 'pending' | string
  waiting_for?: string[]
  change_keys: string[]
  safe_change_keys: string[]
  change_count: number
  output_count?: number
  draft_output_count?: number
  draft_candidate_count?: number
  issue_count?: number
  safe_change_count: number
  blocked_issue_count: number
  compiled_safe_change_count?: number
  compiled_blocked_issue_count?: number
  draft_status?: 'generated' | 'empty' | string
  issues?: Array<{
    code: string
    message: string
    source_refs?: string[]
    blocking?: boolean
    resolution_hint?: string
    affected_change_keys?: string[]
  }>
  apply_result?: AssistantModelTaskApplyResult
  applied_at?: string
  completed_at?: string
}

export interface AssistantModelTaskApplyResult {
  kind?: string
  task_id?: string
  task_status?: string
  counts?: Record<string, number>
  applied_change_keys?: string[]
  safe_change_count?: number
  partial?: boolean
  deferred?: boolean
  draft_preserved?: boolean
  [key: string]: unknown
}

export interface AssistantModelExecutionSummary {
  final: boolean
  status: 'running' | 'waiting_for_confirmation' | 'waiting_for_generation' | 'completed' | 'completed_no_changes' | 'completed_with_gaps' | 'state_error' | string
  message: string
  total_task_count: number
  completed_task_count: number
  applied_task_count: number
  partially_applied_task_count: number
  draft_only_task_count: number
  empty_task_count: number
  current_task_id: string
  current_task_title: string
  remaining_issue_count: number
  remaining_issue_group_count?: number
  blocking_issue_count: number
  issue_groups?: Array<{
    cause: string
    code: string
    message: string
    count: number
    blocking_count: number
    affected_count: number
    resolution_hint?: string
  }>
  remaining_issues?: Array<Record<string, any>>
  resolution_hints?: string[]
}

export interface AssistantModelNextAction {
  type: 'confirm_task' | 'generate_task' | 'refine_model' | 'rebuild_plan' | string
  task_id?: string
  task_title?: string
  requires_confirmation?: boolean
  can_apply?: boolean
  can_apply_partial?: boolean
  can_defer?: boolean
  can_generate?: boolean
  message?: string
}

export interface AssistantProposalApplyResult {
  ok?: boolean
  status?: 'applied' | 'partially_applied' | 'replayed' | string
  message?: string
  data?: Record<string, any>
  proposal?: AssistantProposal
  task_update_text?: string
  execution_summary?: AssistantModelExecutionSummary
  next_action?: AssistantModelNextAction
}

export interface AssistantThought {
  id: string
  title: string
  detail?: string
  status?: 'pending' | 'running' | 'done' | 'error'
}

export interface AssistantMessage {
  id?: string
  thread_id?: string
  role: 'user' | 'assistant' | 'system'
  content: string
  context?: Record<string, any>
  attachments?: AssistantAttachment[] | Record<string, any>[]
  proposal?: AssistantProposal | Record<string, any>
  questions?: AssistantQuestion[]
  sources?: AssistantSource[]
  evidence?: AssistantEvidence
  action_preview?: AssistantActionPreview
  thinking?: AssistantThought[]
  streaming?: boolean
  created_at?: string
}

export interface AssistantReply {
  thread_id: string
  reply: string
  proposal?: AssistantProposal | Record<string, any>
  questions?: AssistantQuestion[]
  suggestions?: string[]
  sources?: AssistantSource[]
  evidence?: AssistantEvidence
  action_preview?: AssistantActionPreview
}

export interface ChatEvent {
  type: string
  data: any
}
