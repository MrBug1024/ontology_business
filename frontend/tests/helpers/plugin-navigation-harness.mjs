import { readFileSync } from 'node:fs'
import ts from 'typescript'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createRenderer, h, nextTick } from 'vue'
import { ElMenu, ElMenuItem } from 'element-plus'
import 'vue-router'

const encode = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const vueUrl = new URL('../../node_modules/vue/dist/vue.runtime.esm-bundler.js', import.meta.url).href
const routerUrl = new URL('../../node_modules/vue-router/dist/vue-router.mjs', import.meta.url).href
const transpile = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
const pureModule = path => encode(transpile(readFileSync(new URL(path, import.meta.url), 'utf8')))
const authModule = encode('export const useAuthStore = () => ({ user: { display_name: "Synthetic user", system_role: "user" }, initialized: true, initialize: async () => {} })')
const emptyComponent = encode('export default { render: () => null }')
let sequence = 0

function node(type, text = '') {
  return { type, text, props: {}, children: [], parent: null }
}

const renderer = createRenderer({
  createElement: type => node(type),
  createText: text => node('#text', text),
  createComment: text => node('#comment', text),
  insert(child, parent, anchor) {
    if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1)
    child.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    if (index < 0) parent.children.push(child)
    else parent.children.splice(index, 0, child)
  },
  remove(child) {
    if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1)
    child.parent = null
  },
  setText: (target, text) => { target.text = text },
  setElementText: (target, text) => { target.text = text; target.children = [] },
  patchProp: (target, key, previous, value) => { target.props[key] = value },
  parentNode: target => target.parent,
  nextSibling: target => target.parent?.children[target.parent.children.indexOf(target) + 1] || null,
})

function descendantText(target) {
  return target.text + target.children.map(descendantText).join('')
}

function find(target, predicate) {
  if (predicate(target)) return target
  for (const child of target.children) {
    const result = find(child, predicate)
    if (result) return result
  }
  return null
}

export async function mountPlatformNavigation(initialPath = '/scenarios') {
  // Compile the production SFC and router. Only unrelated page bodies and
  // browser-only decoration are replaced; Menu/MenuItem execute real clicks.
  const source = readFileSync(new URL('../../src/App.vue', import.meta.url), 'utf8')
  const script = compileScript(parse(source).descriptor, { id: 'navigation-test', inlineTemplate: true }).content
  const appSource = transpile(script)
    .replaceAll('from "vue"', `from '${vueUrl}'`)
    .replaceAll("from 'vue'", `from '${vueUrl}'`)
    .replaceAll("from 'vue-router'", `from '${routerUrl}'`)
    .replaceAll("from '@/stores/auth'", `from '${authModule}'`)
    .replace(/from '@\/components\/[^']+'/g, `from '${emptyComponent}'`)
    .replace("from '@/utils/platformSettings'", `from '${pureModule('../../src/utils/platformSettings.ts')}'`)
    .replace("from '@/utils/scenarioStages'", `from '${pureModule('../../src/utils/scenarioStages.ts')}'`)
  let routeSource = transpile(readFileSync(new URL('../../src/router/index.ts', import.meta.url), 'utf8'))
    .replace("createRouter, createWebHistory", 'createRouter, createMemoryHistory')
    .replace('createWebHistory()', 'createMemoryHistory()')
    .replace("from 'vue-router'", `from '${routerUrl}'`)
    .replace("from '@/stores/auth'", `from '${authModule}'`)
    .replace(/import\('@\/views\/([^']+)\.vue'\)/g, (match, name) => `Promise.resolve({ default: { name: '${name}', render: () => h('section', { 'data-page': '${name}' }) } })`)
  routeSource = `import { h } from '${vueUrl}';\n${routeSource}\n// Isolate each test router: ${++sequence}`
  const globals = new Map(['window', 'document', 'localStorage', 'requestAnimationFrame'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  globalThis.window = { addEventListener() {}, removeEventListener() {} }
  globalThis.document = { documentElement: { dataset: {} }, getElementById: () => null }
  globalThis.localStorage = { getItem: () => null, setItem() {} }
  globalThis.requestAnimationFrame = callback => { callback(); return 0 }
  let app
  const restore = () => {
    app?.unmount()
    for (const [key, descriptor] of globals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
  }
  try {
    const { default: router } = await import(encode(routeSource))
    const { default: App } = await import(encode(appSource))
    await router.push(initialPath)
    await router.isReady()
    const root = node('root')
    app = renderer.createApp(App)
    const wrapper = tag => ({ render() { return h(tag, this.$attrs, this.$slots.default?.()) } })
    for (const name of ['ElContainer', 'ElAside', 'ElMain', 'ElIcon', 'ElDropdown', 'ElDropdownMenu', 'ElDropdownItem']) app.component(name, wrapper('div'))
    app.component('ElButton', wrapper('button'))
    app.component('ElMenu', ElMenu)
    app.component('ElMenuItem', ElMenuItem)
    for (const match of appSource.matchAll(/resolveComponent\("([^"]+)"\)/g)) {
      if (match[1] === 'router-view') continue
      const componentName = match[1].replace(/(^|-)(\w)/g, (value, separator, letter) => letter.toUpperCase())
      if (!app._context.components[componentName]) app.component(match[1], wrapper('span'))
    }
    app.use(router)
    app.mount(root)
    await nextTick()
    return {
      router,
      find: predicate => find(root, predicate),
      async clickMenu(label) {
        const item = find(root, target => target.props.role === 'menuitem' && descendantText(target) === label)
        if (!item) throw new Error(`Menu item not rendered: ${label}`)
        let timer
        let removeHook
        try {
          await new Promise((resolve, reject) => {
            removeHook = router.afterEach((to, from, failure) => failure ? reject(failure) : resolve())
            timer = setTimeout(() => reject(new Error(`Menu did not navigate: ${label}`)), 2000)
            item.props.onClick()
          })
          await nextTick()
        } finally { clearTimeout(timer); removeHook?.() }
      },
      stop: restore,
    }
  } catch (error) {
    restore()
    throw error
  }
}
