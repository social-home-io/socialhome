/**
 * What Social Home tells an app about how to look (D14): language, text
 * direction, light/dark, reduced motion and eight colour tokens.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  APP_TOKEN_NAMES,
  DEFAULT_TOKENS,
  currentAppContext,
  readTokens,
  toHexColor,
  watchAppContext,
  type AppHostContext,
} from './appContext'
import { locale } from '@/i18n/i18n'

describe('toHexColor', () => {
  it.each([
    ['#F4ECE0', '#f4ece0'],
    ['#abc', '#aabbcc'],
    ['rgb(244, 236, 224)', '#f4ece0'],
    ['rgb(244 236 224)', '#f4ece0'],
    ['rgba(26, 24, 20, 0.5)', '#1a181480'],
    ['rgb(26 24 20 / 50%)', '#1a181480'],
    ['rgba(0, 0, 0, 1)', '#000000'],
    // Chrome's computed form of a color-mix() in srgb.
    ['color(srgb 0.705882 0.282353 0.12549)', '#b44820'],
    ['color(srgb 1 1 1 / 0.25)', '#ffffff40'],
  ])('%s → %s', (input, out) => {
    expect(toHexColor(input)).toBe(out)
  })

  it.each([
    '',
    'red',
    'var(--sh-bg)',
    'url(https://evil.example/x.png)',
    'rgb(1, 2, 3); background: url(x)',
    '#12345',
    'expression(alert(1))',
    'color(display-p3 1 0 0)',
    'rgb(300, 0, 0)',
    '#ffffff}body{display:none',
  ])('refuses %j — only plain colours leave the host', (input) => {
    expect(toHexColor(input)).toBeNull()
  })
})

describe('readTokens', () => {
  it('names the eight tokens', () => {
    expect([...APP_TOKEN_NAMES]).toEqual(
      ['bg', 'surface', 'text', 'muted', 'accent', 'onAccent', 'border', 'focus'],
    )
  })

  it('every value is #rrggbb[aa] — the default palette when the page has no computed colours', () => {
    for (const theme of ['light', 'dark'] as const) {
      const tokens = readTokens(theme)
      expect(Object.keys(tokens)).toEqual([...APP_TOKEN_NAMES])
      for (const v of Object.values(tokens)) expect(v).toMatch(/^#[0-9a-f]{6}([0-9a-f]{2})?$/)
    }
    // jsdom resolves no var(): the defaults (same values as tokens.css).
    expect(readTokens('light')).toEqual(DEFAULT_TOKENS.light)
    expect(readTokens('dark')).toEqual(DEFAULT_TOKENS.dark)
    expect(DEFAULT_TOKENS.light.bg).toBe('#f4ece0')
    expect(DEFAULT_TOKENS.dark.bg).toBe('#1a1612')
  })

  it('reads the page’s own colours when the browser computes them', () => {
    const spy = vi.spyOn(window, 'getComputedStyle').mockImplementation(
      () => ({ color: 'rgb(1, 2, 3)' }) as CSSStyleDeclaration,
    )
    try {
      expect(readTokens('light').accent).toBe('#010203')
    } finally {
      spy.mockRestore()
    }
  })

  it('a value that isn’t a plain colour falls back to the default', () => {
    const spy = vi.spyOn(window, 'getComputedStyle').mockImplementation(
      () => ({ color: 'url(javascript:alert(1))' }) as CSSStyleDeclaration,
    )
    try {
      expect(readTokens('dark')).toEqual(DEFAULT_TOKENS.dark)
    } finally {
      spy.mockRestore()
    }
  })
})

describe('currentAppContext', () => {
  const root = document.documentElement
  beforeEach(() => {
    root.className = ''
    root.removeAttribute('dir')
    locale.value = 'en'
  })
  afterEach(() => {
    root.className = ''
    root.removeAttribute('dir')
    locale.value = 'en'
  })

  it('light, ltr, English by default', () => {
    const c = currentAppContext()
    expect(c).toMatchObject({ locale: 'en', dir: 'ltr', theme: 'light', reducedMotion: false })
    expect(c.tokens).toEqual(DEFAULT_TOKENS.light)
  })

  it('follows the SPA’s resolved theme (the class on <html>, set for auto too)', () => {
    root.classList.add('sh-theme-dark')
    const c = currentAppContext()
    expect(c.theme).toBe('dark')
    expect(c.tokens).toEqual(DEFAULT_TOKENS.dark)
  })

  it('the language and its direction', () => {
    locale.value = 'de'
    root.dir = 'rtl'
    expect(currentAppContext()).toMatchObject({ locale: 'de', dir: 'rtl' })
  })

  it('reducedMotion from the system setting', () => {
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: q.includes('reduce'), media: q,
      addEventListener: () => {}, removeEventListener: () => {},
    }))
    try {
      expect(currentAppContext().reducedMotion).toBe(true)
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

describe('watchAppContext', () => {
  const root = document.documentElement
  afterEach(() => {
    root.className = ''
    locale.value = 'en'
  })

  const flush = () => new Promise<void>(r => setTimeout(r, 0))

  it('calls back once per real change: theme and language', async () => {
    const seen: AppHostContext[] = []
    const stop = watchAppContext(c => seen.push(c))
    root.classList.add('sh-theme-light')
    await flush()
    expect(seen).toEqual([]) // nothing the app sees changed
    root.classList.replace('sh-theme-light', 'sh-theme-dark')
    await flush()
    expect(seen.map(c => c.theme)).toEqual(['dark'])
    locale.value = 'nl'
    root.lang = 'nl'
    await flush()
    expect(seen.map(c => c.locale)).toEqual(['en', 'nl'])
    stop()
    root.classList.replace('sh-theme-dark', 'sh-theme-light')
    await flush()
    expect(seen).toHaveLength(2)
  })
})
