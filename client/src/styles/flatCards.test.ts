/**
 * Post cards are flat app-wide (owner decision, Spaces redesign): no
 * per-card rotation and no washi-tape pseudo-element on the feed card,
 * the comment overlay or the empty-state card. The taped-paper look is
 * kept only where paper is the content — the Stickies board.
 *
 * jsdom does no layout, so these pin the CSS itself.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

// jsdom gives import.meta.url an http: scheme; vitest still injects __dirname.
const css = readFileSync(join(__dirname, 'app.css'), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')

/** Every rule (selector list + body), at-rule nesting included. */
function rules(): { selectors: string[], body: string }[] {
  const out: { selectors: string[], body: string }[] = []
  for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    out.push({ selectors: m[1].split(',').map(s => s.trim()), body: m[2] })
  }
  return out
}

/** Bodies of the rules with a selector that targets ``cls`` (any
 *  compound or pseudo built on it). */
function bodiesTouching(cls: string): string[] {
  const re = new RegExp(`\\${cls}(?![\\w-])`)
  return rules().filter(r => r.selectors.some(s => re.test(s))).map(r => r.body)
}

describe('flat post cards', () => {
  it.each(['.sh-feed-item', '.sh-empty-state', '.sh-comment-overlay'])(
    '%s never rotates', (cls) => {
      for (const body of bodiesTouching(cls)) {
        expect(body).not.toMatch(/rotate\(/)
      }
    })

  it.each(['.sh-feed-item', '.sh-empty-state', '.sh-comment-overlay'])(
    '%s carries no tape pseudo-element', (cls) => {
      const pseudo = rules().filter(r => r.selectors.some(
        s => s.includes(cls) && /::before/.test(s) && !/\*::before/.test(s)))
      expect(pseudo.map(r => r.selectors.join(', '))).toEqual([])
    })

  it('the stagger variable and settle-in keyframes are gone', () => {
    expect(css).not.toMatch(/--sh-feed-rot/)
    expect(css).not.toMatch(/sh-card-settle/)
  })

  it('a feed card keeps the list anatomy: 1 px border on the shared radius', () => {
    const body = rules().find(r => r.selectors.includes('.sh-feed-item'))!.body
    expect(body).toMatch(/border\s*:\s*1px solid var\(--sh-border\)/)
    expect(body).toMatch(/border-radius\s*:\s*var\(--sh-radius-lg\)/)
  })

  it('the Stickies board keeps its taped paper look', () => {
    expect(bodiesTouching('.sh-sticky__grip').join('\n')).toMatch(/var\(--sh-sticky-tape\)/)
    expect(rules().some(r => r.selectors.includes('.sh-sticky-grid .sh-sticky::before'))).toBe(true)
  })
})
