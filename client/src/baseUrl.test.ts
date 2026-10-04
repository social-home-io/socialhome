import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
// Note: ``baseUrl.ts`` evaluates ``document.baseURI`` at module load,
// which in JSDOM resolves to ``http://localhost/`` (the default
// origin). Tests therefore exercise the no-prefix code path
// directly; the prefixed path is exercised by the helpers'
// own logic (``addBase`` / ``stripBase`` are pure functions that
// don't read from ``document``).
import { basePath, addBase, stripBase, appHref } from './baseUrl'

describe('baseUrl', () => {
  describe('basePath (no-prefix, JSDOM default)', () => {
    it('is /', () => {
      expect(basePath).toBe('/')
    })
  })

  describe('stripBase (pure function)', () => {
    it('returns pathname unchanged when basePath is /', () => {
      expect(stripBase('/feed')).toBe('/feed')
      expect(stripBase('/spaces/abc')).toBe('/spaces/abc')
      expect(stripBase('/')).toBe('/')
    })
  })

  describe('addBase (pure function)', () => {
    it('returns path unchanged when basePath is /', () => {
      expect(addBase('/feed')).toBe('/feed')
      expect(addBase('/')).toBe('/')
      expect(addBase('feed?x=1')).toBe('feed?x=1')
    })
  })
})

describe('appHref (no-prefix, JSDOM default)', () => {
  it('leaves every href unchanged when basePath is /', () => {
    expect(appHref('/post/p1')).toBe('/post/p1')
    expect(appHref('https://example.org/x')).toBe('https://example.org/x')
  })
})

describe('under the HA ingress prefix (/api/hassio_ingress/<token>/)', () => {
  let baseEl: HTMLBaseElement
  beforeEach(() => {
    baseEl = document.createElement('base')
    baseEl.href = '/api/hassio_ingress/tok/'
    document.head.prepend(baseEl)
    vi.resetModules()
  })
  afterEach(() => {
    baseEl.remove()
    vi.resetModules()
  })

  it('appHref adds the ingress prefix to in-app paths only', async () => {
    const mod = await import('./baseUrl')
    expect(mod.basePath).toBe('/api/hassio_ingress/tok/')
    expect(mod.appHref('/post/p1')).toBe('/api/hassio_ingress/tok/post/p1')
    expect(mod.appHref('/')).toBe('/api/hassio_ingress/tok/')
    expect(mod.appHref('https://example.org/x')).toBe('https://example.org/x')
    expect(mod.appHref('//cdn.example/x')).toBe('//cdn.example/x')
    expect(mod.appHref('#frag')).toBe('#frag')
    expect(mod.appHref('api/calendars/x.ics')).toBe('api/calendars/x.ics')
  })
})

describe('in-app links keep the ingress prefix (source guard)', () => {
  // A root-relative ``href="/spaces/…"`` resolves against the origin,
  // not ``<base href>``: under HA ingress a middle-click / new tab /
  // copied link escapes to HA Core. Every in-app href goes through
  // ``addBase`` / ``appHref``.
  const sources = import.meta.glob(['./**/*.tsx', '!./**/*.test.tsx'], {
    query: '?raw', import: 'default', eager: true,
  }) as Record<string, string>

  it('finds the SPA sources', () => {
    expect(Object.keys(sources).length).toBeGreaterThan(50)
  })

  it('no JSX href is a bare root-relative path that skips the ingress prefix', () => {
    const bare = /href=(?:"\/(?!\/)|\{\s*[`'"]\/(?!\/))/
    const offenders = Object.entries(sources).flatMap(([file, src]) =>
      src.split('\n')
        .map((line, i) => [line, i + 1] as const)
        .filter(([line]) => bare.test(line))
        .map(([line, n]) => `${file}:${n}: ${line.trim()}`),
    )
    expect(offenders).toEqual([])
  })
})
