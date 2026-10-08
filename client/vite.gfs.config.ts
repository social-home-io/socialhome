/* Vite build for the GFS-served Preact surfaces.
 *
 * Two entry points today:
 *
 * - ``highlight_public_viewer`` — the §highlights_public landing-page
 *   bootstrap mounted into ``<div id="root">`` on
 *   ``GET /highlight/{instance}/{highlight}/{token}``.
 * - ``admin`` — the GFS admin portal mounted on ``GET /admin``.
 *
 * Future GFS surfaces (e.g. public global-space pages) plug in as
 * additional rollup inputs so they share the build infra without
 * dragging the SPA bundle's chunk strategy along.
 *
 * Output lands directly in the Python package at
 * ``socialhome/global_server/static/`` so the GFS process ships
 * the bundles without a separate dist step.
 */
import { defineConfig } from 'vite'
import preact from '@preact/preset-vite'
import { resolve } from 'path'

export default defineConfig({
  plugins: [preact()],
  // Avoid copying the SPA's ``public/`` (favicon, PWA manifest,
  // service worker) into the GFS dir.
  publicDir: false,
  build: {
    outDir: '../socialhome/global_server/static',
    emptyOutDir: false,
    rollupOptions: {
      // Multi-entry build → emits one ``<name>.js`` + ``<name>.css``
      // per input. Both bundles render their own ``<div id="root">``
      // root; entry filenames are referenced verbatim by the SSR
      // pages that ship them.
      input: {
        highlight_public_viewer: resolve(__dirname, 'gfs/public_highlight.tsx'),
        moment_public_viewer: resolve(__dirname, 'gfs/public_moments.tsx'),
        admin:               resolve(__dirname, 'gfs/admin/main.tsx'),
      },
      output: {
        format: 'es',
        // Keep filenames stable so the SSR page tags stay literal —
        // emit ``highlight_public_viewer.js`` + ``admin.js`` in
        // ``socialhome/global_server/static/``. ES modules need
        // ``type="module"`` on the page ``<script>`` tag, which
        // every browser the public viewer + admin portal target
        // supports. Vite normally factors shared modules out into
        // their own chunks; we want bundle parallelism here, so let
        // it produce ``hooks.module-<hash>.js`` for the dedup'd
        // ``preact/hooks`` import.
        entryFileNames: '[name].js',
        chunkFileNames: '[name]-[hash].js',
        // Code shared by the two public viewers (``gfs/viewer_*.ts{,x}``)
        // would otherwise land in its own ``viewer_<name>-<hash>.js``
        // chunk — a name neither ``.gitignore`` nor the wheel's
        // ``[tool.hatch.build].artifacts`` covers, so the wheel would ship
        // the viewers without it (import 404 → blank page). Name it into
        // the ``*.module-*.js`` family both globs already cover.
        // The Preact runtime gets its own explicit chunk: otherwise the
        // bundler folds it into ``viewer.module`` (a viewer module imports
        // it) and the admin portal ends up importing the viewer chunk.
        manualChunks(id: string) {
          if (/[\\/]node_modules[\\/]preact[\\/]/.test(id)) return 'preact.module'
          if (/\/gfs\/viewer_[^/]+$/.test(id)) return 'viewer.module'
          return undefined
        },
        assetFileNames: '[name][extname]',
      },
      preserveEntrySignatures: false,
    },
  },
  resolve: {
    alias: { '@': resolve(__dirname, 'src') },
  },
})
