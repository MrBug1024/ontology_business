import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig(({ mode }) => {
  // Vite does not automatically expose .env values while evaluating this
  // config file. Loading them here makes VITE_API_PROXY_TARGET work for local
  // development. Production uses the built-in API base URL from the bundle.
  const env = loadEnv(mode, process.cwd(), '')
  const apiTarget = process.env.VITE_API_PROXY_TARGET
    || env.VITE_API_PROXY_TARGET
    || 'http://127.0.0.1:8001'

  return {
    plugins: [vue()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    preview: {
      allowedHosts: ['ontology.rhzy.ai'],
    },
    server: {
      port: 5173,
      strictPort: true,
      host: '0.0.0.0',
      proxy: {
        '/api': {
          target: apiTarget,
          changeOrigin: true,
        },
      },
    },
  }
})
