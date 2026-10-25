/**
 * Font guard — every font the SPA uses is bundled and served by the
 * household itself, under every document base.
 *
 * A font CDN (Google Fonts, used until 2026-10) leaks each viewer's IP
 * address and user agent to the font host on every page load and fails
 * on a LAN-only / offline install. The CSP refuses it too
 * (``font-src 'self'``, socialhome/csp.py) — this test catches the
 * regression in source, before a browser silently falls back to a
 * system font.
 *
 * Under the HA ingress prefix (``<base href="/api/hassio_ingress/<token>/">``)
 * an absolute ``/…`` URL skips the prefix and 404s against HA Core, so
 * every font URL is relative: Vite fingerprints the file and rewrites
 * the reference relative to the emitted CSS. ``client/scripts/
 * fonts-check.js`` asserts the same thing on the built bundle in CI.
 */
import { describe, expect, it } from 'vitest'
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { APP_FONT_STACK, FONT_STACKS } from '@/utils/themeFonts'

// jsdom gives import.meta.url an http: scheme; vitest still injects __dirname.
const STYLES = __dirname
const SRC = join(STYLES, '..')
const CLIENT = join(SRC, '..')
const FONTS_CSS = join(STYLES, 'fonts.css')
const THIRD_PARTY_FONT_HOST = /fonts\.(googleapis|gstatic)\.com|use\.typekit|fonts\.bunny/i

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(css|ts|tsx|html|js)$/.test(name)) out.push(p)
  }
  return out
}

function fontFaces(css: string): string[] {
  return css.match(/@font-face\s*\{[^}]*\}/g) ?? []
}

