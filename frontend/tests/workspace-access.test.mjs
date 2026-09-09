import assert from 'node:assert/strict'
import test from 'node:test'
import { effectScope } from 'vue'
import { useAccessResource } from '../src/composables/useAccessResource.ts'
import { accessPageNumber } from '../src/utils/accessPresentation.ts'
import { safeInternalReturnPath } from '../src/utils/navigation.ts'

test('late workspace response cannot replace the newest request', async () => {
  const pending = []
  const scope = effectScope()
  const resource = scope.run(() => useAccessResource(signal => new Promise(resolve => pending.push({ signal, resolve }))))
  const first = resource.reload()
  const second = resource.reload()
  assert.equal(pending[0].signal.aborted, true)
  pending[1].resolve('current workspace')
  await second
  pending[0].resolve('previous workspace')
  await first
  assert.equal(resource.data.value, 'current workspace')
  assert.equal(resource.loading.value, false)
  scope.stop()
})

test('unmount cancels requests and prevents a late response from committing', async () => {
  let complete
  let signal
  const scope = effectScope()
  const resource = scope.run(() => useAccessResource(active => { signal = active; return new Promise(resolve => { complete = resolve }) }))
  const request = resource.reload()
  scope.stop()
  assert.equal(signal.aborted, true)
  complete('stale account list')
  await request
  assert.equal(resource.data.value, undefined)
})

test('failed loads provide a retry and recover without losing prior data', async () => {
  const scope = effectScope()
  let attempt = 0
  const resource = scope.run(() => useAccessResource(async () => {
    if (++attempt === 2) throw new Error('permission changed')
    return attempt
  }))
  await resource.reload()
  await resource.reload()
  assert.equal(resource.data.value, 1)
  assert.equal(resource.error.value, 'permission changed')
  await resource.reload()
  assert.equal(resource.data.value, 3)
  assert.equal(resource.error.value, '')
  scope.stop()
})

test('untrusted pagination and login return paths remain bounded and internal', () => {
  for (const value of ['0', '-1', 'NaN', '9999999999999999', ['2'], undefined]) assert.equal(accessPageNumber(value), 1)
  assert.equal(accessPageNumber('3'), 3)
  assert.equal(safeInternalReturnPath('/invitations'), '/invitations')
  assert.equal(safeInternalReturnPath('//attacker.example/invitations'), '/scenarios')
})
