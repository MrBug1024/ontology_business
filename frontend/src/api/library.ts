import { http } from '@/api'
import type { BucketFile, DataSource } from '@/types'
import type { LibraryWrite } from '@/types/library'

export const libraryApi = {
  test: (id: string) => http.post<{ ok: boolean; message: string }>(`/data-sources/${encodeURIComponent(id)}/test`),
  create: (payload: LibraryWrite) => http.post<DataSource>('/data-sources', payload),
  update: (id: string, payload: LibraryWrite) => http.put<DataSource>(`/data-sources/${encodeURIComponent(id)}`, payload),
  uploadSqlite: (id: string, file: File) => {
    const body = new FormData()
    body.append('file', file)
    return http.post<BucketFile>(`/data-sources/${encodeURIComponent(id)}/sqlite-file`, body)
  },
}
