/**
 * Guard for shared controls that must be ≥ 36 px tall on every pointer,
 * not only under ``(pointer: coarse)``: the protected-account notice's
 * "See what's limited" link (shown on several surfaces — one shared rule,
 * no per-surface override) and a feed post's reaction chips and
 * latest-comment row. jsdom does no layout, so check the rules.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

// jsdom gives import.meta.url an http: scheme; vitest still injects __dirname.
const css = readFileSync(join(__dirname, 'app.css'), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')

/** Bodies of every top-level rule whose selector list is exactly *selector*. */
function bodies(selector: string): string[] {
  const out: string[] = []
  const re = /([^{}]+)\{([^{}]*)\}/g
  let m: RegExpExecArray | null
  while ((m = re.exec(css)) !== null) {
    if (m[1].trim() === selector) out.push(m[2])
  }
  return out
}

describe('36 px tap targets', () => {
  it.each([
    '.sh-protected-notice__text .sh-link',
    '.sh-reactions .sh-reaction-chip',
    '.sh-post-latest-comment',
  ])('%s has a 36 px floor', (selector) => {
    expect(bodies(selector).some(b => /min-height:\s*36px/.test(b))).toBe(true)
  })

  it('no surface patches the protected-notice link on its own', () => {
    const scoped = css.match(/[^{}]+\.sh-protected-notice\s+\.sh-link\s*\{/g) ?? []
    expect(scoped).toEqual([])
  })
})
