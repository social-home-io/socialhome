/**
 * Guard for the shared ``.sh-form-actions`` Save row.
 *
 * The row doubles as a full-bleed sticky modal footer: negative margins
 * cancel ``.sh-modal-body``'s padding so it runs flush to the sheet
 * edges. That geometry is only right inside a modal body — applied to an
 * in-page form, the negative bottom margin pulled the next section up
 * under the row (Settings › Profile hid its "Public @handle" heading
 * behind the profile's Save bar) and the side margins pushed the row past
 * narrower cards. jsdom does no layout, so guard the rules themselves:
 * every negative margin on the row must be scoped to ``.sh-modal-body``,
 * and the modal footer must keep its bleed.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

// jsdom gives import.meta.url an http: scheme; vitest still injects __dirname.
const css = readFileSync(join(__dirname, 'app.css'), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')

interface Rule { selectors: string[]; body: string; media: boolean }

/** Flat rules (inner rules of an ``@media`` block included) whose
 *  selector list targets the row itself, not a descendant of it. */
function formActionsRules(): Rule[] {
  const rules: Rule[] = []
  // Walk the braces so a rule knows whether it sits in an @media block.
  const preludes: string[] = []
  let start = 0
  for (let i = 0; i < css.length; i++) {
    const c = css[i]
    if (c === '{') {
      preludes.push(css.slice(start, i).trim())
      start = i + 1
    } else if (c === '}') {
      const prelude = preludes.pop() ?? ''
      const body = css.slice(start, i)
      start = i + 1
      if (prelude.startsWith('@') || body.includes('{')) continue
      const selectors = prelude.split(',').map(s => s.trim()).filter(Boolean)
      if (!selectors.some(s => /\.sh-form-actions(\.[\w-]+)*$/.test(s))) continue
      rules.push({ selectors, body, media: preludes.some(p => p.startsWith('@media')) })
    }
  }
  return rules
}

const NEGATIVE_MARGIN = /margin(-left|-right|-bottom)?\s*:[^;]*calc\(\s*-1/

describe('.sh-form-actions', () => {
  it('only takes negative (full-bleed) margins inside a modal body', () => {
    const offenders = formActionsRules()
      .filter(r => NEGATIVE_MARGIN.test(r.body))
      .flatMap(r => r.selectors)
      .filter(s => /\.sh-form-actions/.test(s) && !s.startsWith('.sh-modal-body '))
    expect(offenders).toEqual([])
  })

  it('stays a sticky, opaque row on in-page forms', () => {
    const base = formActionsRules().filter(
      r => r.selectors.includes('.sh-form-actions'),
    )
    expect(base.length).toBeGreaterThan(0)
    for (const r of base) expect(r.body).not.toMatch(NEGATIVE_MARGIN)
    const main = base.find(r => !r.media)!
    expect(main.body).toMatch(/position:\s*sticky/)
    expect(main.body).toMatch(/background:\s*var\(--sh-bg-tertiary\)/)
  })

  it('keeps the flush sticky footer in modals, desktop and phone', () => {
    const modal = formActionsRules().filter(
      r => r.selectors.includes('.sh-modal-body .sh-form-actions'),
    )
    const desk = modal.find(r => !r.media)
    const phone = modal.find(r => r.media)
    expect(desk?.body).toMatch(/margin-bottom:\s*calc\(-1 \* var\(--sh-space-lg\)\)/)
    expect(desk?.body).toMatch(/margin-left:\s*calc\(-1 \* var\(--sh-space-lg\)\)/)
    expect(phone?.body).toMatch(/margin-bottom:\s*calc\(-1 \* var\(--sh-space-md\)\)/)
  })
})
