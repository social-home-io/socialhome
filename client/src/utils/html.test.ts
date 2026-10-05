import { describe, it, expect } from 'vitest'
import { escapeHtml } from './html'

describe('escapeHtml', () => {
  it('escapes the five HTML-significant characters', () => {
    expect(escapeHtml(`<a href="x">'&'</a>`)).toBe(
      '&lt;a href=&quot;x&quot;&gt;&#39;&amp;&#39;&lt;/a&gt;',
    )
  })

  it('leaves plain text untouched', () => {
    expect(escapeHtml('Office · Zürich')).toBe('Office · Zürich')
  })

  it('round-trips through innerHTML as literal text', () => {
    const name = '<img src=x onerror="window.__xss=1"> & <b>Bold</b>'
    const el = document.createElement('div')
    el.innerHTML = `<strong>${escapeHtml(name)}</strong>`
    expect(el.querySelector('img')).toBeNull()
    expect(el.textContent).toBe(name)
  })

  it('cannot break out of a quoted attribute', () => {
    const el = document.createElement('div')
    el.innerHTML = `<a id="${escapeHtml('"><img src=x>')}">x</a>`
    expect(el.querySelector('img')).toBeNull()
    expect(el.querySelector('a')!.id).toBe('"><img src=x>')
  })
})
