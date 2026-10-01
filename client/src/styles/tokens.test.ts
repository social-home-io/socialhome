/**
 * Design-token guard — every ``var(--sh-…)`` in the stylesheets must
 * resolve to something.
 *
 * A custom property that is never defined makes the whole declaration
 * "invalid at computed-value time": ``color`` falls back to inherited,
 * ``background`` / ``border-color`` to ``transparent`` / ``currentColor``,
 * ``border-radius`` to ``0`` — silently, with no console error. This
 * test parses ``app.css`` + ``tokens.css`` and fails on any
 * ``var(--sh-x)`` WITHOUT a fallback whose ``--sh-x`` is not:
 *
 *   - declared in ``tokens.css`` or ``app.css`` (``--sh-x: …``), or
 *   - set at runtime by the SPA (``style.setProperty('--sh-x', …)`` or a
 *     ``{ '--sh-x': … }`` inline-style object) — scanned from ``src/``.
 *
 * ``useSpaceTheme.ts`` is deliberately NOT a runtime source: it only
 * applies a space's overrides when the space has one (``--sh-accent``
 * is "this space's accent, if any"), so every use of a prop it sets
 * must either be a global token or carry a fallback —
 * ``var(--sh-accent, var(--sh-primary))``.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

// jsdom gives import.meta.url an http: scheme; vitest still injects __dirname.
const STYLES = __dirname
const SRC = join(STYLES, '..')

/** Props the space-theme hook applies only when the space overrides
 *  them — never a valid "defined" source on their own. */
const CONDITIONAL_SOURCE = join(SRC, 'hooks', 'useSpaceTheme.ts')

/**
 * Pre-existing undefined tokens (aliases that were never added to
 * tokens.css). Frozen as a ratchet: a NEW undefined token, or MORE
 * uses of one of these, fails the test. Fixing sites lowers the
 * count — lower the number here (and drop the entry at 0).
 */
const KNOWN_UNDEFINED: Record<string, number> = {
  '--sh-modal-backdrop': 1,
  '--sh-fg': 4,
  '--sh-fg-secondary': 2,
  '--sh-space-2': 1,
  '--sh-muted': 19,
  '--sh-surface-2': 9,
  '--sh-surface-1': 2,
  '--sh-surface-0': 2,
  '--sh-error': 5,
  '--sh-space-xxs': 7,
  '--sh-text-secondary': 4,
  '--sh-surface': 5,
  '--sh-bg-soft': 4,
  '--sh-surface-3': 2,
}

/** Blank out comments but keep newlines so line numbers survive. */
function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, m => m.replace(/[^\n]/g, ' '))
}

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name)) out.push(p)
  }
  return out
}

const sheets = {
  'tokens.css': stripComments(readFileSync(join(STYLES, 'tokens.css'), 'utf8')),
  'app.css': stripComments(readFileSync(join(STYLES, 'app.css'), 'utf8')),
}

function definedInCss(): Set<string> {
  const out = new Set<string>()
  for (const css of Object.values(sheets)) {
    for (const m of css.matchAll(/(--sh-[\w-]+)\s*:/g)) out.add(m[1])
  }
  return out
}

function setAtRuntime(): Set<string> {
  const out = new Set<string>()
  for (const file of walk(SRC)) {
    if (file === CONDITIONAL_SOURCE) continue
    const code = readFileSync(file, 'utf8')
    for (const m of code.matchAll(/setProperty\(\s*['"`](--sh-[\w-]+)['"`]/g)) out.add(m[1])
    for (const m of code.matchAll(/['"](--sh-[\w-]+)['"]\s*:/g)) out.add(m[1])
  }
  return out
}

/** token → ["app.css:123", …] for every fallback-less use that resolves to nothing. */
function undefinedUses(): Record<string, string[]> {
  const known = new Set([...definedInCss(), ...setAtRuntime()])
  const out: Record<string, string[]> = {}
  for (const [name, css] of Object.entries(sheets)) {
    css.split('\n').forEach((line, i) => {
      for (const m of line.matchAll(/var\(\s*(--sh-[\w-]+)\s*\)/g)) {
        if (!known.has(m[1])) (out[m[1]] ??= []).push(`${name}:${i + 1}`)
      }
    })
  }
  return out
}

describe('design tokens', () => {
  it('every fallback-less var(--sh-…) is defined, runtime-set, or known debt', () => {
    const offenders: string[] = []
    for (const [token, sites] of Object.entries(undefinedUses())) {
      const allowed = KNOWN_UNDEFINED[token] ?? 0
      if (sites.length > allowed) {
        offenders.push(`${token} (${sites.length} uses, ${allowed} allowed): ${sites.join(', ')}`)
      }
    }
    expect(offenders).toEqual([])
  })

  it('the known-debt list only names tokens that are still undefined', () => {
    const uses = undefinedUses()
    const stale = Object.entries(KNOWN_UNDEFINED)
      .filter(([token, n]) => (uses[token]?.length ?? 0) < n)
      .map(([token, n]) => `${token}: ${uses[token]?.length ?? 0} uses, list says ${n}`)
    expect(stale).toEqual([])
  })

  it('treats a space-theme override as optional, not as a definition', () => {
    // --sh-accent only exists while a space with an accent is open.
    expect(definedInCss().has('--sh-accent')).toBe(false)
    expect(setAtRuntime().has('--sh-accent')).toBe(false)
  })

  it('counts props the SPA sets inline as defined', () => {
    expect(setAtRuntime()).toContain('--sh-swipe')
  })

  it('derives the text-on-fill hearth from --sh-primary in both themes', () => {
    // Space themes / the household studio override --sh-primary at
    // runtime; the fill must follow it rather than pin a hex.
    const fills = [...sheets['tokens.css'].matchAll(/--sh-primary-fill\s*:\s*([^;]+);/g)]
      .map(m => m[1].trim())
    expect(fills).toHaveLength(2)
    for (const value of fills) expect(value).toContain('var(--sh-primary)')
  })
})
