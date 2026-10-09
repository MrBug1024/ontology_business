import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

const apiTarget = process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8001'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 3099,
    strictPort: true,
    host: '0.0.0.0',
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
      '/mcp': {
        target: apiTarget,
        changeOrigin: true,
        // Preserve the legacy settings page for ordinary browser navigation.
        // MCP clients advertise JSON/SSE and must reach the protocol server.
        bypass: (request) => request.headers.accept?.includes('text/html') ? '/index.html' : undefined,
      },
    },
  },
})
