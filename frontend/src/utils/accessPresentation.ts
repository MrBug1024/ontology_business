import type { AccountStatus, Invitation, SystemRole, WorkspaceRole } from '../types/access'

export const workspaceRoleLabels: Record<WorkspaceRole, string> = { owner: '所有者', admin: '管理员', operator: '操作员', viewer: '查看者' }
export const systemRoleLabels: Record<SystemRole, string> = { superadmin: '超级管理员', user: '普通账户' }
export const accountStatusLabels: Record<AccountStatus, string> = { active: '正常', pending: '待验证邮箱', disabled: '已禁用' }
export const invitationStatusLabels: Record<Invitation['status'], string> = { pending: '等待回应', accepted: '已同意', declined: '已拒绝', revoked: '已撤销', expired: '已过期' }
export const deliveryStatusLabels: Record<Invitation['delivery_status'], string> = { queued: '邮件等待投递', sending: '邮件投递中', sent: '邮件已发送', failed: '邮件发送失败', indeterminate: '投递结果待确认', cancelled: '邮件投递已取消' }
export function accessDate(value: string | null): string {
  if (!value) return '暂无记录'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}
export function accessPageNumber(value: unknown): number {
  const page = typeof value === 'string' && /^\d+$/.test(value) ? Number(value) : 1
  return Number.isSafeInteger(page) && page >= 1 && page <= 5001 ? page : 1
}
