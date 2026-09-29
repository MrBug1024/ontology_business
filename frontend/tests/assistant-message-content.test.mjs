import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createRenderer, h, nextTick, reactive } from 'vue'
import ts from 'typescript'
import { splitAssistantMessage } from '../src/utils/distillationConversation.ts'

const encode = source => 'data:text/javascript;base64,' + Buffer.from(source).toString('base64')
const vueUrl = import.meta.resolve('vue')
const markdownUrl = encode("import { h } from '" + vueUrl + "'; export default { props: ['content'], setup: props => () => h('markdown', props.content) }")
const { descriptor } = parse(readFileSync(new URL('../src/components/AssistantMessageContent.vue', import.meta.url), 'utf8'))
const compiled = compileScript(descriptor, { id: 'thinking-message-test', inlineTemplate: true })
const source = ts.transpileModule(compiled.content, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
  .replaceAll(/from ["']vue["']/g, "from '" + vueUrl + "'")
  .replace("from './SafeMarkdown.vue'", "from '" + markdownUrl + "'")
  .replace("from '@/utils/distillationConversation'", "from '" + new URL('../src/utils/distillationConversation.ts', import.meta.url).href + "'")
const { default: Message } = await import(encode(source))

function mount(content, streaming = false) {
  const node = (type, text = '') => ({ type, text, props: {}, children: [], parent: null })
  const remove = child => { const list = child.parent?.children; if (list?.includes(child)) list.splice(list.indexOf(child), 1) }
  const renderer = createRenderer({
    createElement: type => node(type), createText: text => node('text', text), createComment: () => node('comment'),
    setText: (target, text) => { target.text = text }, setElementText: (target, text) => { target.text = text; target.children = [] },
    patchProp: (target, key, _old, value) => { target.props[key] = value },
    insert(child, parent, anchor) { remove(child); const index = parent.children.indexOf(anchor); parent.children.splice(index < 0 ? parent.children.length : index, 0, child); child.parent = parent },
    remove, parentNode: target => target.parent, nextSibling: target => target.parent?.children[target.parent.children.indexOf(target) + 1] || null,
  })
  const props = reactive({ content, streaming }), root = node('root')
  const app = renderer.createApp({ setup: () => () => h(Message, props) }); app.mount(root)
  const all = () => { const nodes = []; const visit = item => { nodes.push(item); item.children.forEach(visit) }; visit(root); return nodes }
  return { props, all, details: () => all().filter(item => item.type === 'details'), markdown: () => all().filter(item => item.type === 'markdown'), stop: () => app.unmount() }
}

test('every token prefix hides incomplete tags without losing thinking or answer', () => {
  for (const close of ['</think>', '<' + String.fromCharCode(92) + 'think>']) {
    const input = '<THINK>checking' + close + '**Answer**'
    for (let end = 1; end <= input.length; end++) {
      const parts = splitAssistantMessage(input.slice(0, end), true)
      assert.ok(parts.every(part => !/[<>]/.test(part.content)), 'tag flash at ' + end)
      assert.ok(parts.filter(part => part.kind === 'answer').every(part => !part.content.includes('checking')))
    }
    assert.deepEqual(splitAssistantMessage(input), [
      { kind: 'thinking', content: 'checking', streaming: false },
      { kind: 'answer', content: '**Answer**', streaming: false },
    ])
  }
})

test('ordinary answers, multiple blocks and incomplete thinking retain content', () => {
  assert.deepEqual(splitAssistantMessage('2 < 10\n**Answer**'), [{ kind: 'answer', content: '2 < 10\n**Answer**', streaming: false }])
  assert.equal(splitAssistantMessage('Literal <')[0].content, 'Literal <')
  const parts = splitAssistantMessage('<think>one</think>first<think>two</think>second')
  assert.deepEqual(parts.map(part => [part.kind, part.content]), [['thinking', 'one'], ['answer', 'first'], ['thinking', 'two'], ['answer', 'second']])
  assert.deepEqual(splitAssistantMessage('<think>unfinished</thi'), [{ kind: 'thinking', content: 'unfinished', streaming: true }])
})

test('live thinking opens then collapses while the answer renders separately', async () => {
  const view = mount('<think>', true)
  assert.equal(view.details()[0].props.open, true)
  assert.ok(view.all().some(item => item.text === '生成中…'))
  view.props.content = '<think>checking'
  await nextTick()
  assert.equal(view.markdown()[0].text, 'checking')
  view.props.content = '<think>checking</think>**Answer**'
  await nextTick()
  assert.equal(view.details()[0].props.open, false)
  assert.deepEqual(view.markdown().map(item => item.text), ['checking', '**Answer**'])
  assert.equal(view.markdown()[0].parent.type, 'details')
  assert.equal(view.markdown()[1].parent.props.class, 'assistant-answer')
  view.props.streaming = false
  await nextTick()
  assert.ok(!view.all().some(item => item.text === '生成中…'))
  view.stop()
})

test('history is collapsed and cancellation does not turn partial thinking into an answer', async () => {
  const view = mount('<think>checking</think>Answer')
  assert.equal(view.details()[0].props.open, false)
  view.props.content = '<think>partial'
  view.props.streaming = true
  await nextTick()
  view.props.streaming = false
  await nextTick()
  assert.equal(view.details()[0].props.open, false)
  assert.ok(view.all().some(item => item.text === '未完成'))
  assert.ok(!view.all().some(item => item.props.class === 'assistant-answer'))
  view.props.content = 'Plain answer'
  await nextTick()
  assert.equal(view.details().length, 0)
  assert.equal(view.markdown()[0].text, 'Plain answer')
  view.stop()
})
