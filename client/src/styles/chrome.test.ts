/**
 * Guards for the app chrome's layering — the desktop sidebar's identity
 * footer and the TopBar's search results.
 *
 * jsdom does no layout, so these pin the CSS rules that keep the two
 * bugs fixed:
 *
 * - The sidebar's identity strip was ``position: sticky; bottom: 0``
 *   inside the scrolling sidebar, so it painted over whichever nav items
 *   sat behind it (Corner / Apps at 1280×720). It is now a plain flex
 *   footer under a dedicated ``.sh-sidenav-scroll`` region.
 * - The search results were an in-flow child of the TopBar flex row, so
 *   opening them grew the sticky bar and pushed the page title down.
 *   They are now an absolutely positioned popover.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

// jsdom gives import.meta.url an http: scheme; vitest still injects __dirname.
const css = readFileSync(join(__dirname, 'app.css'), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')

interface Rule { selectors: string[]; body: string; media: string | null }

/** Every flat rule (``@media`` inner rules included, tagged with the
 *  enclosing media prelude). */
function allRules(): Rule[] {
  const rules: Rule[] = []
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
      const media = preludes.find(p => p.startsWith('@media')) ?? null
      rules.push({ selectors: prelude.split(',').map(s => s.trim()), body, media })
    }
  }
  return rules
}

const rules = allRules()
const bodiesFor = (selector: string, media: 'any' | 'none' = 'any') =>
  rules
    .filter(r => r.selectors.includes(selector) && (media === 'any' || r.media === null))
    .map(r => r.body)
    .join('\n')

describe('desktop sidebar identity footer', () => {
  it('is never sticky / fixed / absolute (it sits in normal flow under the nav)', () => {
    const footer = rules.filter(r => r.selectors.some(s => /\.sh-sidenav-identity$/.test(s)))
    expect(footer.length).toBeGreaterThan(0)
    for (const r of footer) expect(r.body).not.toMatch(/position\s*:\s*(sticky|fixed|absolute)/)
    expect(bodiesFor('.sh-sidenav-identity')).toMatch(/flex\s*:\s*0 0 auto/)
  })

  it('the nav region owns the scroll and can shrink below its content', () => {
    const region = bodiesFor('.sh-sidenav-scroll', 'none')
    expect(region).toMatch(/overflow-y\s*:\s*auto/)
    expect(region).toMatch(/min-height\s*:\s*0/)
    expect(region).toMatch(/flex\s*:\s*1 1 auto/)
    // The sidebar itself no longer scrolls — otherwise the footer would
    // scroll away with (or over) the items again.
    const aside = bodiesFor('.sh-sidenav', 'none')
    expect(aside).toMatch(/overflow\s*:\s*hidden/)
    expect(aside).not.toMatch(/overflow-y\s*:\s*auto/)
  })
})

describe('TopBar search results popover', () => {
  it('is absolutely positioned under a relatively positioned search wrapper', () => {
    expect(bodiesFor('.sh-search', 'none')).toMatch(/position\s*:\s*relative/)
    const popover = bodiesFor('.sh-search-popover', 'none')
    expect(popover).toMatch(/position\s*:\s*absolute/)
    expect(popover).toMatch(/top\s*:\s*100%/)
    // Above page content (inside the TopBar's stacking context), below
    // modals — the dropdown layer, never the modal / toast layers.
    expect(popover).toMatch(/z-index\s*:\s*var\(--sh-z-dropdown\)/)
  })

  it('scrolls inside a height cap so a long list never runs off-screen', () => {
    const list = bodiesFor('.sh-search-popover__list', 'none')
    expect(list).toMatch(/max-height\s*:/)
    expect(list).toMatch(/overflow-y\s*:\s*auto/)
  })

  it('spans the TopBar with the page gutter on phones', () => {
    const phone = rules.filter(r => r.media?.includes('max-width: 768px'))
    const wrapper = phone.find(r => r.selectors.includes('.sh-topbar > .sh-search'))
    const popover = phone.find(r => r.selectors.includes('.sh-topbar .sh-search-popover'))
    expect(wrapper?.body).toMatch(/position\s*:\s*static/)
    expect(popover?.body).toMatch(/inset-inline\s*:\s*var\(--sh-space-md\)/)
  })

  it('rows lay the name and @handle out with a gap and truncate them', () => {
    const row = bodiesFor('.sh-search-popover .sh-search-result', 'none')
    expect(row).toMatch(/display\s*:\s*flex/)
    expect(row).toMatch(/gap\s*:/)
    const parts = bodiesFor('.sh-search-result__name', 'none')
    expect(parts).toMatch(/text-overflow\s*:\s*ellipsis/)
    expect(parts).toMatch(/white-space\s*:\s*nowrap/)
    expect(parts).toMatch(/min-width\s*:\s*0/)
  })
})
