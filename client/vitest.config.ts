import { defineConfig } from 'vitest/config'
import preact from '@preact/preset-vite'
import { resolve } from 'path'
import { cpus } from 'os'

// On CI, cap the fork pool to roughly the number of *physical* cores.
// GitHub runners report hyperthreaded vCPUs, so vitest's default (one fork
// per reported CPU) over-subscribes the real cores. Under that contention the
// heavy full-page integration tests (e.g. DmThreadPage's cold render, which
// waits through a multi-step async load → render → layout-effect chain) get
// CPU-starved and their wall-clock crosses the in-test `waitFor` budget — a
// flake that only manifests on CI, never locally. Giving each running test
// more sustained CPU keeps those chains under budget. Half the reported CPUs
// is a safe approximation of the physical-core count; local runs are left at
// the vitest default (fast multi-core boxes don't have the problem).
// Vitest 4 dropped ``poolOptions.forks.maxForks`` (silently ignored since);
// the top-level ``maxWorkers`` is the knob now.
const ciMaxWorkers = Math.max(1, Math.floor(cpus().length / 2))

export default defineConfig({
  plugins: [preact()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./vitest.setup.ts'],
    include: ['src/**/*.test.{ts,tsx}', 'gfs/**/*.test.{ts,tsx}'],
    ...(process.env.CI ? { maxWorkers: ciMaxWorkers } : {}),
  },
  resolve: {
    alias: { '@': resolve(import.meta.dirname, 'src') },
  },
})
