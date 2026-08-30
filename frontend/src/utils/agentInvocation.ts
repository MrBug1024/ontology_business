import type {
  AgentCapabilityDataPort,
  AgentCapabilityTarget,
  AgentChatRequest,
  AgentManagedBindingKind,
  AgentManagedInput,
} from '@/types'

export const AGENT_INVOCATION_FILE_ACCEPT = [
  '.csv', '.xlsx', '.xls', '.docx', '.doc', '.pdf', '.txt', '.md', '.json', '.pptx',
].join(',')

const SUPPORTED_EXTENSIONS = new Set(AGENT_INVOCATION_FILE_ACCEPT.split(','))
const PORT_KEY_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]*$/
const SIGNATURE_PATTERN = /^[a-f0-9]{64}$/i
const MANAGED_BINDING_KINDS = new Set<AgentManagedBindingKind>([
  'dataset_version', 'dataset_head', 'asset_version', 'connector_binding',
])

export type InvocationAttachmentStatus = 'uploading' | 'ready' | 'error'

export interface InvocationAttachmentDraft {
  uid: string
  file: File
  filename: string
  size: number
  progress: number
  status: InvocationAttachmentStatus
  portKey: string
  assetVersionId?: string
  expectedSignature?: string
  error?: string
}

export interface InvocationManagedInputDraft {
  portKey: string
  bindingKind: AgentManagedBindingKind | ''
  referenceId: string
  expectedSignature?: string
}

export interface AgentInvocationDraft {
  message: string
  structuredJson: string
  attachments: InvocationAttachmentDraft[]
  managedInputs?: InvocationManagedInputDraft[]
  portContracts?: AgentCapabilityDataPort[]
  capability?: AgentCapabilityTarget
  idempotencyKey?: string
}

export interface AgentInvocationValidation {
  payload?: AgentChatRequest
  messageError?: string
  structuredError?: string
  idempotencyError?: string
  attachmentErrors: Record<string, string>
  managedInputErrors: Record<string, string>
}

function portKey(port: AgentCapabilityDataPort) {
  return String(port.port_key || port.key || '').trim()
}

export function managedBindingKindsForPort(
  port: AgentCapabilityDataPort,
): AgentManagedBindingKind[] {
  const configured = Array.isArray(port.binding_kinds)
    ? port.binding_kinds.filter((kind): kind is AgentManagedBindingKind => (
      MANAGED_BINDING_KINDS.has(kind)
    ))
    : []
  if (configured.length) return [...new Set(configured)]
  switch (String(port.media_kind || 'structured').toLowerCase()) {
    case 'dataset': return ['dataset_head', 'dataset_version']
    case 'document':
    case 'artifact': return ['asset_version']
    case 'connector': return ['connector_binding']
    case 'structured': return [
      'dataset_head', 'dataset_version', 'asset_version', 'connector_binding',
    ]
    default: return []
  }
}

export function allowsManagedInputSelection(port: AgentCapabilityDataPort) {
  if (port.direction === 'output') return false
  if (typeof port.allow_override === 'boolean') return port.allow_override
  return String(port.binding_policy || 'none').toLowerCase() === 'per_invocation'
}

export function isAttachmentInputPort(port: AgentCapabilityDataPort) {
  return allowsManagedInputSelection(port)
    && ['document', 'artifact'].includes(String(port.media_kind || '').toLowerCase())
    && managedBindingKindsForPort(port).includes('asset_version')
}

export function isSupportedInvocationFile(filename: string) {
  const normalized = String(filename || '').trim().toLowerCase()
  const dot = normalized.lastIndexOf('.')
  return dot >= 0 && SUPPORTED_EXTENSIONS.has(normalized.slice(dot))
}

export function parseStructuredInputs(source: string): {
  value?: Record<string, unknown>
  error?: string
} {
  const raw = String(source || '').trim()
  if (!raw) return { value: {} }
  try {
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { error: '结构化参数必须是 JSON 对象' }
    }
    return { value: parsed as Record<string, unknown> }
  } catch {
    return { error: 'JSON 格式不正确，请检查引号、逗号和括号' }
  }
}

function managedReference(
  portKeyValue: string,
  bindingKind: AgentManagedBindingKind,
  referenceId: string,
  expectedSignature?: string,
): AgentManagedInput {
  const result: AgentManagedInput = { port_key: portKeyValue }
  if (bindingKind === 'dataset_version') result.dataset_version_id = referenceId
  else if (bindingKind === 'dataset_head') result.dataset_head_id = referenceId
  else if (bindingKind === 'asset_version') result.asset_version_id = referenceId
  else result.binding_key = referenceId
  if (expectedSignature) result.expected_signature = expectedSignature
  return result
}

