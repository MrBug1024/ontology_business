import type {
  CatalogBindingRole,
  CatalogCanonicalBindingRole,
  CatalogEnvironment,
  ScenarioDatasetBindingCreate,
} from '@/types'

export interface CatalogBindingRoleOption {
  value: CatalogCanonicalBindingRole
  label: string
  description: string
}

export const CATALOG_BINDING_ROLE_OPTIONS: CatalogBindingRoleOption[] = [
  { value: 'modeling_evidence', label: '建模依据', description: '帮助理解数据结构、关系与业务语义' },
  { value: 'test_fixture', label: '验证样例', description: '用于能力验证与回归测试，不代表正式业务输入' },
  { value: 'invocation_input', label: '调用输入（按次）', description: '声明客户端调用时需要提供的数据契约' },
  { value: 'reference', label: '参考资料', description: '调用期间可检索或查阅的辅助信息' },
  { value: 'rules', label: '规则资料', description: '用于判断、约束或解释的规则数据' },
  { value: 'output', label: '输出目标', description: '声明结果写入或交付的数据契约' },
]

export function catalogBindingRoleMeta(role: CatalogBindingRole | string) {
  const option = CATALOG_BINDING_ROLE_OPTIONS.find((item) => item.value === role)
  if (option) return { ...option, compatibility: false }
  if (role === 'input') {
    return {
      value: 'input',
      label: '待确认（兼容）',
      description: '旧版 input 标记仅作兼容展示，需重新确认用途后才能作为调用输入。',
      compatibility: true,
    }
  }
  return {
    value: String(role || 'unknown'),
    label: '未分类',
    description: '目录尚未识别该用途，请由业务专家确认。',
    compatibility: true,
  }
}

export interface CatalogBindingDraft {
  scenario_id: string
  dataset_id: string
  binding_key: string
  environment: CatalogEnvironment | ''
  role: CatalogCanonicalBindingRole | ''
  binding_mode: 'head' | 'pinned' | ''
  target_id: string
  is_required: boolean
}

const ENVIRONMENTS = new Set<CatalogEnvironment>(['dev', 'staging', 'prod'])
const CANONICAL_ROLES = new Set(CATALOG_BINDING_ROLE_OPTIONS.map((item) => item.value))

export function buildScenarioDatasetBindingRequest(draft: CatalogBindingDraft): {
  scenarioId: string
  payload: ScenarioDatasetBindingCreate
} {
  const scenarioId = draft.scenario_id.trim()
  const datasetId = draft.dataset_id.trim()
  const bindingKey = draft.binding_key.trim()
  const targetId = draft.target_id.trim()
  if (!scenarioId) throw new Error('请选择业务场景')
  if (!datasetId) throw new Error('请选择 LogicalDataset')
  if (!draft.environment || !ENVIRONMENTS.has(draft.environment)) throw new Error('请选择运行环境')
  if (!draft.role || !CANONICAL_ROLES.has(draft.role)) throw new Error('请选择资源用途')
  if (!draft.binding_mode) throw new Error('请选择跟随 Head 或固定 Version')
  if (!bindingKey) throw new Error('请填写绑定标识')
  if (!targetId) throw new Error(draft.binding_mode === 'head' ? '请选择 Dataset Head' : '请选择固定版本')

  return {
    scenarioId,
    payload: {
      dataset_id: datasetId,
      binding_key: bindingKey,
      environment: draft.environment as CatalogEnvironment,
      role: draft.role,
      binding_mode: draft.binding_mode,
      dataset_head_id: draft.binding_mode === 'head' ? targetId : null,
      dataset_version_id: draft.binding_mode === 'pinned' ? targetId : null,
      is_required: draft.is_required,
      status: 'active',
      config: {},
    },
  }
}
