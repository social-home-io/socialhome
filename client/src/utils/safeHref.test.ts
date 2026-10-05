import { describe, it, expect } from 'vitest'
import { safeHref } from './safeHref'

describe('safeHref', () => {
  it.each([
    ['https://example.com/a', 'https://example.com/a'],
    ['http://example.com', 'http://example.com/'],
    ['  https://example.com/x  ', 'https://example.com/x'],
    ['/spaces/abc', '/spaces/abc'],
    ['api/media/f1.pdf?exp=1&sig=x', 'api/media/f1.pdf?exp=1&sig=x'],
    ['mailto:a@b.co', 'mailto:a@b.co'],
  ])('keeps %s', (raw, want) => {
    expect(safeHref(raw)).toBe(want)
  })

  it.each([
    ['javascript:alert(1)'],
    ['JavaScript:alert(1)'],
    [' javascript:alert(1)'],
    ['java\tscript:alert(1)'],
    ['java\nscript:alert(1)'],
    ['\u0001javascript:alert(1)'],
    ['data:text/html,<script>alert(1)</script>'],
    ['vbscript:msgbox(1)'],
    ['file:///etc/passwd'],
    ['blob:https://example.com/uuid'],
    ['https://user:pw@example.com/'],
    ['other/relative/path'],
    ['../api/media/x'],
    ['//evil.example/x'],
    ['/\\evil.example/x'],
    [''],
    ['   '],
  ])('rejects %j', (raw) => {
    expect(safeHref(raw)).toBeUndefined()
  })

  it('rejects non-strings', () => {
    expect(safeHref(null)).toBeUndefined()
    expect(safeHref(undefined)).toBeUndefined()
    expect(safeHref(42 as unknown as string)).toBeUndefined()
  })

  it('admits blob: only when the caller opts in', () => {
    const b = 'blob:https://example.com/0f0e'
    expect(safeHref(b, { blob: true })).toBe(b)
    expect(safeHref('javascript:alert(1)', { blob: true })).toBeUndefined()
  })
})
