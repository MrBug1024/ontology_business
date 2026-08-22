import axios from 'axios'
import type {
  ActionExecutionLog,
  AdvancedAsset,
  AdvancedAssetSummary,
  AdvancedFeedback,
  AdvancedRecord,
  AdvancedRecordPage,
  AdvancedRun,
  Agent,
  AssistantAttachment,
  AssistantMessage,
  AssistantReply,
  AssistantThread,
  AuthMessage,
  BucketFile,
  ChatMessage,
  Conversation,
  ConnectorBinding,
  ConnectorCatalogItem,
  ConnectorReadiness,
  DataMapping,
  DataMappingPreview,
  DataMappingRefreshJob,
  DataMappingRefresh,
  DocumentReindexResult,
  DocumentSearchResult,
  DataSource,
  EventEnvelope,
  FunctionDefinition,
  GraphData,
  IncidentCase,
  IncidentCaseCreateInput,
  IncidentCaseHistory,
  IncidentCaseUpdateInput,
  LineageGraph,
  ReleaseBranch,
  ReleaseProposal,
  ReleaseRecord,
  ReleaseReview,
  ReleaseRollback,
  ReleaseSnapshot,
  LLMConfig,
  LLMEvaluation,
  LLMEvaluationSummary,
  LLMTrace,
  LLMUsageSummary,
  MCPConfig,
  MCPTool,
  OntologyInstance,
  OntologyResourcePackage,
  PackageImportProposal,
  PackageImportPreview,
  StarterKit,
  StarterKitImportProposal,
  RelationInstance,
  ObjectDetail,
  ObjectSearchResult,
  Organization,
  OrganizationMember,
  OrganizationRole,
  PermissionGrant,
  PermissionGrantInput,
  PermissionResource,
  Scenario,
  ScenarioDetail,
  Skill,
  TableInfo,
  User,
  WorkflowApproval,
  WorkflowRun,
} from '@/types'

// 响应拦截器已把 r.data 解包，因此客户端方法在类型上直接返回 Promise<T>
interface ApiClient {
  get<T = any>(url: string, config?: any): Promise<T>
  post<T = any>(url: string, data?: any, config?: any): Promise<T>
  put<T = any>(url: string, data?: any, config?: any): Promise<T>
  patch<T = any>(url: string, data?: any, config?: any): Promise<T>
  delete<T = any>(url: string, config?: any): Promise<T>
}

const instance = axios.create({ baseURL: '/api', timeout: 120000, withCredentials: true })

instance.interceptors.response.use(
  (r) => r.data,
  (err) => {
    if (err.response?.status === 401 && !String(err.config?.url || '').startsWith('/auth') && window.location.pathname !== '/login') {
      window.location.assign('/login')
    }
    const msg = err.response?.data?.detail || err.message || '请求失败'
    const error = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg)) as Error & { status?: number }
    error.status = Number(err.response?.status || 0) || undefined
    return Promise.reject(error)
  },
)

const http = instance as unknown as ApiClient

