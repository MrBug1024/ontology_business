import type { AcceptanceRole, PackageCapability, PackageEvidence, PackageAcceptanceCase } from '@/types/scenarioPackage'
export function packageCases(capabilities: PackageCapability[], evidence: PackageEvidence[], choices: Record<string, string>): PackageAcceptanceCase[] {
  const roles: AcceptanceRole[] = ['success', 'boundary', 'failure']
  const cases: PackageAcceptanceCase[] = []
  for (const item of capabilities) for (const role of roles) {
    const chosen = evidence.find(row => row.invocation_id === choices[`${item.kind}:${item.key}:${role}`]
      && row.kind === item.kind && row.key === item.key)
    if (!chosen?.eligible) throw new Error('请为每项能力选择三类已完成并核对的执行案例')
    if (role === 'success' && (chosen.status !== 'succeeded' || (chosen.kind === 'workflow' && chosen.workflow_status !== 'succeeded'))) throw new Error('成功案例必须实际执行成功')
    cases.push({ ...item, role, invocation_id: chosen.invocation_id, expected_status: chosen.status })
  }
  if (new Set(cases.map(item => item.invocation_id)).size !== cases.length) throw new Error('不同案例需使用不同的执行回执')
  return cases
}
