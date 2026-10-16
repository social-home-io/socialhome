/**
 * Contrast guards that jsdom can't measure (it does no layout or
 * compositing), pinned as CSS rules instead. Each one fixed an axe
 * ``color-contrast`` violation; tokens.test.ts holds the token-pairing
 * rules (text on a hearth tint, text on a hearth fill).
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

// jsdom gives import.meta.url an http: scheme; vitest still injects __dirname.
const css = readFileSync(join(__dirname, 'app.css'), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')

/** [selector, body] for every flat rule (media-wrapped ones included). */
function rules(): Array<[string, string]> {
  return [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(m => [m[1].trim(), m[2]])
}

describe('calendar filter strip pins', () => {
  // A hidden calendar's pin used to fade as a whole (opacity 0.55), which
  // took the name to ~2.6:1. The pin is a live toggle, not a disabled
  // control, so its label must stay ≥ 4.5:1: only the tape + avatar fade.
  const pinRules = rules().filter(([sel]) =>
    sel.split(',').some(s => /\.sh-cal-strip-pin(?:--[\w-]+|:[\w-]+)*$/.test(s.trim())))

  it('never fades the whole pin (that would fade its name)', () => {
    expect(pinRules.length).toBeGreaterThan(0)
    const faded = pinRules.filter(([, body]) => /(?:^|[\s;])opacity\s*:/.test(body))
    expect(faded.map(([sel]) => sel)).toEqual([])
  })

  it('fades only the decoration, never the name', () => {
    const dim = rules().find(([sel]) => sel === '.sh-cal-strip-pin > :not(.sh-cal-strip-name)')
    expect(dim?.[1]).toMatch(/opacity\s*:\s*var\(--sh-pin-dim\)/)
    const nameRules = rules().filter(([sel]) => /\.sh-cal-strip-name$/.test(sel.split(',').pop()!.trim()))
    for (const [, body] of nameRules) {
      expect(body).not.toMatch(/(?:^|[\s;])opacity\s*:/)
      const color = /(?:^|[\s;])color\s*:\s*([^;]+)/.exec(body)?.[1]?.trim()
      if (color) expect(['var(--sh-text)', 'var(--sh-text-muted)']).toContain(color)
    }
  })
})

describe('system album card', () => {
  it('lifts the muted line on its hearth-tinted info strip', () => {
    // Dark mode: --sh-text-muted on the 6% hearth tint is 4.3:1 (axe).
    const rule = rules().find(([sel]) => sel === '.sh-album-card--system .sh-album-info .sh-muted')
    expect(rule?.[1]).toMatch(/color\s*:\s*color-mix\(in srgb, var\(--sh-text-muted\) 75%, var\(--sh-text\)\)/)
  })
})

describe('space theme preview chip', () => {
  it('mixes the picked accent toward the theme ink', () => {
    const rule = rules().find(([sel]) => sel === '.sh-theme-preview-chip')
    const color = /(?:^|[\s;])color\s*:\s*([^;]+)/.exec(rule?.[1] ?? '')?.[1]
    expect(color).toMatch(/^color-mix\(in srgb, var\(--preview-accent.*\) 55%, var\(--sh-text\)\)$/)
  })
})
