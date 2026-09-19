export type LibraryType = 'postgres' | 'mysql' | 'sqlite3' | 'file_bucket'

export interface LibraryWrite {
  name: string
  type: LibraryType
  scenario_id: string | null
  config: {
    host?: string
    port?: number
    database?: string
    user?: string
    password?: string
  }
}

export interface LibraryForm {
  name: string
  type: LibraryType
  scenario_id: string
  host: string
  port: number | undefined
  database: string
  user: string
  password: string
}
