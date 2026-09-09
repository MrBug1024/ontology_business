export type WorkspaceRole = 'owner' | 'admin' | 'operator' | 'viewer'
export type SystemRole = 'user' | 'superadmin'
export type AccountStatus = 'pending' | 'active' | 'disabled'

export interface Workspace {
  tenant_id: string
  name: string
  role: WorkspaceRole
}

export interface Member {
  id: string
  user_id: string
  email: string
  display_name: string
  role: WorkspaceRole
  status: string
  account_status: AccountStatus
  email_verified: boolean
  revision: number
  created_at: string
  can_edit: boolean
}

export interface Invitation {
  id: string
  workspace_name: string
  email: string
  display_name: string
  role: WorkspaceRole
  status: 'pending' | 'accepted' | 'declined' | 'revoked' | 'expired'
  delivery_status: 'queued' | 'sending' | 'sent' | 'failed' | 'indeterminate' | 'cancelled'
  expires_at: string
  revision: number
}

export interface Account {
  id: string
  email: string
  display_name: string
  system_role: SystemRole
  status: AccountStatus
  email_verified: boolean
  revision: number
  created_at: string
  last_login_at: string | null
  workspace_count: number
}

export interface AccessPage<T> { items: T[]; total: number }
export interface MemberPage extends AccessPage<Member> { can_manage: boolean; role: WorkspaceRole }
export interface InviteInput { email: string; display_name: string; role: WorkspaceRole }
export interface AccountUpdate { expected_revision: number; system_role: SystemRole; status: AccountStatus; reason: string }
export interface AccessAudit {
  id: string
  actor_name: string
  action: string
  before_value: string
  after_value: string
  reason: string
  created_at: string
}
