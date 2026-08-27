import type { DataSource } from '@/types'

export function dataSourceLocationLabel(
  source: Pick<DataSource, 'type' | 'config'>,
): string {
  const config = source.config || {}
  if (source.type === 'file_bucket') {
    const backend = String(config.storage_backend || '').trim().toLowerCase()
    if (backend === 'minio') return 'MinIO'
    if (backend === 'local') return '本地'
    return '托管存储'
  }
  if (source.type === 'dataset') return 'MinIO 版本化数据集'
  return String(config.host || config.path || '未配置')
}
