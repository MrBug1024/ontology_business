import type { DataSource } from '@/types'
import type { LibraryForm, LibraryType, LibraryWrite } from '@/types/library'

export const LIBRARY_TYPES: { value: LibraryType; label: string }[] = [
  { value: 'mysql', label: 'MySQL' }, { value: 'postgres', label: 'PostgreSQL' },
  { value: 'sqlite3', label: 'SQLite3' }, { value: 'file_bucket', label: '文件桶' },
]

export function defaultLibraryPort(type: LibraryType): number | undefined {
  return type === 'mysql' ? 3306 : type === 'postgres' ? 5432 : undefined
}

export function libraryForm(source?: DataSource | null, scenarioId = ''): LibraryForm {
  const config = source?.config || {}
  const type = LIBRARY_TYPES.some((item) => item.value === source?.type) ? source?.type as LibraryType : 'postgres'
  const configuredPort = typeof config.port === 'number' ? config.port
    : typeof config.port === 'string' && /^\d+$/.test(config.port.trim()) ? Number(config.port.trim()) : undefined
  return {
    name: source?.name || '', type, scenario_id: source ? source.scenario_id || '' : scenarioId,
    host: typeof config.host === 'string' ? config.host : '',
    port: configuredPort ?? defaultLibraryPort(type),
    database: typeof config.database === 'string' ? config.database : '',
    user: typeof config.user === 'string' ? config.user : typeof config.username === 'string' ? config.username : '',
    password: '',
  }
}

export function libraryPayload(form: LibraryForm): LibraryWrite {
  if (!form.name.trim()) throw new Error('请填写资料库名称')
  const remote = form.type === 'mysql' || form.type === 'postgres'
  if (remote && (!form.host.trim() || !form.database.trim() || !form.user.trim()
    || !form.port || !Number.isInteger(form.port) || form.port < 1 || form.port > 65535)) {
    throw new Error('请完整填写有效的数据库主机、端口、数据库和用户名')
  }
  return {
    name: form.name.trim(), type: form.type, scenario_id: form.scenario_id || null,
    config: remote ? { host: form.host.trim(), port: form.port, database: form.database.trim(),
      user: form.user.trim(), password: form.password } : {},
  }
}

export function validateSqliteFile(file: File): string {
  if (!/\.(?:db|sqlite|sqlite3)$/i.test(file.name)) return '请选择 .db、.sqlite 或 .sqlite3 文件'
  if (file.size < 100 || file.size > 32 * 1024 * 1024) return 'SQLite3 文件大小须在 100 字节至 32 MB 之间'
  return ''
}
