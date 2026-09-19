import assert from 'node:assert/strict'
import test from 'node:test'
import { libraryForm, libraryPayload, validateSqliteFile } from '../src/utils/library.ts'

test('library editor keeps credentials out of forms returned from the server', () => {
  const form = libraryForm({ name: 'Research', type: 'mysql', config: { host: 'example.invalid', port: 3306, database: 'research', user: 'reader', password: 'never-copy' } })
  assert.equal(form.password, '')
  assert.equal(form.user, 'reader')
  assert.equal(libraryForm().type, 'postgres')
})

test('new database libraries start with their database port', () => {
  assert.equal(libraryForm().port, 5432)
  assert.equal(libraryForm({ name: 'MySQL', type: 'mysql', config: {} }).port, 3306)
})

test('editing preserves supported legacy string ports', () => {
  assert.equal(libraryForm({ name: 'Legacy PostgreSQL', type: 'postgres', config: { port: ' 5544 ' } }).port, 5544)
  assert.equal(libraryForm({ name: 'Legacy MySQL', type: 'mysql', config: { port: '3307' } }).port, 3307)
  assert.equal(libraryForm({ name: 'PostgreSQL', type: 'postgres', config: { port: 5444 } }).port, 5444)
})

test('file libraries submit no physical or credential configuration', () => {
  const form = { ...libraryForm(), name: 'SQLite research', type: 'sqlite3', host: 'old-host', password: 'old-password' }
  assert.deepEqual(libraryPayload(form), { name: 'SQLite research', type: 'sqlite3', scenario_id: null, config: {} })
})

test('remote library validation preserves the draft and rejects invalid ports', () => {
  const form = { ...libraryForm(), name: 'Research', host: 'example.invalid', database: 'research', user: 'reader', port: 0 }
  const before = structuredClone(form)
  assert.throws(() => libraryPayload(form), /端口/)
  assert.deepEqual(form, before)
})

test('SQLite upload validates extension and size before creating a library', () => {
  assert.equal(validateSqliteFile(new File([new Uint8Array(100)], 'snapshot.sqlite3')), '')
  assert.match(validateSqliteFile(new File(['small'], 'snapshot.db')), /大小/)
  assert.match(validateSqliteFile(new File([new Uint8Array(100)], 'snapshot.txt')), /请选择/)
})
