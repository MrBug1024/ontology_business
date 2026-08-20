import axios from 'axios'
import type {
  Agent,
  AssistantAttachment,
  AssistantMessage,
  AssistantReply,
  AssistantThread,
  AuthMessage,
  BucketFile,
  Conversation,
  DataMapping,
  DataSource,
  GraphData,
  LLMConfig,
  MCPConfig,
  MCPTool,
  OntologyInstance,
  RelationInstance,
  Scenario,
  ScenarioDetail,
  Skill,
  TableInfo,
  User,
} from '@/types'

// 响应拦截器已把 r.data 解包，因此客户端方法在类型上直接返回 Promise<T>
interface ApiClient {
  get<T = any>(url: string, config?: any): Promise<T>
  post<T = any>(url: string, data?: any, config?: any): Promise<T>
  put<T = any>(url: string, data?: any, config?: any): Promise<T>
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
    return Promise.reject(new Error(typeof msg === 'string' ? msg : JSON.stringify(msg)))
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
    mode?: 'ask' | 'draft' | 'execute'
  }) => http.post<AssistantReply>('/assistant/chat', d),
  applyAssistantProposal: (d: { kind: 'ontology' | 'workflow'; scenario_id: string; thread_id?: string; payload: any }) =>
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

  // 数据映射
  createMapping: (sid: string, d: Partial<DataMapping>) => http.post(`/scenarios/${sid}/mappings`, d),
  deleteMapping: (mid: string) => http.delete(`/scenarios/mappings/${mid}`),
  importMapping: (mid: string, limit = 50) => http.post(`/scenarios/mappings/${mid}/import`, { limit }),

  // 操作（Actions）
  createAction: (sid: string, d: any) => http.post(`/scenarios/${sid}/actions`, d),
  updateAction: (id: string, d: any) => http.put(`/scenarios/actions/${id}`, d),
  deleteAction: (id: string) => http.delete(`/scenarios/actions/${id}`),
  executeAction: (id: string, params: any) => http.post(`/scenarios/actions/${id}/execute`, { params }),

  // 规则（Rules）
  createRule: (sid: string, d: any) => http.post(`/scenarios/${sid}/rules`, d),
  updateRule: (id: string, d: any) => http.put(`/scenarios/rules/${id}`, d),
  deleteRule: (id: string) => http.delete(`/scenarios/rules/${id}`),
  evaluateRule: (id: string, record: any) => http.post(`/scenarios/rules/${id}/evaluate`, { record }),

  // 事件（Events）
  createEvent: (sid: string, d: any) => http.post(`/scenarios/${sid}/events`, d),
  updateEvent: (id: string, d: any) => http.put(`/scenarios/events/${id}`, d),
  deleteEvent: (id: string) => http.delete(`/scenarios/events/${id}`),

  // 工作流（Workflows）
  createWorkflow: (sid: string, d: any) => http.post(`/scenarios/${sid}/workflows`, d),
  updateWorkflow: (id: string, d: any) => http.put(`/scenarios/workflows/${id}`, d),
  deleteWorkflow: (id: string) => http.delete(`/scenarios/workflows/${id}`),
  executeWorkflow: (id: string, params: any) => http.post(`/scenarios/workflows/${id}/execute`, { params }),
  generateWorkflow: (sid: string, description: string) =>
    http.post<{ name: string; description: string; nodes: any[]; edges: any[] }>(
      `/scenarios/${sid}/workflows/generate`,
      { description },
    ),

  // 执行日志
  listExecutionLogs: (sid: string) => http.get(`/scenarios/${sid}/execution-logs`),

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

  // Agent
  listAgents: () => http.get<Agent[]>('/agents'),
  getAgent: (id: string) => http.get<Agent>(`/agents/${id}`),
  createAgent: (d: Partial<Agent>) => http.post<Agent>('/agents', d),
  updateAgent: (id: string, d: Partial<Agent>) => http.put<Agent>(`/agents/${id}`, d),
  deleteAgent: (id: string) => http.delete(`/agents/${id}`),
  listConversations: (agentId: string) => http.get<Conversation[]>(`/agents/${agentId}/conversations`),
  createConversation: (agentId: string) => http.post<Conversation>(`/agents/${agentId}/conversations`),
  deleteConversation: (cid: string) => http.delete(`/agents/conversations/${cid}`),
  listMessages: (cid: string) => http.get(`/agents/conversations/${cid}/messages`),
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
    mode?: 'ask' | 'draft' | 'execute'
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
