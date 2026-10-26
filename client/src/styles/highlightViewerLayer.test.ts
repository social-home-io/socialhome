/**
 * The full-screen highlight viewer opens dialogs (publish a public link,
 * report) and shows toasts. Those sit on the shared layer tokens
 * (``--sh-z-modal`` 200, ``--sh-z-toast`` 300); the viewer used a literal
 * ``z-index: 1000`` and covered them, so "Publish public link" — and the
 * GFS_TOO_OLD message inside it — was never visible. jsdom does no layout,
 * so this pins the rule: the viewer stays below the modal layer.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const strip = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, '')
const app = strip(readFileSync(join(__dirname, 'app.css'), 'utf8'))
const tokens = strip(readFileSync(join(__dirname, 'tokens.css'), 'utf8'))

function body(css: string, selector: string): string {
  for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (m[1].split(',').map(s => s.trim()).includes(selector)) return m[2]
  }
  throw new Error(`no rule for ${selector}`)
}

const token = (name: string) =>
  Number(new RegExp(`${name}\\s*:\\s*(\\d+)`).exec(tokens)?.[1])

/** Resolve ``var(--x)``, ``calc(var(--x) ± n)`` or a number. */
function resolve(value: string): number {
  const v = value.trim()
  const calc = /^calc\(\s*var\((--[\w-]+)\)\s*([+-])\s*(\d+)\s*\)$/.exec(v)
  if (calc) return token(calc[1]) + (calc[2] === '+' ? 1 : -1) * Number(calc[3])
  const ref = /^var\((--[\w-]+)\)$/.exec(v)
  if (ref) return token(ref[1])
  return Number(v)
}

describe('highlight viewer layering', () => {
  const z = resolve(/z-index\s*:\s*([^;]+);/.exec(body(app, '.sh-hlv'))![1])

  it('sits below dialogs and toasts it opens', () => {
    expect(z).toBeLessThan(token('--sh-z-modal'))
    expect(z).toBeLessThan(token('--sh-z-toast'))
  })

  it('still covers the page chrome (dropdowns, banners)', () => {
    expect(z).toBeGreaterThan(token('--sh-z-banner'))
    expect(z).toBeGreaterThan(token('--sh-z-dropdown'))
  })

  it('the modal overlay is on the modal layer', () => {
    expect(resolve(/z-index\s*:\s*([^;]+);/.exec(body(app, '.sh-modal-overlay'))![1]))
      .toBe(token('--sh-z-modal'))
  })
})
