/**
 * Right-to-left guards for the spaces surfaces and the app chrome.
 *
 * jsdom does no layout, so these pin the CSS / markup that keeps RTL
 * mirrored:
 *
 * - Space, calendar, member and chrome rules use logical properties
 *   (``margin-inline-start``, ``inset-inline-end``, ``text-align: start``…),
 *   so card stripes, tape, popovers and the drawer flip with ``dir``.
 * - ``.sh-flip-rtl`` mirrors drawn SVG arrows only. ‹ › « » are
 *   bidi-mirrored characters that RTL already turns; flipping them as
 *   well pointed the settings back link the LTR way.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

// jsdom gives import.meta.url an http: scheme; vitest still injects __dirname.
const css = readFileSync(join(__dirname, 'app.css'), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')
const SRC = join(__dirname, '..')

/** Rules whose selectors name these blocks must mirror in RTL. */
const SCOPED = /\.sh-(?:space|spaces|ss|member|cal|calendar|event|moderation|subscribed|sidenav|mobile-drawer|mobile-nav|post-overflow-wrap)(?:[-_\s.:>+~,[]|$)/

/** Physical inline-axis properties (``left: 50%`` + translate centring is
 *  direction-neutral, so it is allowed). */
const PHYSICAL = /(?:^|[\s;{])(?:(?:margin|padding|border)-(?:left|right)(?:-[a-z]+)?|border-(?:top|bottom)-(?:left|right)-radius|(?:left|right)\s*:(?!\s*50%)|text-align\s*:\s*(?:left|right)|float\s*:\s*(?:left|right))/

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.tsx$/.test(name) && !/\.test\.tsx$/.test(name)) out.push(p)
  }
  return out
}

describe('RTL', () => {
  it('space, calendar, member and chrome rules use logical properties', () => {
    const offenders: string[] = []
    for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      const sel = m[1].trim().split('\n').pop()!.trim()
      if (!SCOPED.test(sel)) continue
      const bad = m[2].split(';').filter(d => PHYSICAL.test(d))
      if (bad.length) offenders.push(`${sel} → ${bad.map(d => d.trim()).join('; ')}`)
    }
    expect(offenders).toEqual([])
  })

  it('the phone drawer slides in from the inline-start edge in RTL too', () => {
    expect(css).toMatch(/\[dir='rtl'\] \.sh-mobile-drawer \{[^}]*translateX\(101%\)/)
    expect(css).toMatch(/\.sh-mobile-drawer\.sh-mobile-drawer--open \{ transform: translateX\(0\); \}/)
  })

  it('never flips a bidi-mirrored text glyph', () => {
    const offenders: string[] = []
    for (const file of walk(SRC)) {
      const src = readFileSync(file, 'utf8')
      for (const m of src.matchAll(/sh-flip-rtl[^>]*>\s*([‹›«»]|&[lr]saquo;|&[lr]aquo;)/g)) {
        offenders.push(`${file.slice(SRC.length + 1)}: ${m[0]}`)
      }
    }
    expect(offenders).toEqual([])
  })
})
