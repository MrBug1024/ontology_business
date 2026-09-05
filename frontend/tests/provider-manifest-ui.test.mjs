import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const scenarioDetail = fs.readFileSync(path.join(root, 'src/views/ScenarioDetail.vue'), 'utf8')
const editorPath = path.join(root, 'src/components/ProviderConfigEditor.vue')
const providerUtilitiesPath = path.join(root, 'src/utils/providerManifests.ts')

async function loadProviderUtilities() {
  const source = fs.readFileSync(providerUtilitiesPath, 'utf8')
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText
  return import(`data:text/javascript;base64,${Buffer.from(output).toString('base64')}`)
}

test('generic scenario view delegates Provider identity and config rendering', () => {
  assert.ok(fs.existsSync(editorPath), 'missing generic ProviderConfigEditor')
  assert.match(scenarioDetail, /ProviderConfigEditor/)
  assert.match(scenarioDetail, /listFunctionProviderManifests/)
  assert.doesNotMatch(scenarioDetail, /builtin\.semantic-dataset-query/)
  assert.doesNotMatch(scenarioDetail, /semanticQueryOutputSchema/)
  assert.doesNotMatch(scenarioDetail, /provider_config\.semantic_mapping_ids/)
})

test('generic Provider editor renders only trusted manifest controls', () => {
  const editor = fs.readFileSync(editorPath, 'utf8')
  assert.match(editor, /config_schema/)
  assert.match(editor, /ui_schema/)
  assert.match(editor, /semantic_mapping_multiselect/)
  assert.doesNotMatch(editor, /builtin\.semantic-/)
})

test('leaving Provider mode restores only a captured manual contract and clears Provider config', async () => {
  const {
    captureFunctionContractSchemas,
    functionRuntimeConfigForSave,
    restoreFunctionContractSchemas,
  } = await loadProviderUtilities()
  const inputSchema = {
    type: 'object',
    properties: { amount: { type: 'number' } },
    required: ['amount'],
    additionalProperties: false,
  }
  const outputSchema = {
    type: 'object',
    properties: { accepted: { type: 'boolean' } },
    additionalProperties: false,
  }
  const snapshot = captureFunctionContractSchemas(inputSchema, outputSchema)

  inputSchema.properties.amount.type = 'string'
  const restored = restoreFunctionContractSchemas(snapshot)
  assert.equal(restored.input_schema.properties.amount.type, 'number')
  assert.deepEqual(restored.output_schema, outputSchema)
  assert.deepEqual(restoreFunctionContractSchemas(null), {
    input_schema: { type: 'object', properties: {}, additionalProperties: false },
    output_schema: { type: 'object', properties: {}, additionalProperties: false },
  })
  assert.deepEqual(functionRuntimeConfigForSave('contract', {
    provider_key: 'builtin.example',
    provider_version: '1.0.0',
    provider_config: { hidden: true },
  }), {})
})

test('scenario function form wires the Provider-to-contract transition through the isolated helpers', () => {
  assert.match(scenarioDetail, /previousKind === 'contract'[\s\S]*captureFunctionContractSchemas/)
  assert.match(scenarioDetail, /previousKind === 'provider'[\s\S]*restoreFunctionContractSchemas/)
  assert.match(scenarioDetail, /runtime_config: functionRuntimeConfigForSave\(/)
})
