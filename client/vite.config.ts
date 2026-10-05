// vite 8 (Rolldown). The earlier "vite 8 prod build infinite-loops the SPA on
// boot" symptom was NOT a vite/Rolldown bug — it was the RouteProgress suspend
// loop (a signal mutated synchronously inside preact-iso's suspend handler);
// vite 8's chunk timing merely surfaced it at initial route load. Fixed at the
// source (RouteProgress defers the write), so vite 8 boots + navigates clean.
import { defineConfig } from 'vite'
import preact from '@preact/preset-vite'
import { resolve } from 'path'

export default defineConfig({
  plugins: [preact()],
  base: './',
  build: {
    outDir:   '../socialhome/static',
    emptyOutDir: true,
    // Never inline a script asset as a ``data:`` URL (the STT AudioWorklet
    // is one): the SPA's CSP is ``script-src 'self'`` with no ``data:``
    // (socialhome/csp.py). Other assets keep Vite's default limit.
    assetsInlineLimit: (file) => (/\.[cm]?js$/.test(file) ? false : undefined),
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