export function validateAgentInvocationDraft(
  draft: AgentInvocationDraft,
): AgentInvocationValidation {
  const result: AgentInvocationValidation = {
    attachmentErrors: {},
    managedInputErrors: {},
  }
  const structured = parseStructuredInputs(draft.structuredJson)
  if (structured.error) result.structuredError = structured.error

  const contracts = (draft.portContracts || []).filter((port) => Boolean(portKey(port)))
  const contractsByKey = new Map(
    contracts.map((port) => [portKey(port).toLowerCase(), port]),
  )
  const managedInputs: AgentManagedInput[] = []
  const usedPorts = new Set<string>()

  for (const selection of draft.managedInputs || []) {
    const selectedPortKey = String(selection.portKey || '').trim()
    const folded = selectedPortKey.toLowerCase()
    const contract = contractsByKey.get(folded)
    const bindingKind = selection.bindingKind
    const referenceId = String(selection.referenceId || '').trim()
    const required = Boolean(
      contract?.required
      && String(contract.binding_policy || '').toLowerCase() === 'per_invocation',
    )
    if (!bindingKind && !referenceId && !required) continue
    if (!selectedPortKey || selectedPortKey.length > 128 || !PORT_KEY_PATTERN.test(selectedPortKey)) {
      result.managedInputErrors[selectedPortKey || `managed-${managedInputs.length + 1}`] = '受管输入端口格式无效'
      continue
    }
    if (!contract || !allowsManagedInputSelection(contract)) {
      result.managedInputErrors[selectedPortKey] = '该端口不允许本次调用选择数据'
      continue
    }
    if (!bindingKind || !managedBindingKindsForPort(contract).includes(bindingKind)) {
      result.managedInputErrors[selectedPortKey] = '请选择该端口支持的数据引用类型'
      continue
    }
    if (!referenceId || referenceId.length > 240) {
      result.managedInputErrors[selectedPortKey] = '请选择有效的受治理数据版本或绑定'
      continue
    }
    if (selection.expectedSignature && !SIGNATURE_PATTERN.test(selection.expectedSignature)) {
      result.managedInputErrors[selectedPortKey] = '所选资源签名无效，请刷新目录后重试'
      continue
    }
    if (usedPorts.has(folded)) {
      result.managedInputErrors[selectedPortKey] = '同一输入端口不能重复提交'
      continue
    }
    usedPorts.add(folded)
    managedInputs.push(managedReference(
      selectedPortKey,
      bindingKind,
      referenceId,
      selection.expectedSignature,
    ))
  }

  for (const attachment of draft.attachments) {
    if (attachment.status === 'uploading') {
      result.attachmentErrors[attachment.uid] = '文件仍在上传'
      continue
    }
    if (attachment.status === 'error' || !attachment.assetVersionId) {
      result.attachmentErrors[attachment.uid] = attachment.error || '文件尚未成功上传'
      continue
    }
    const selectedPortKey = String(attachment.portKey || '').trim()
    const folded = selectedPortKey.toLowerCase()
    const contract = contractsByKey.get(folded)
    if (!selectedPortKey) {
      result.attachmentErrors[attachment.uid] = '请选择文档或制品输入端口'
      continue
    }
    if (selectedPortKey.length > 128 || !PORT_KEY_PATTERN.test(selectedPortKey)) {
      result.attachmentErrors[attachment.uid] = '端口只能包含字母、数字、点、下划线、冒号或短横线'
      continue
    }
    if (!contract || !isAttachmentInputPort(contract)) {
      result.attachmentErrors[attachment.uid] = '附件只能提交到允许覆盖的文档或制品端口'
      continue
    }
    if (attachment.expectedSignature && !SIGNATURE_PATTERN.test(attachment.expectedSignature)) {
      result.attachmentErrors[attachment.uid] = '上传资源签名无效，请重新上传'
      continue
    }
    if (usedPorts.has(folded)) {
      result.attachmentErrors[attachment.uid] = '同一输入端口不能重复提交'
      continue
    }
    usedPorts.add(folded)
    managedInputs.push(managedReference(
      selectedPortKey,
      'asset_version',
      attachment.assetVersionId,
      attachment.expectedSignature,
    ))
  }

  for (const contract of contracts) {
    const key = portKey(contract)
    if (
      contract.required
      && String(contract.binding_policy || '').toLowerCase() === 'per_invocation'
      && allowsManagedInputSelection(contract)
      && !usedPorts.has(key.toLowerCase())
      && !result.managedInputErrors[key]
    ) {
      result.managedInputErrors[key] = '该端口是本次调用的必填受管输入'
    }
  }

  const idempotencyKey = String(draft.idempotencyKey || '').trim()
  if (idempotencyKey.length > 180) result.idempotencyError = '幂等键不能超过 180 个字符'

  const message = String(draft.message || '').trim()
  const structuredValue = structured.value || {}
  if (
    !message
    && Object.keys(structuredValue).length === 0
    && managedInputs.length === 0
    && !draft.capability
  ) {
    result.messageError = '请输入需求、结构化参数、受管数据或选择要验证的能力'
  }

  if (
    result.messageError
    || result.structuredError
    || result.idempotencyError
    || Object.keys(result.attachmentErrors).length
    || Object.keys(result.managedInputErrors).length
  ) return result

  result.payload = {
    message,
    inputs: structuredValue,
    managed_inputs: managedInputs,
    ...(draft.capability ? { capability: draft.capability } : {}),
    ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {}),
  }
  return result
}
