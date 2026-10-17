/**
 * Guards for the author OnlinePill inside a post card's byline.
 *
 * ``.sh-post-meta`` is a column flexbox (author / pill / time stacked).
 * A flex item is blockified and, with the default
 * ``align-items: stretch``, stretched across the cross axis — so the
 * pill's ``display: inline-flex`` alone did not stop it from rendering
 * as a bordered box spanning the whole post header. jsdom does no
 * layout, so this pins the CSS rule that makes the pill hug its text.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

// jsdom gives import.meta.url an http: scheme; vitest still injects __dirname.
const css = readFileSync(join(__dirname, 'app.css'), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')

/** Concatenated bodies of the top-level rules whose selector list
 *  contains ``selector`` exactly. */
function bodiesFor(selector: string): string {
  const out: string[] = []
  const re = /([^{}]+)\{([^{}]*)\}/g
  for (const m of css.matchAll(re)) {
    const selectors = m[1].split(',').map(s => s.trim())
    if (selectors.includes(selector)) out.push(m[2])
  }
  return out.join('\n')
}

describe('OnlinePill in the post byline', () => {
  it('the byline is a column flexbox (the reason the pill would stretch)', () => {
    const meta = bodiesFor('.sh-post-meta')
    expect(meta).toMatch(/display\s*:\s*flex/)
    expect(meta).toMatch(/flex-direction\s*:\s*column/)
  })

  it('the pill opts out of the cross-axis stretch so it hugs its content', () => {
    expect(bodiesFor('.sh-post-meta > .sh-online-pill'))
      .toMatch(/align-self\s*:\s*flex-start/)
  })

  it('the base pill stays inline-flex and never full-width', () => {
    const pill = bodiesFor('.sh-online-pill')
    expect(pill).toMatch(/display\s*:\s*inline-flex/)
    expect(pill).not.toMatch(/width\s*:\s*100%/)
  })
})