describe('bundled fonts (styles/fonts.css)', () => {
  const css = readFileSync(FONTS_CSS, 'utf8')
  const faces = fontFaces(css)

  it('declares every brand family the tokens name', () => {
    const families = new Set(faces.map((f) => /font-family:\s*'([^']+)'/.exec(f)?.[1]))
    for (const family of ['Manrope', 'Fraunces', 'JetBrains Mono', 'Nunito Variable']) {
      expect(families, family).toContain(family)
    }
  })

  it('swaps in fonts instead of blocking first paint', () => {
    for (const face of faces) expect(face).toMatch(/font-display:\s*swap/)
  })

  it('points every src at a relative URL of a file that exists', () => {
    const urls = [...css.matchAll(/url\(\s*['"]?([^'")]+)['"]?\s*\)/g)].map((m) => m[1])
    expect(urls.length).toBeGreaterThan(0)
    for (const url of urls) {
      // Relative only: an absolute ``/…`` skips the ingress prefix, a
      // scheme / ``//host`` is a third-party fetch, ``data:`` is refused
      // by ``font-src 'self'``.
      expect(url, url).toMatch(/^\.\.?\//)
      expect(existsSync(resolve(dirname(FONTS_CSS), url)), url).toBe(true)
    }
  })

  it('covers the latin + latin-ext subsets for every brand family', () => {
    for (const family of ['Manrope', 'Fraunces', 'JetBrains Mono']) {
      const own = faces.filter((f) => f.includes(`'${family}'`))
      expect(own.some((f) => /-latin-[a-z-]*normal\.woff2/.test(f) && f.includes('U+0000-00FF')), family).toBe(true)
      expect(own.some((f) => /-latin-ext-/.test(f)), family).toBe(true)
    }
  })

  it('ships each font licence beside the font', () => {
    const fontDir = join(SRC, 'assets', 'fonts')
    for (const family of readdirSync(fontDir)) {
      expect(existsSync(join(fontDir, family, 'OFL.txt')), family).toBe(true)
    }
    for (const name of ['Manrope', 'Fraunces', 'JetBrainsMono', 'Nunito']) {
      expect(existsSync(join(CLIENT, 'public', 'assets', `${name}-OFL.txt`)), name).toBe(true)
    }
  })
})

describe('no third-party font host anywhere in the SPA source', () => {
  it('src/ and index.html never reference a font CDN', () => {
    const files = [...walk(SRC), join(CLIENT, 'index.html')].filter((f) => f !== __filename)
    const offenders = files.filter((f) => THIRD_PARTY_FONT_HOST.test(readFileSync(f, 'utf8')))
    expect(offenders).toEqual([])
  })

  it('the index.html font preload is relative (resolves under <base href>)', () => {
    const html = readFileSync(join(CLIENT, 'index.html'), 'utf8')
    const preloads = [...html.matchAll(/<link[^>]*rel="preload"[^>]*>/g)].map((m) => m[0])
    for (const tag of preloads.filter((t) => t.includes('as="font"'))) {
      // ``/src/…`` is Vite's project-root form; the build rewrites it to
      // ``./assets/<hash>.woff2`` (checked on the bundle by fonts-check.js).
      expect(tag).toMatch(/href="\/src\/assets\/fonts\//)
      expect(tag).toContain('crossorigin')
    }
  })
})

/** The colour emoji fonts, behind one alias (``fonts.css``) whose
 *  ``unicode-range`` holds emoji code points only. On Linux and ChromeOS
 *  the generic ``sans-serif`` / ``serif`` resolve to DejaVu, which draws
 *  U+1F600–1F64F (😂 😮 😢) as monochrome outlines, so a colour family has
 *  to come before the generic one. Named directly, Noto Color Emoji would
 *  then also draw digits, ``#``, ``*`` and spaces (it carries them for
 *  keycaps) wherever no earlier font covers them — the public viewer, a
 *  Serif theme without Georgia. The alias can only draw emoji. */
const EMOJI_ALIAS = 'SH Color Emoji'
const EMOJI_FONTS = ['Apple Color Emoji', 'Segoe UI Emoji', 'Noto Color Emoji']
const GENERIC_FAMILIES = new Set(['sans-serif', 'serif', 'monospace', 'system-ui'])

function expectColourEmoji(stack: string, where: string): void {
  const families = stack.split(',').map((f) => f.trim().replace(/^['"]|['"]$/g, ''))
  const generic = families.findIndex((f) => GENERIC_FAMILIES.has(f))
  const at = families.indexOf(EMOJI_ALIAS)
  expect(at, `${where} lists ${EMOJI_ALIAS}`).toBeGreaterThan(-1)
  if (generic > -1) expect(at, `${where}: ${EMOJI_ALIAS} before ${families[generic]}`).toBeLessThan(generic)
  for (const font of EMOJI_FONTS) {
    expect(families, `${where} names ${font} only through the alias`).not.toContain(font)
  }
}

/** The ``unicode-range`` of the alias's ``@font-face`` in ``css``, as
 *  [start, end] code point pairs. */
function emojiAliasRanges(css: string, where: string): [number, number][] {
  const face = (css.match(/@font-face\s*\{[^}]*\}/g) ?? []).find((f) => f.includes(`'${EMOJI_ALIAS}'`))
  expect(face, `${where} declares @font-face '${EMOJI_ALIAS}'`).toBeTruthy()
  for (const font of EMOJI_FONTS) expect(face, `${where}: local('${font}')`).toContain(`local('${font}')`)
  const range = /unicode-range:\s*([^;}]+)/.exec(face!)?.[1] ?? ''
  return range.split(',').map((r) => {
    const [a, b] = r.trim().replace(/^U\+/i, '').split('-')
    return [parseInt(a, 16), parseInt(b ?? a, 16)] as [number, number]
  })
}

function expectEmojiOnly(css: string, where: string): void {
  const ranges = emojiAliasRanges(css, where)
  const covers = (cp: number) => ranges.some(([a, b]) => cp >= a && cp <= b)
  for (const ch of ' 0123456789#*aZ.,ü') {
    expect(covers(ch.codePointAt(0)!), `${where} alias must not draw ${JSON.stringify(ch)}`).toBe(false)
  }
  for (const ch of ['😂', '😮', '😢', '😊', '❤', '🔥', '👏', '☕', '🎃', '\u200d', '\ufe0f']) {
    expect(covers(ch.codePointAt(0)!), `${where} alias draws ${ch}`).toBe(true)
  }
}

function fontStackIn(css: string, pattern: string): string {
  const m = new RegExp(`${pattern}:\\s*([^;]+);`).exec(css)
  expect(m, pattern).not.toBeNull()
  return m![1].replace(/\s+/g, ' ')
}

describe('emoji draw in colour everywhere', () => {
  const tokens = readFileSync(join(STYLES, 'tokens.css'), 'utf8')

  it('the body and display font tokens list a colour emoji family', () => {
    expectColourEmoji(fontStackIn(tokens, '--sh-font-family'), '--sh-font-family')
    expectColourEmoji(fontStackIn(tokens, '--sh-font-family-display'), '--sh-font-family-display')
  })

  it('every household / space theme font lists one too', () => {
    for (const [id, stack] of Object.entries(FONT_STACKS)) {
      if (stack === APP_FONT_STACK) continue // ``system`` = the token above
      expectColourEmoji(stack, `theme font ${id}`)
    }
  })

  it('the public highlight viewer (GFS + pinned page) lists one', () => {
    const css = readFileSync(join(CLIENT, 'gfs', 'public_highlight.css'), 'utf8')
    expectColourEmoji(fontStackIn(css, 'html,body\\{[^}]*?font-family'), 'public_highlight.css')
    expectEmojiOnly(css, 'public_highlight.css')
  })

  it('the alias draws emoji only, never digits, # or spaces', () => {
    expectEmojiOnly(readFileSync(FONTS_CSS, 'utf8'), 'fonts.css')
  })
})
