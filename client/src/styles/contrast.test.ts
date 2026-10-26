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

describe('calendar person chips', () => {
  // The old polaroid pin faded a hidden person's name below 4.5:1. A
  // chip is a live toggle: an off chip keeps the full ink, and no chip
  // state fades (the selected tint is the only state difference).
  const chipRules = rules().filter(([sel]) =>
    sel.split(',').some(s => /\.sh-cal-person(?:--[\w-]+|\.is-on|:[\w-]+)*$/.test(s.trim())))

  it('never fades a chip', () => {
    expect(chipRules.length).toBeGreaterThan(0)
    const faded = chipRules.filter(([, body]) => /(?:^|[\s;])opacity\s*:/.test(body))
    expect(faded.map(([sel]) => sel)).toEqual([])
  })

  it('labels an off chip in the full ink', () => {
    const base = chipRules.find(([sel]) => sel === '.sh-cal-person')
    expect(/(?:^|[\s;])color\s*:\s*([^;]+)/.exec(base?.[1] ?? '')?.[1]?.trim()).toBe('var(--sh-text)')
  })
})

describe('system album card', () => {
  it('lifts the muted line on its hearth-tinted info strip', () => {
    // Dark mode: --sh-text-muted on the 6% hearth tint is 4.3:1 (axe).
    const rule = rules().find(([sel]) => sel === '.sh-album-card--system .sh-album-info .sh-muted')
    expect(rule?.[1]).toMatch(/color\s*:\s*color-mix\(in srgb, var\(--sh-text-muted\) 75%, var\(--sh-text\)\)/)
  })
})

describe('theme preview chip', () => {
  it('labels the chip in the theme ink, not the picked accent', () => {
    // A bright preset accent (#FFD447) as text on its own tint is ~1.5:1.
    const rule = rules().find(([sel]) => sel === '.sh-theme-preview-chip')
    const color = /(?:^|[\s;])color\s*:\s*([^;]+)/.exec(rule?.[1] ?? '')?.[1]
    expect(color).toBe('var(--sh-text)')
  })
})

describe('own chat bubble meta', () => {
  // Dark: --sh-on-primary-fill at 80 % on the hearth fill is ~4.3:1 — the
  // time, receipts and "edited" on your own bubble keep the full ink.
  it('never fades the time, a receipt or "edited" on your own bubble', () => {
    const sent = rules().find(([sel]) => sel === '.sh-message--mine .sh-receipt--sent')
    expect(sent?.[1]).toMatch(/color\s*:\s*var\(--sh-on-primary-fill\)\s*;/)
    const edited = rules().find(([sel]) => sel === '.sh-message--mine .sh-message-edited')
    expect(edited?.[1]).toMatch(/opacity\s*:\s*1\s*;/)
    const meta = rules().find(([sel]) => sel === '.sh-message--mine .sh-message-meta')
    expect(meta?.[1]).toMatch(/color\s*:\s*var\(--sh-on-primary-fill\)/)
    expect(meta?.[1]).not.toMatch(/opacity/)
  })
})

describe('keyboard focus ring', () => {
  // The plain hearth ring was ~3.1:1 on the hearth tints (active nav,
  // Needs-you card, selected chips): every focus ring takes
  // --sh-focus-ring (the text hearth, ≥ 4.5:1 on every light surface).
  it('draws every hearth focus ring in --sh-focus-ring', () => {
    const plain = rules().filter(([sel, body]) =>
      /focus/.test(sel) && /outline\s*:[^;]*var\(--sh-primary\)/.test(body))
    expect(plain.map(([sel]) => sel)).toEqual([])
    const global = rules().find(([sel]) => sel === ':focus-visible')
    expect(global?.[1]).toMatch(/outline\s*:\s*2px solid var\(--sh-focus-ring\)/)
  })
})

describe('space banners and hints', () => {
  it('lifts the muted line on the hearth-tinted archived / follower banner', () => {
    // Light: --sh-text-muted on the 8 % hearth tint is 4.42:1 (axe).
    const rule = rules().find(([sel]) => sel === '.sh-subscriber-banner__body .sh-muted')
    expect(rule?.[1]).toMatch(/color\s*:\s*color-mix\(in srgb, var\(--sh-text-muted\) 75%, var\(--sh-text\)\)/)
  })

  it('lifts the muted lines on your own (hearth-tinted) member row', () => {
    // Dark: "joined today" on the 6 % hearth tint is 4.26:1 (axe).
    const rule = rules().find(([sel]) => sel === '.sh-member-row--me .sh-member-joined')
    expect(rule?.[1]).toMatch(/color\s*:\s*color-mix\(in srgb, var\(--sh-text-muted\) 75%, var\(--sh-text\)\)/)
  })

  it('underlines a link inside a settings hint (link-in-text-block)', () => {
    const rule = rules().find(([sel]) => sel === '.sh-ss-hint a')
    expect(rule?.[1]).toMatch(/text-decoration\s*:\s*underline/)
  })
})

describe('highlight viewer chrome', () => {
  // The viewer is black in both themes; its header / footer took the light
  // theme's dark ink, so the ghost Close and "Publish public link" were
  // 1.18:1. Viewer-scoped tokens keep every shared piece light on black.
  const hex = (h: string): [number, number, number] =>
    [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16) / 255) as [number, number, number]
  const lum = ([r, g, b]: [number, number, number]) => {
    const f = (c: number) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
  }
  const onBlack = (h: string) => (lum(hex(h)) + 0.05) / 0.05
  const scope = rules().find(([sel]) => sel === '.sh-hlv')
  const token = (name: string) =>
    new RegExp(`${name}\\s*:\\s*(#[0-9A-Fa-f]{6})`).exec(scope?.[1] ?? '')?.[1] ?? '#000000'

  it('scopes its own ink to the whole viewer, its sheets included', () => {
    expect(scope).toBeTruthy()
    expect(token('--sh-bg')).toBe('#000000')
  })

  it('reads ≥ 4.5:1 on black for text, muted text and the focus ring', () => {
    for (const name of ['--sh-text', '--sh-text-muted', '--sh-focus-ring']) {
      expect(onBlack(token(name)), name).toBeGreaterThanOrEqual(4.5)
    }
  })

  it('draws the ghost button outline ≥ 3:1 on black', () => {
    expect(onBlack(token('--sh-border-strong'))).toBeGreaterThanOrEqual(3)
  })

  it('draws the reaction tray in white ink, your reaction ringed', () => {
    // Colour emoji come from the font token (#1164); white ink keeps a
    // monochrome fallback glyph visible on the dark buttons.
    const btn = rules().find(([sel]) => sel === '.sh-hlv__emobtn')
    expect(btn?.[1]).toMatch(/color\s*:\s*#FFFFFF/)
    const on = rules().find(([sel]) => sel === '.sh-hlv__emobtn.is-on')
    expect(on?.[1]).toMatch(/box-shadow\s*:\s*inset 0 0 0 3px #F08A4B/)
    // The ring against the dark "on" fill and the black page: ≥ 3:1.
    expect(onBlack('#F08A4B')).toBeGreaterThanOrEqual(3)
  })
})
