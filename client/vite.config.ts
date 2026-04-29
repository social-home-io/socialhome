import { defineConfig } from 'vite'
import preact from '@preact/preset-vite'
import { resolve } from 'path'

export default defineConfig({
  plugins: [preact()],
  base: './',
  build: {
    outDir:   '../socialhome/static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      // /api/ws is declared first so Vite's proxy router matches the
      // WebSocket route before the broader /api HTTP rule. Vite walks
      // proxy entries in declaration order.
      '/api/ws': { target: 'ws://localhost:8099', ws: true },
      // Strip the `Origin` header in dev — the backend's cors-deny
      // middleware (`socialhome.hardening.build_cors_deny_middleware`)
      // allows same-origin requests, but Vite (5173) → backend (8099)
      // is technically cross-origin (different port), so the only way
      // to make every /api/* call work without an env-var allowlist
      // is to drop the Origin header. Production deploys serve the
      // SPA from the backend itself, so this only affects `pnpm run dev`.
      '/api': {
        target: 'http://localhost:8099',
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => proxyReq.removeHeader('origin'))
        },
      },
    },
  },
  resolve: {
    alias: { '@': resolve(__dirname, 'src') },
  },
})
