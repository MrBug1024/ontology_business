export type MCPTransport = 'stdio' | 'sse' | 'streamable_http'

export interface StandardMCPServerConfig {
  type?: string
  command?: string
  args?: string[]
  url?: string
  env?: Record<string, string>
  headers?: Record<string, string>
  enabled?: boolean
  disabled?: boolean
}

export interface StandardMCPImportPayload {
  mcpServers: Record<string, StandardMCPServerConfig>
}

export interface StandardMCPPreview {
  name: string
  transport: MCPTransport
  endpoint: string
  envKeys: string[]
  headerKeys: string[]
}

function stringMap(value: unknown, path: string): Record<string, string> {
  if (value === undefined) return {}
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${path} 必须是文本键值对象`)
  }
  if (Object.keys(value).length > 100) throw new Error(`${path} 最多允许 100 项`)
  const result: Record<string, string> = {}
  const seen = new Set<string>()
  const isHeaders = path.endsWith('.headers')
  for (const [key, item] of Object.entries(value)) {
    const normalizedKey = key.trim()
    if (!normalizedKey) throw new Error(`${path} 包含空键名`)
    if (typeof item !== 'string') throw new Error(`${path}.${key} 的值必须是文本`)
    if (isHeaders && !/^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(normalizedKey)) {
      throw new Error(`${path}.${normalizedKey} 不是合法的 HTTP 请求头名称`)
    }
    const identity = isHeaders ? normalizedKey.toLowerCase() : normalizedKey
    if (seen.has(identity)) throw new Error(`${path} 中存在重复键名：${normalizedKey}`)
    seen.add(identity)
    result[normalizedKey] = item
  }
  return result
}

function normalizeTransport(value: unknown, hasCommand: boolean): MCPTransport {
  const token = String(value || (hasCommand ? 'stdio' : 'http')).trim().toLowerCase()
  if (token === 'http' || token === 'streamable-http' || token === 'streamable_http') return 'streamable_http'
  if (token === 'stdio' || token === 'sse') return token
  throw new Error(`不支持的 MCP type：${token || '空值'}`)
}

export function parseStandardMCPConfig(text: string): {
  payload: StandardMCPImportPayload
  preview: StandardMCPPreview[]
} {
  let raw: unknown
  try {
    raw = JSON.parse(text)
  } catch (error) {
    const detail = error instanceof Error ? error.message : 'JSON 语法错误'
    throw new Error(`JSON 无法解析：${detail}`)
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error('配置根节点必须是对象')
  }
  const root = raw as Record<string, unknown>
  const servers = root.mcpServers
  if (!servers || typeof servers !== 'object' || Array.isArray(servers)) {
    throw new Error('缺少 mcpServers 对象')
  }
  const entries = Object.entries(servers)
  if (!entries.length) throw new Error('mcpServers 至少需要一个服务')

  const preview: StandardMCPPreview[] = []
  for (const [rawName, value] of entries) {
    const name = rawName.trim()
    const path = `mcpServers.${name || rawName}`
    if (!name) throw new Error('mcpServers 中的服务名称不能为空')
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw new Error(`${path} 必须是对象`)
    }
    const server = value as Record<string, unknown>
    const command = typeof server.command === 'string' ? server.command.trim() : ''
    const url = typeof server.url === 'string' ? server.url.trim() : ''
    const transport = normalizeTransport(server.type, Boolean(command))
    if (transport === 'stdio' && !command) throw new Error(`${path}.command 不能为空`)
    if (transport !== 'stdio') {
      if (!url) throw new Error(`${path}.url 不能为空`)
      let parsed: URL
      try {
        parsed = new URL(url)
      } catch {
        throw new Error(`${path}.url 必须是完整的 HTTP 或 HTTPS 地址`)
      }
      if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) {
        throw new Error(`${path}.url 必须是完整的 HTTP 或 HTTPS 地址`)
      }
      if (parsed.username || parsed.password) {
        throw new Error(`${path}.url 不能包含用户凭据，请改用 headers`)
      }
      for (const key of parsed.searchParams.keys()) {
        const collapsed = key.toLowerCase().replace(/[^a-z0-9]/g, '')
        if (['apikey', 'accesstoken', 'authorization', 'password', 'secret', 'token'].some((token) => collapsed.includes(token))) {
          throw new Error(`${path}.url 查询参数不能携带凭据，请改用 headers`)
        }
      }
    }
    if (server.args !== undefined && (!Array.isArray(server.args) || server.args.some((item) => typeof item !== 'string'))) {
      throw new Error(`${path}.args 必须是文本数组`)
    }
    if ((server.args as unknown[] | undefined)?.some((item) => /(?:--(?:api-?key|access-token|token|password|secret)|authorization=|password=|token=)/i.test(String(item)))) {
      throw new Error(`${path}.args 不能携带凭据，请改用 env`)
    }
    const env = stringMap(server.env, `${path}.env`)
    const headers = stringMap(server.headers, `${path}.headers`)
    preview.push({
      name,
      transport,
      endpoint: transport === 'stdio' ? command : url,
      envKeys: Object.keys(env).sort(),
      headerKeys: Object.keys(headers).sort(),
    })
  }
  return { payload: raw as StandardMCPImportPayload, preview }
}
