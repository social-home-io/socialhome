import { describe, it, expect } from 'vitest'
import { cssUrl } from './cssUrl'

describe('cssUrl', () => {
  it('wraps a plain URL in a quoted url()', () => {
    expect(cssUrl('api/media/cover.webp')).toBe('url("api/media/cover.webp")')
  })

  it('keeps a hostile URL to a single background layer', () => {
    const el = document.createElement('div')
    // One quoted string token: the injected ``url(...)`` stays inside it.
    const oneLayer = /^url\("(?:[^"\\]|\\.)*"\)$/
    for (const u of [
      'x"), url("https://evil.example/beacon',
      'x), url(https://evil.example/beacon',
    ]) {
      el.style.backgroundImage = cssUrl(u)
      expect(el.style.backgroundImage).toMatch(oneLayer)
    }
  })

  it('escapes backslashes and line breaks', () => {
    expect(cssUrl('a\\b\nc')).toBe('url("a\\5c b\\a c")')
  })
})
