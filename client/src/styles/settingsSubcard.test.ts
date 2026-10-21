/**
 * Guards for the settings sub-card spacing.
 *
 * A ``.sh-settings-subcard`` drops its top padding and dashed divider only
 * when it opens its section. The rule used ``:first-of-type``, which also
 * matched the first sub-card AFTER plain toggle rows (it is the section's
 * first ``<div>``): Settings › Privacy's "Show in my sidebar" heading then
 * sat flush against the toggle row above it. jsdom does no layout, but it
 * does match selectors — so these tests run the real rule's selector
 * against the two real section shapes.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const css = readFileSync(join(__dirname, 'app.css'), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')

/** Selector lists of the top-level rules whose body matches *body*. */
function selectorsWith(body: RegExp): string[] {
  const out: string[] = []
  for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (body.test(m[2])) out.push(m[1].trim())
  }
  return out
}

const collapse = selectorsWith(/padding-top\s*:\s*0\s*;/)
  .filter(s => s.includes('.sh-settings-subcard'))
  .join(', ')

function section(html: string): HTMLElement {
  const el = document.createElement('section')
  el.className = 'sh-settings-section'
  el.innerHTML = html
  document.body.replaceChildren(el)
  return el
}

describe('settings sub-card spacing', () => {
  it('has exactly one collapse rule and it no longer uses :first-of-type', () => {
    expect(collapse).not.toBe('')
    expect(collapse).not.toContain(':first-of-type')
  })

  it('a sub-card after toggle rows keeps its top spacing (Privacy › sidebar)', () => {
    const s = section(`
      <h2>Privacy</h2>
      <label class="sh-toggle-row"><input type="checkbox"> Online status</label>
      <label class="sh-toggle-row"><input type="checkbox"> Picture and bio</label>
      <div class="sh-settings-subcard" id="sidebar"><h3>Show in my sidebar</h3></div>
      <div class="sh-settings-subcard" id="location"><h3>Space location</h3></div>`)
    expect(s.querySelector('#sidebar')!.matches(collapse)).toBe(false)
    expect(s.querySelector('#location')!.matches(collapse)).toBe(false)
    // The toggle row's own dashed bottom is the divider there — no second one.
    const noSecondLine = selectorsWith(/border-top\s*:\s*none/).join(', ')
    expect(s.querySelector('#sidebar')!.matches(noSecondLine)).toBe(true)
    expect(s.querySelector('#location')!.matches(noSecondLine)).toBe(false)
  })

  it('a sub-card right under the section title still opens it flush (Admin › Settings)', () => {
    const s = section(`
      <h2>Settings</h2>
      <div class="sh-settings-subcard" id="name"><h3>Home name</h3></div>
      <div class="sh-settings-subcard" id="home"><h3>Home location</h3></div>`)
    expect(s.querySelector('#name')!.matches(collapse)).toBe(true)
    expect(s.querySelector('#home')!.matches(collapse)).toBe(false)
  })
})
