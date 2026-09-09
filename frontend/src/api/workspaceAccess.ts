import { http } from '@/api'
import type { AccessAudit, AccessPage, Account, AccountUpdate, Invitation, InviteInput, MemberPage, Workspace, WorkspaceRole } from '@/types/access'

const revision = (expected_revision: number) => ({ expected_revision })
export const workspaceAccess = {
  memberOptions: (search: string, userIds: string[], signal?: AbortSignal) => {
    const params = new URLSearchParams({ search, limit: '100' })
    for (const id of userIds) params.append('user_ids', id)
    return http.get<MemberPage>('/workspace/members', { params, signal })
  },
  workspaces: (signal?: AbortSignal) => http.get<Workspace[]>('/workspaces', { signal }),
  switchWorkspace: (tenant_id: string) => http.post<{ message: string }>('/workspaces/switch', { tenant_id }),
  members: (page: number, signal?: AbortSignal) => http.get<MemberPage>('/workspace/members', { params: { offset: (page - 1) * 20, limit: 20 }, signal }),
  setRole: (id: string, role: WorkspaceRole, version: number) => http.patch<{ message: string }>(`/workspace/members/${id}`, { ...revision(version), role }),
  remove: (id: string, version: number) => http.post<{ message: string }>(`/workspace/members/${id}/remove`, revision(version)),
  invite: (payload: InviteInput) => http.post<Invitation>('/workspace/invitations', payload),
  invitations: (inbox: boolean, page: number, signal?: AbortSignal) => http.get<AccessPage<Invitation>>(inbox ? '/invitations' : '/workspace/invitations', { params: { offset: (page - 1) * 20, limit: 20 }, signal }),
  respond: (id: string, action: 'accept' | 'decline', version: number) => http.post<{ message: string }>(`/invitations/${id}/${action}`, revision(version)),
  manageInvitation: (id: string, action: 'resend' | 'revoke', version: number) => http.post<{ message: string }>(`/workspace/invitations/${id}/${action}`, revision(version)),
  accounts: (page: number, search: string, status: string, signal?: AbortSignal) => http.get<AccessPage<Account>>('/system/accounts', { params: { offset: (page - 1) * 20, limit: 20, search, status }, signal }),
  updateAccount: (id: string, payload: AccountUpdate) => http.patch<{ message: string }>(`/system/accounts/${id}`, payload),
  audit: (id: string, page: number, signal?: AbortSignal) => http.get<AccessAudit[]>(`/system/accounts/${id}/audit`, { params: { offset: (page - 1) * 20, limit: 20 }, signal }),
}
