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
import { THEME_INKS, fillHoverCss } from '@/utils/primaryFill'
import { DARK_INK, LIGHT_INK } from '@/features/stickies/ink'

// jsdom gives import.meta.url an http: scheme; vitest still injects __dirname.
const STYLES = __dirname
const SRC = join(STYLES, '..')

/** Props the space-theme hook applies only when the space overrides
 *  them — never a valid "defined" source on their own. */
const CONDITIONAL_SOURCE = join(SRC, 'hooks', 'useSpaceTheme.ts')

/**
 * Known undefined tokens, as a ratchet: a NEW undefined token, or MORE
 * uses of a listed one, fails the test. Empty since every legacy alias
 * (--sh-muted, --sh-surface-*, --sh-fg, --sh-error, …) was mapped onto
 * a real token — keep it that way rather than adding entries.
 */
const KNOWN_UNDEFINED: Record<string, number> = {}

/**
 * Intentional per-scope hooks: props that exist only while a space theme
 * (``useSpaceTheme`` + ``utils/primaryFill.ts``) overrides them, so every
 * use carries a fallback — ``var(--sh-accent, var(--sh-primary))``. Any
 * OTHER ``var(--sh-x, fallback)`` whose ``--sh-x`` is never defined is a
 * dead alias: the fallback always wins, and a hex fallback (``#f7f8fa``)
 * silently ignores the dark theme.
 */
const SCOPED_HOOKS = new Set([
  '--sh-accent',
  '--sh-bg-space-tint',
  '--sh-post-layout-gap',
  '--sh-on-primary-fill-light',
  '--sh-on-primary-fill-dark',
  '--sh-primary-fill-hover-light',
  '--sh-primary-fill-hover-dark',
])

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

  it('text on a filled primary uses --sh-on-primary-fill', () => {
    // A rule that paints a primary fill (solid or gradient) and sets a
    // text colour must take the adaptive ink: #fff / --sh-bg there is
    // 3.18:1 in dark mode, or < 3:1 on a mid-tone custom space primary.
    const offenders: string[] = []
    const css = sheets['app.css']
    for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      const [, sel, body] = m
      const bg = /(?:^|[\s;])background(?:-color)?\s*:\s*([^;]+)/.exec(body)?.[1] ?? ''
      const color = /(?:^|[\s;])color\s*:\s*([^;]+)/.exec(body)?.[1]?.trim()
      const fill = /var\(--sh-primary(?:-fill)?(?:-hover)?\)/.test(bg) && !/color-mix/.test(bg.split('gradient')[0])
      if (fill && color && !color.includes('--sh-on-primary-fill')) {
        offenders.push(`${sel.trim().split('\n').pop()} → ${color}`)
      }
    }
    expect(offenders).toEqual([])
  })

  it('the on-fill ink and the filled hover default per theme, overridable per space', () => {
    const tokens = sheets['tokens.css']
    const values = (prop: string) => [...tokens.matchAll(new RegExp(`${prop}\\s*:\\s*([^;]+);`, 'g'))].map(m => m[1].trim())
    expect(values('--sh-on-primary-fill')).toEqual([
      'var(--sh-on-primary-fill-light, var(--sh-bg))',
      'var(--sh-on-primary-fill-dark, var(--sh-bg))',
    ])
    // Same formulas utils/primaryFill.ts uses for a custom primary.
    expect(values('--sh-primary-fill-hover')).toEqual([
      `var(--sh-primary-fill-hover-light, ${fillHoverCss('#000')})`,
      `var(--sh-primary-fill-hover-dark, ${fillHoverCss('#fff')})`,
    ])
  })

  it('a var(--sh-x, fallback) names a real token or an intentional per-scope hook', () => {
    const known = new Set([...definedInCss(), ...setAtRuntime(), ...SCOPED_HOOKS])
    const dead: string[] = []
    for (const [name, css] of Object.entries(sheets)) {
      css.split('\n').forEach((line, i) => {
        for (const m of line.matchAll(/var\(\s*(--sh-[\w-]+)\s*,/g)) {
          if (!known.has(m[1])) dead.push(`${m[1]} @ ${name}:${i + 1}`)
        }
      })
    }
    expect(dead).toEqual([])
  })

  it('every per-scope hook is still consumed somewhere', () => {
    const all = Object.values(sheets).join('\n')
    for (const hook of SCOPED_HOOKS) expect(all).toContain(`var(${hook},`)
  })

  it('text inside my own (filled) DM bubble derives from --sh-on-primary-fill', () => {
    // Translucent white on the fill vanished under the dark-mode hearth
    // and on a light custom primary. A descendant that paints its OWN
    // opaque surface (the ink "Save" button) is exempt.
    const offenders: string[] = []
    for (const m of sheets['app.css'].matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      const sel = m[1].trim().split('\n').pop() ?? ''
      const mine = sel.split(',').some(part => /\.sh-message--mine(?![\w-])/.test(part.replace(/:not\([^)]*\)/g, '')))
      if (!mine) continue
      const body = m[2]
      const color = /(?:^|[\s;])color\s*:\s*([^;]+)/.exec(body)?.[1]?.trim()
      const bg = /(?:^|[\s;])background(?:-color)?\s*:\s*([^;]+)/.exec(body)?.[1] ?? ''
      const ownSurface = bg !== '' && !bg.includes('--sh-on-primary-fill') && !/--sh-primary-fill/.test(bg)
      if (color && !ownSurface && !color.includes('--sh-on-primary-fill')) offenders.push(`${sel} → ${color}`)
    }
    expect(offenders).toEqual([])
  })

  it('the JS ink mirrors (primaryFill, sticky ink) match tokens.css', () => {
    const tokens = sheets['tokens.css']
    const darkAt = tokens.indexOf('.sh-theme-dark {')
    const block = (dark: boolean) => {
      const body = dark ? tokens.slice(darkAt) : tokens.slice(0, darkAt)
      return (prop: string) => new RegExp(`${prop}\\s*:\\s*(#[0-9a-fA-F]{3,8})`).exec(body)?.[1]?.toUpperCase()
    }
    const light = block(false)
    const dark = block(true)
    expect(darkAt).toBeGreaterThan(0)
    expect(THEME_INKS.light).toEqual({ bg: light('--sh-bg'), text: light('--sh-text') })
    expect(THEME_INKS.dark).toEqual({ bg: dark('--sh-bg'), text: dark('--sh-text') })
    // ink.ts chooses with the light-theme sticky inks.
    expect(DARK_INK).toBe(light('--sh-sticky-ink'))
    expect(LIGHT_INK).toBe(light('--sh-sticky-ink-light'))
  })
})
