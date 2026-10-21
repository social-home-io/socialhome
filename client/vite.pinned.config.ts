/* Vite build for the PINNED public-highlight viewer (§highlights_public).
 *
 * Builds ``gfs/public_highlight.tsx`` into ONE self-contained ES module —
 * no chunks, no CSS asset, nothing fetched at run time — that
 * ``scripts/build-pinned-viewer.mjs`` wraps into a static, content-addressed
 * page (``dist-pinned/v/<hash>/``) pinned by SRI. Served from an origin no
 * GFS runs, so a GFS can't rewrite the script that reads the link secret.
 *
 * Reproducible: the same lockfile gives the same bytes (no timestamps, no
 * hashed chunk names, a single output file). A change to the viewer code
 * changes the hash, and CI fails until ``socialhome/pinned_viewer.json``
 * is regenerated in the same PR (``pnpm build:pinned-viewer``).
 */
import { defineConfig } from 'vite'
import preact from '@preact/preset-vite'
import { resolve } from 'path'

export default defineConfig({
  plugins: [preact()],
  publicDir: false,
  logLevel: 'warn',
  build: {
    outDir: 'dist-pinned/.build',
    emptyOutDir: true,
    // No ``<link rel=modulepreload>`` polyfill: there is nothing to preload.
    modulePreload: false,
    copyPublicDir: false,
    reportCompressedSize: false,
    rollupOptions: {
      input: resolve(import.meta.dirname, 'gfs/public_highlight.tsx'),
      output: {
        format: 'es',
        entryFileNames: 'viewer.js',
        // Everything in the one file: no shared chunks, no dynamic imports.
        codeSplitting: false,
      },
      preserveEntrySignatures: false,
    },
  },
})
