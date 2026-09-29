import { describe, it, expect } from 'vitest'
import { firstUrl, linkDomain, safeWebUrl } from './linkPreview'

describe('firstUrl', () => {
  it.each([
    ['see https://example.com/a?b=1 now', 'https://example.com/a?b=1'],
    ['end https://example.com/x.', 'https://example.com/x'],
    ['[label](https://example.com/p) more', 'https://example.com/p'],
    ['<https://example.com/q>', 'https://example.com/q'],
    ['two https://a.example then https://b.example', 'https://a.example'],
    ['no link', null],
    ['ftp://example.com/', null],
    ['', null],
  ])('%s → %s', (text, expected) => {
    expect(firstUrl(text)).toBe(expected)
  })

  it('handles null / undefined', () => {
    expect(firstUrl(null)).toBeNull()
    expect(firstUrl(undefined)).toBeNull()
  })
})

describe('safeWebUrl', () => {
  it('keeps plain web links', () => {
    expect(safeWebUrl('https://example.com/a')).toBe('https://example.com/a')
    expect(safeWebUrl('http://example.com')).toBe('http://example.com/')
  })

  it.each([
    'javascript:alert(1)',
    'data:text/html,<b>x</b>',
    'vbscript:msgbox',
    'file:///etc/passwd',
    'https://user:pw@example.com/',
    '/relative/path',
    'not a url',
    '',
  ])('refuses %s', (raw) => {
    expect(safeWebUrl(raw)).toBeNull()
  })

  it('handles null', () => {
    expect(safeWebUrl(null)).toBeNull()
  })
})

describe('linkDomain', () => {
  it('strips www. and returns the host', () => {
    expect(linkDomain('https://www.Example.com/a')).toBe('example.com')
    expect(linkDomain('https://news.example.org/x')).toBe('news.example.org')
  })

  it('is empty for unsafe links', () => {
    expect(linkDomain('javascript:alert(1)')).toBe('')
    expect(linkDomain(null)).toBe('')
  })
})