// ── 场景 & 本体 ──────────────────────────────
export const api = {
  // 认证
  me: () => http.get<User>('/auth/me'),
  register: (d: { email: string; password: string; password_confirm: string; display_name?: string }) =>
    http.post<AuthMessage>('/auth/register', d),
  verifyEmail: (d: { email: string; code: string }) => http.post<AuthMessage>('/auth/verify-email', d),
  resendCode: (email: string) => http.post<AuthMessage>('/auth/resend-code', { email }),
  login: (d: { email: string; password: string }) => http.post<User>('/auth/login', d),
  logout: () => http.post<AuthMessage>('/auth/logout'),
  forgotPassword: (email: string) => http.post<AuthMessage>('/auth/forgot-password', { email }),
  resetPassword: (d: { email: string; code: string; password: string; password_confirm: string }) =>
    http.post<AuthMessage>('/auth/reset-password', d),

  // 组织、角色与细粒度授权（服务端是唯一的权限判定点；管理能力由 API 统一校验）
  getOrganization: () => http.get<Organization>('/permissions/organization'),
  listOrganizationRoles: () => http.get<OrganizationRole[]>('/permissions/roles'),
  listOrganizationMembers: () => http.get<OrganizationMember[]>('/permissions/members'),
  saveOrganizationMember: (data: { user_id: string; role_key: string }) =>
    http.post<OrganizationMember>('/permissions/members', data),
  removeOrganizationMember: (memberId: string) => http.delete<AuthMessage>(`/permissions/members/${memberId}`),
  listPermissionGrants: () => http.get<PermissionGrant[]>('/permissions/grants'),
  createPermissionGrant: (data: PermissionGrantInput) => http.post<PermissionGrant>('/permissions/grants', data),
  deletePermissionGrant: (grantId: string) => http.delete<AuthMessage>(`/permissions/grants/${grantId}`),
  listPermissionResources: (scenarioId: string) =>
    http.get<PermissionResource[]>(`/permissions/resources/${scenarioId}`),

  // P1 运营 Case / Incident 中心
  listIncidents: (scenarioId: string, params: { status?: string; severity?: string; limit?: number } = {}) =>
    http.get<IncidentCase[]>(`/incidents/scenarios/${scenarioId}`, { params }),
  createIncident: (scenarioId: string, data: IncidentCaseCreateInput) =>
    http.post<IncidentCase>(`/incidents/scenarios/${scenarioId}`, data),
  getIncident: (incidentId: string) => http.get<IncidentCase>(`/incidents/${incidentId}`),
  updateIncident: (incidentId: string, data: IncidentCaseUpdateInput) =>
    http.patch<IncidentCase>(`/incidents/${incidentId}`, data),
  acknowledgeIncident: (incidentId: string, comment = '') =>
    http.post<IncidentCase>(`/incidents/${incidentId}/acknowledge`, { comment }),
  resolveIncident: (incidentId: string, data: { resolution: string; comment?: string }) =>
    http.post<IncidentCase>(`/incidents/${incidentId}/resolve`, data),
  listIncidentHistory: (incidentId: string) =>
    http.get<IncidentCaseHistory[]>(`/incidents/${incidentId}/history`),

  // 全局 AI 助手
  listAssistantThreads: (context: { scenario_id?: string; page?: string; path?: string } = {}) =>
    http.get<AssistantThread[]>('/assistant/threads', { params: {
      scenario_id: context.scenario_id || undefined,
      page: context.page || undefined,
      path: context.path || undefined,
    } }),
  createAssistantThread: (context: { scenario_id?: string; page?: string; path?: string } = {}) =>
    http.post<AssistantThread>('/assistant/threads', undefined, { params: {
      scenario_id: context.scenario_id || undefined,
      page: context.page || undefined,
      path: context.path || undefined,
    } }),
  listAssistantMessages: (threadId: string, context: { scenario_id?: string; page?: string; path?: string } = {}) =>
    http.get<AssistantMessage[]>(`/assistant/threads/${threadId}/messages`, { params: {
      scenario_id: context.scenario_id || undefined,
      page: context.page || undefined,
      path: context.path || undefined,
    } }),
  deleteAssistantThread: (threadId: string, context: { scenario_id?: string; page?: string; path?: string } = {}) =>
    http.delete(`/assistant/threads/${threadId}`, { params: {
      scenario_id: context.scenario_id || undefined,
      page: context.page || undefined,
      path: context.path || undefined,
    } }),
  uploadAssistantAttachment: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return http.post<AssistantAttachment>('/assistant/attachments', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  deleteAssistantAttachment: (id: string) => http.delete(`/assistant/attachments/${id}`),
  assistantChat: (d: {
    message: string
    thread_id?: string
    scenario_id?: string
    page?: string
    path?: string
    selection?: Record<string, any>
    attachment_ids?: string[]
    mode?: 'ask' | 'explain' | 'draft' | 'apply' | 'execute'
  }) => http.post<AssistantReply>('/assistant/chat', d),
  applyAssistantProposal: (d: { kind: 'scenario' | 'ontology' | 'mapping' | 'workflow'; scenario_id?: string; thread_id: string; proposal_id: string; confirm: boolean }) =>
    http.post('/assistant/proposals/apply', d),

  // 场景
  listScenarios: () => http.get<Scenario[]>('/scenarios'),
  getScenario: (id: string) => http.get<ScenarioDetail>(`/scenarios/${id}`),
  createScenario: (d: Partial<Scenario>) => http.post<Scenario>('/scenarios', d),
  updateScenario: (id: string, d: Partial<Scenario>) => http.put<Scenario>(`/scenarios/${id}`, d),
  deleteScenario: (id: string) => http.delete(`/scenarios/${id}`),

  // 实体
  createEntity: (sid: string, d: any) => http.post(`/scenarios/${sid}/entities`, d),
  updateEntity: (eid: string, d: any) => http.put(`/scenarios/entities/${eid}`, d),
  deleteEntity: (eid: string) => http.delete(`/scenarios/entities/${eid}`),

  // 关系
  createRelation: (sid: string, d: any) => http.post(`/scenarios/${sid}/relations`, d),
  updateRelation: (rid: string, d: any) => http.put(`/scenarios/relations/${rid}`, d),
  deleteRelation: (rid: string) => http.delete(`/scenarios/relations/${rid}`),

  // 图谱
  getGraph: (sid: string, mode: 'schema' | 'instance' = 'schema') =>
    http.get<GraphData>(`/scenarios/${sid}/graph`, { params: { mode } }),
  getScenarioLineage: (sid: string, limit = 300) =>
    http.get<LineageGraph>(`/lineage/scenarios/${sid}`, { params: { limit } }),

  // P2 发布治理
  listReleaseScenarios: () => http.get<Scenario[]>('/releases/scenarios'),
  listReleaseBranches: (scenarioId: string) =>
    http.get<ReleaseBranch[]>(`/releases/scenarios/${scenarioId}/branches`),
  createReleaseBranch: (scenarioId: string, data: { name: string; description?: string }) =>
    http.post<ReleaseBranch>(`/releases/scenarios/${scenarioId}/branches`, data),
  getReleaseBranch: (branchId: string) => http.get<ReleaseBranch>(`/releases/branches/${branchId}`),
  getReleaseSnapshot: (snapshotId: string) => http.get<ReleaseSnapshot>(`/releases/snapshots/${snapshotId}`),
  listReleaseProposals: (scenarioId: string, params: { branch_id?: string; status?: string } = {}) =>
    http.get<ReleaseProposal[]>(`/releases/scenarios/${scenarioId}/proposals`, { params }),
  createReleaseProposal: (branchId: string, data: { title: string; description?: string; content: Record<string, any>; submit?: boolean }) =>
    http.post<ReleaseProposal>(`/releases/branches/${branchId}/proposals`, data),
  submitReleaseProposal: (proposalId: string) =>
    http.post<ReleaseProposal>(`/releases/proposals/${proposalId}/submit`),
  reviewReleaseProposal: (proposalId: string, data: { decision: 'approve' | 'reject'; comment?: string }) =>
    http.post<ReleaseReview>(`/releases/proposals/${proposalId}/reviews`, data),
  mergeReleaseProposal: (proposalId: string, note = '') =>
    http.post<ReleaseProposal>(`/releases/proposals/${proposalId}/merge`, { confirmed: true, note }),
  listReleaseRecords: (scenarioId: string, environment?: 'dev' | 'staging' | 'prod') =>
    http.get<ReleaseRecord[]>(`/releases/scenarios/${scenarioId}/publish`, { params: { environment } }),
  publishRelease: (scenarioId: string, data: { environment: 'dev' | 'staging' | 'prod'; branch_id?: string; proposal_id?: string; snapshot_id?: string; notes?: string }) =>
    http.post<ReleaseRecord>(`/releases/scenarios/${scenarioId}/publish`, { ...data, confirmed: true }),
  rollbackRelease: (scenarioId: string, data: { target_snapshot_id: string; branch_id?: string; environment?: 'dev' | 'staging' | 'prod'; reason?: string }) =>
    http.post<ReleaseRollback>(`/releases/scenarios/${scenarioId}/rollback`, { ...data, confirmed: true }),

  // P2 标准连接器与环境绑定：只关联既有配置，不返回也不传递任何凭据。
  listConnectors: (scenarioId: string) =>
    http.get<ConnectorCatalogItem[]>('/connectors', { params: { scenario_id: scenarioId } }),
  listConnectorBindings: (scenarioId: string, environment?: 'dev' | 'staging' | 'prod') =>
    http.get<ConnectorBinding[]>(`/connectors/scenarios/${scenarioId}/bindings`, { params: { environment } }),
  saveConnectorBinding: (scenarioId: string, data: {
    environment: 'dev' | 'staging' | 'prod'
    binding_key: string
    kind: 'data_source' | 'mcp' | 'llm'
    connector_id: string
    reference_label?: string
    check?: boolean
  }) => http.put<ConnectorBinding>(`/connectors/scenarios/${scenarioId}/bindings`, data),
  checkConnectorBinding: (scenarioId: string, bindingId: string) =>
    http.post<ConnectorBinding>(`/connectors/scenarios/${scenarioId}/bindings/${bindingId}/check`),
  deleteConnectorBinding: (scenarioId: string, bindingId: string) =>
    http.delete(`/connectors/scenarios/${scenarioId}/bindings/${bindingId}`),
  getConnectorReadiness: (scenarioId: string, data: {
    snapshot_id: string
    environment: 'dev' | 'staging' | 'prod'
  }) => http.get<ConnectorReadiness>(`/connectors/scenarios/${scenarioId}/readiness`, { params: data }),

  // P2 可移植资源包：仅导出与导入预检，实际应用必须进入发布提案流程。
  exportScenarioPackage: (scenarioId: string) =>
    http.get<OntologyResourcePackage>(`/packages/scenarios/${scenarioId}/export`),
  validateResourcePackage: (resourcePackage: Record<string, any>) =>
    http.post<{ valid: boolean; errors: Array<Record<string, any>>; warnings: Array<Record<string, any>>; normalized: OntologyResourcePackage; fingerprint: string }>('/packages/validate', { package: resourcePackage }),
  previewScenarioPackageImport: (scenarioId: string, resourcePackage: Record<string, any>, environment: 'dev' | 'staging' | 'prod' = 'dev') =>
    http.post<PackageImportPreview>(`/packages/scenarios/${scenarioId}/import-preview`, { package: resourcePackage, environment }),
  createPackageImportProposal: (scenarioId: string, data: {
    package: Record<string, any>
    branch_id: string
    environment?: 'dev' | 'staging' | 'prod'
    title: string
    description?: string
    submit?: boolean
  }) => http.post<PackageImportProposal>(`/packages/scenarios/${scenarioId}/import-proposal`, data),
  listStarterKits: () => http.get<StarterKit[]>('/starter-kits'),
  getStarterKit: (starterKitId: string) => http.get<StarterKit>(`/starter-kits/${starterKitId}`),
  previewStarterKitImport: (starterKitId: string, scenarioId: string, environment: 'dev' | 'staging' | 'prod' = 'dev') =>
    http.post<PackageImportPreview>(`/starter-kits/${starterKitId}/scenarios/${scenarioId}/import-preview`, { environment }),
  createStarterKitImportProposal: (starterKitId: string, scenarioId: string, data: {
    branch_id: string
    environment?: 'dev' | 'staging' | 'prod'
    expected_fingerprint: string
    title: string
    description?: string
    submit?: boolean
  }) => http.post<StarterKitImportProposal>(`/starter-kits/${starterKitId}/scenarios/${scenarioId}/import-proposal`, data),

  // AI 生成本体
  generateOntology: (sid: string, description: string) =>
    http.post<{ entities: any[]; relations: any[] }>(`/scenarios/${sid}/generate-ontology`, { description }),
  applyOntology: (sid: string, data: { entities: any[]; relations: any[] }) =>
    http.post(`/scenarios/${sid}/apply-ontology`, data),

  // 实例
  createInstance: (sid: string, d: Partial<OntologyInstance>) => http.post(`/scenarios/${sid}/instances`, d),
  updateInstance: (iid: string, d: Partial<OntologyInstance>) => http.put(`/scenarios/instances/${iid}`, d),
  deleteInstance: (iid: string) => http.delete(`/scenarios/instances/${iid}`),

  // 关系实例
  createRelationInstance: (sid: string, d: Partial<RelationInstance>) =>
    http.post(`/scenarios/${sid}/relation-instances`, d),
  deleteRelationInstance: (rid: string) => http.delete(`/scenarios/relation-instances/${rid}`),

  // 对象运行时
  searchObjects: (sid: string, params: { q?: string; entity_id?: string; limit?: number; offset?: number } = {}) =>
    http.get<ObjectSearchResult>(`/scenarios/${sid}/objects`, { params }),
  getObject: (sid: string, objectId: string) =>
    http.get<ObjectDetail>(`/scenarios/${sid}/objects/${objectId}`),

  // 数据映射
  createMapping: (sid: string, d: Partial<DataMapping>) => http.post(`/scenarios/${sid}/mappings`, d),
  deleteMapping: (mid: string) => http.delete(`/scenarios/mappings/${mid}`),
  previewMapping: (mid: string, limit = 20) => http.post<DataMappingPreview>(`/scenarios/mappings/${mid}/preview`, { limit }),
  testMapping: (mid: string, limit = 20) => http.post<DataMappingPreview>(`/scenarios/mappings/${mid}/test`, { limit }),
  enqueueMappingRefresh: (mid: string, limit = 50) => http.post<DataMappingRefreshJob>(`/scenarios/mappings/${mid}/refresh-jobs`, { limit }),
  getMappingRefreshJob: (jobId: string) => http.get<DataMappingRefreshJob>(`/scenarios/mappings/refresh-jobs/${jobId}`),
  refreshMapping: (mid: string, limit = 50) => http.post<DataMappingRefresh>(`/scenarios/mappings/${mid}/refresh`, { limit }),
  importMapping: (mid: string, limit = 50) => http.post(`/scenarios/mappings/${mid}/import`, { limit }),

  // 受治理函数：声明式契约 + 服务端 allowlist 内置算子
  listFunctions: (sid: string) => http.get<FunctionDefinition[]>(`/scenarios/${sid}/functions`),
  createFunction: (sid: string, d: FunctionDefinition) => http.post<FunctionDefinition>(`/scenarios/${sid}/functions`, d),
  updateFunction: (id: string, d: FunctionDefinition) => http.put<FunctionDefinition>(`/scenarios/functions/${id}`, d),
  deleteFunction: (id: string) => http.delete(`/scenarios/functions/${id}`),
  runFunction: (id: string, data: { params: Record<string, any>; idempotency_key?: string }) =>
    http.post<AdvancedRun>(`/advanced/functions/${id}/run`, data),
  listFunctionRuns: (id: string, limit = 100) =>
    http.get<AdvancedRun[]>(`/advanced/functions/${id}/runs`, { params: { limit } }),

  // P2 高级数据 / 模型资产：记录、实时游标、媒体、仿真与反馈均受场景 ACL 保护。
  listAdvancedAssets: (scenarioId: string, kind?: string) =>
    http.get<AdvancedAsset[]>(`/advanced/scenarios/${scenarioId}/assets`, { params: { kind: kind || undefined } }),
  createAdvancedAsset: (scenarioId: string, data: Partial<AdvancedAsset>) =>
    http.post<AdvancedAsset>(`/advanced/scenarios/${scenarioId}/assets`, data),
  updateAdvancedAsset: (assetId: string, data: Partial<AdvancedAsset>) =>
    http.patch<AdvancedAsset>(`/advanced/assets/${assetId}`, data),
  deleteAdvancedAsset: (assetId: string) => http.delete(`/advanced/assets/${assetId}`),
  getAdvancedAssetSummary: (assetId: string) => http.get<AdvancedAssetSummary>(`/advanced/assets/${assetId}/summary`),
  listAdvancedRecords: (assetId: string, params: { after_sequence?: number; from_time?: string; to_time?: string; event_type?: string; bbox?: string; limit?: number } = {}) =>
    http.get<AdvancedRecordPage>(`/advanced/assets/${assetId}/records`, { params }),
  createAdvancedRecord: (assetId: string, data: Partial<AdvancedRecord>) =>
    http.post<AdvancedRecord>(`/advanced/assets/${assetId}/records`, data),
  uploadAdvancedMedia: (assetId: string, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return http.post<AdvancedRecord>(`/advanced/assets/${assetId}/media`, fd, { headers: { 'Content-Type': 'multipart/form-data' } })
  },
  advancedMediaUrl: (assetId: string, recordId: string) => `/api/advanced/assets/${assetId}/media/${recordId}`,
  runAdvancedAsset: (assetId: string, runType: string, data: { params: Record<string, any>; idempotency_key?: string }) =>
    http.post<AdvancedRun>(`/advanced/assets/${assetId}/runs`, data, { params: { run_type: runType } }),
  listAdvancedRuns: (assetId: string, limit = 100) => http.get<AdvancedRun[]>(`/advanced/assets/${assetId}/runs`, { params: { limit } }),
  createAdvancedFeedback: (assetId: string, data: Partial<AdvancedFeedback>) =>
    http.post<AdvancedFeedback>(`/advanced/assets/${assetId}/feedback`, data),
  listAdvancedFeedback: (assetId: string, limit = 100) => http.get<AdvancedFeedback[]>(`/advanced/assets/${assetId}/feedback`, { params: { limit } }),

  // 操作（Actions）
  createAction: (sid: string, d: any) => http.post(`/scenarios/${sid}/actions`, d),
  updateAction: (id: string, d: any) => http.put(`/scenarios/actions/${id}`, d),
  deleteAction: (id: string) => http.delete(`/scenarios/actions/${id}`),
  executeAction: (id: string, payload: {
    params: any
    dry_run?: boolean
    confirm?: boolean
    idempotency_key?: string
    preview_log_id?: string
    correlation_id?: string
    expected_environment?: 'dev' | 'staging' | 'prod'
    expected_definition_snapshot_id?: string
    expected_release_id?: string
    expected_definition_hash?: string
  }) =>
    http.post(`/scenarios/actions/${id}/execute`, payload),

  // 规则（Rules）
  createRule: (sid: string, d: any) => http.post(`/scenarios/${sid}/rules`, d),
  updateRule: (id: string, d: any) => http.put(`/scenarios/rules/${id}`, d),
  deleteRule: (id: string) => http.delete(`/scenarios/rules/${id}`),
  evaluateRule: (id: string, record: any) => http.post(`/scenarios/rules/${id}/evaluate`, { record }),

  // 事件（Events）
  createEvent: (sid: string, d: any) => http.post(`/scenarios/${sid}/events`, d),
  updateEvent: (id: string, d: any) => http.put(`/scenarios/events/${id}`, d),
  deleteEvent: (id: string) => http.delete(`/scenarios/events/${id}`),
  publishEvent: (id: string, d: { payload?: Record<string, any>; dedupe_key?: string } = {}) =>
    http.post<EventEnvelope>(`/scenarios/events/${id}/publish`, d),

  // 工作流（Workflows）
  createWorkflow: (sid: string, d: any) => http.post(`/scenarios/${sid}/workflows`, d),
  updateWorkflow: (id: string, d: any) => http.put(`/scenarios/workflows/${id}`, d),
  deleteWorkflow: (id: string) => http.delete(`/scenarios/workflows/${id}`),
  executeWorkflow: (id: string, params: any) => http.post(`/scenarios/workflows/${id}/execute`, { params }),
  submitWorkflowRun: (id: string, params: Record<string, any> = {}) =>
    http.post<WorkflowRun>(`/scenarios/workflows/${id}/runs`, { params }),
  generateWorkflow: (sid: string, description: string) =>
    http.post<{ name: string; description: string; nodes: any[]; edges: any[] }>(
      `/scenarios/${sid}/workflows/generate`,
      { description },
    ),

  // P1 任务运行与审批
  listTasks: (params: { scenario_id?: string; status?: string; limit?: number } = {}) =>
    http.get<WorkflowRun[]>('/tasks', { params }),
  getTask: (id: string) => http.get<WorkflowRun>(`/tasks/${id}`),
  listTaskApprovals: (params: { scenario_id?: string } = {}) =>
    http.get<WorkflowApproval[]>('/tasks/approvals', { params }),
  approveTask: (id: string, comment = '') => http.post<WorkflowRun>(`/tasks/${id}/approve`, { comment }),
  rejectTask: (id: string, comment = '') => http.post<WorkflowRun>(`/tasks/${id}/reject`, { comment }),
  retryTask: (id: string) => http.post<WorkflowRun>(`/tasks/${id}/retry`),
  cancelTask: (id: string) => http.post<WorkflowRun>(`/operations/runs/${id}/cancel`),

  // 执行日志（P0 兼容）
  listExecutionLogs: (sid: string, limit = 50) =>
    http.get<ActionExecutionLog[]>(`/scenarios/${sid}/execution-logs`, { params: { limit } }),

  // 数据源
  listDataSources: (sid?: string) =>
    http.get<DataSource[]>('/data-sources', { params: sid ? { scenario_id: sid } : {} }),
  createDataSource: (d: Partial<DataSource>) => http.post<DataSource>('/data-sources', d),
  updateDataSource: (id: string, d: Partial<DataSource>) => http.put<DataSource>(`/data-sources/${id}`, d),
  deleteDataSource: (id: string) => http.delete(`/data-sources/${id}`),
  testDataSource: (id: string) => http.post(`/data-sources/${id}/test`),
  listTables: (id: string) => http.get<TableInfo[]>(`/data-sources/${id}/tables`),
  query: (id: string, sql: string) => http.post(`/data-sources/${id}/query`, { sql }),
  listFiles: (id: string) => http.get<BucketFile[]>(`/data-sources/${id}/files`),
  searchDocuments: (d: { query: string; data_source_ids?: string[]; scenario_id?: string; top_k?: number }) =>
    http.post<DocumentSearchResult>('/data-sources/search', d),
  reindexFiles: (id: string) => http.post<DocumentReindexResult>(`/data-sources/${id}/reindex`),
  uploadFiles: (id: string, files: File[]) => {
    const fd = new FormData()
    files.forEach((f) => fd.append('files', f))
    return http.post<BucketFile[]>(`/data-sources/${id}/files`, fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  reparseFile: (fid: string) => http.post(`/data-sources/files/${fid}/reparse`),
  fileText: (fid: string) => http.get<{ filename: string; text: string }>(`/data-sources/files/${fid}/text`),
  deleteFile: (fid: string) => http.delete(`/data-sources/files/${fid}`),

  // LLM
  listLLM: () => http.get<LLMConfig[]>('/llm-configs'),
  createLLM: (d: Partial<LLMConfig>) => http.post<LLMConfig>('/llm-configs', d),
  updateLLM: (id: string, d: Partial<LLMConfig>) => http.put<LLMConfig>(`/llm-configs/${id}`, d),
  deleteLLM: (id: string) => http.delete(`/llm-configs/${id}`),
  testLLM: (id: string) => http.post(`/llm-configs/${id}/test`),
  resolveLLM: (capability: 'chat' | 'embedding' | 'vision' | 'tool' = 'chat') =>
    http.get<{ capability: string; selected: LLMConfig; candidates: LLMConfig[] }>('/llm-configs/resolve', { params: { capability } }),
  listLLMTraces: (id: string, limit = 100) => http.get<LLMTrace[]>(`/llm-configs/${id}/traces`, { params: { limit } }),
  getLLMUsageSummary: (id: string, days = 30) => http.get<LLMUsageSummary>(`/llm-configs/${id}/usage-summary`, { params: { days } }),
  listLLMEvaluations: (id: string, limit = 100) => http.get<LLMEvaluation[]>(`/llm-configs/${id}/evaluations`, { params: { limit } }),
  createLLMEvaluation: (id: string, data: Partial<LLMEvaluation>) => http.post<LLMEvaluation>(`/llm-configs/${id}/evaluations`, data),
  getLLMEvaluationSummary: (id: string) => http.get<LLMEvaluationSummary>(`/llm-configs/${id}/evaluation-summary`),

  // Skill
  listSkills: () => http.get<Skill[]>('/skills'),
  rescanSkills: () => http.post('/skills/rescan'),
  toggleSkill: (id: string, enabled: boolean) => http.put<Skill>(`/skills/${id}`, { enabled }),
  executeSkill: (id: string, args: string[]) => http.post(`/skills/${id}/execute`, { args }),

  // MCP
  listMCP: () => http.get<MCPConfig[]>('/mcp'),
  createMCP: (d: Partial<MCPConfig>) => http.post<MCPConfig>('/mcp', d),
  updateMCP: (id: string, d: Partial<MCPConfig>) => http.put<MCPConfig>(`/mcp/${id}`, d),
  deleteMCP: (id: string) => http.delete(`/mcp/${id}`),
  testMCP: (id: string) => http.post(`/mcp/${id}/test`),
  mcpTools: (id: string) => http.get<MCPTool[]>(`/mcp/${id}/tools`),

  // Connector runtime (read-only deployment context; callers cannot switch it)
  getRuntimeEnvironment: () => http.get<{ environment: 'dev' | 'staging' | 'prod' }>('/connectors/runtime-environment'),

  // Agent
  listAgents: () => http.get<Agent[]>('/agents'),
  getAgent: (id: string) => http.get<Agent>(`/agents/${id}`),
  createAgent: (d: Partial<Agent>) => http.post<Agent>('/agents', d),
  updateAgent: (id: string, d: Partial<Agent>) => http.put<Agent>(`/agents/${id}`, d),
  deleteAgent: (id: string) => http.delete(`/agents/${id}`),
  listConversations: (agentId: string) => http.get<Conversation[]>(`/agents/${agentId}/conversations`),
  createConversation: (agentId: string) => http.post<Conversation>(`/agents/${agentId}/conversations`),
  deleteConversation: (cid: string) => http.delete(`/agents/conversations/${cid}`),
  listMessages: (cid: string) => http.get<ChatMessage[]>(`/agents/conversations/${cid}/messages`),
}

// SSE 流式对话
export function streamChat(
  agentId: string,
  payload: { message: string; conversation_id?: string },
  onEvent: (ev: { type: string; data: any }) => void,
  onDone: () => void,
  onError: (e: Error) => void,
) {
  const ctrl = new AbortController()
  fetch(`/api/agents/${agentId}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(payload),
    signal: ctrl.signal,
  })
    .then(async (res) => {
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const lines = buf.split('\n')
        buf = lines.pop() || ''
        for (const line of lines) {
          const t = line.trim()
          if (!t.startsWith('data:')) continue
          const data = t.slice(5).trim()
          if (data === '[DONE]') {
            onDone()
            return
          }
          try {
            onEvent(JSON.parse(data))
          } catch {
            /* ignore */
          }
        }
      }
      onDone()
    })
    .catch((e) => {
      if (e.name !== 'AbortError') onError(e)
    })
  return ctrl
}

// 全局助手 SSE：token 流 + 安全处理摘要 + 草稿元数据
export function streamAssistantChat(
  payload: {
    message: string
    thread_id?: string
    scenario_id?: string
    page?: string
    path?: string
    selection?: Record<string, any>
    attachment_ids?: string[]
    mode?: 'ask' | 'explain' | 'draft' | 'apply' | 'execute'
  },
  onEvent: (ev: { type: string; data: any }) => void,
  onDone: () => void,
  onError: (e: Error) => void,
) {
  const ctrl = new AbortController()
  let finished = false
  const finish = () => {
    if (finished) return
    finished = true
    onDone()
  }
  fetch('/api/assistant/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    credentials: 'include',
    cache: 'no-store',
    body: JSON.stringify(payload),
    signal: ctrl.signal,
  })
    .then(async (res) => {
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const lines = buf.split('\n')
        buf = lines.pop() || ''
        for (const line of lines) {
          const text = line.trim()
          if (!text.startsWith('data:')) continue
          const data = text.slice(5).trim()
          if (data === '[DONE]') {
            finish()
            return
          }
          try {
            onEvent(JSON.parse(data))
          } catch {
            /* 忽略不完整或非 JSON SSE 行 */
          }
        }
      }
      finish()
    })
    .catch((error) => {
      if (error.name !== 'AbortError') onError(error)
    })
  return ctrl
}
