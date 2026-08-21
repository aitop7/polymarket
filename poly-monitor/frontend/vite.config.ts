import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const sharedDir = path.resolve(__dirname, '../shared')

const apiProxy = {
  '/api': {
    target: 'http://127.0.0.1:8000',
    changeOrigin: true,
    ws: true,
  },
}

export default defineConfig({
  plugins: [react()],
  server: {
    // Listen on all interfaces so other PCs on the LAN can open this UI.
    // /api is still proxied to 127.0.0.1:8000 on this machine.
    host: '0.0.0.0',
    port: 5173,
    proxy: apiProxy,
    fs: {
      allow: [path.resolve(__dirname), sharedDir],
    },
  },
  preview: {
    port: 4173,
    proxy: apiProxy,
  },
})
