import { describe, it, expect } from 'vitest'
import { safeIconSrc } from './appIcon'

describe('safeIconSrc', () => {
  it('returns data:image URIs unchanged', () => {
    const d = 'data:image/svg+xml,%3Csvg%3E%3C/svg%3E'
    expect(safeIconSrc(d)).toBe(d)
    expect(safeIconSrc('DATA:image/png;base64,iVBORw0KGgo=')).toBe('DATA:image/png;base64,iVBORw0KGgo=')
  })
  it('rejects remote http(s) icons — no third-party fetch from a catalog or manifest', () => {
    // The SPA CSP has no ``https:`` in img-src; the placeholder renders.
    expect(safeIconSrc('https://example.com/icon.png')).toBeNull()
    expect(safeIconSrc('http://example.com/icon.png')).toBeNull()
    expect(safeIconSrc('//example.com/icon.png')).toBeNull()
  })
  it('rejects non-image data: URIs', () => {
    expect(safeIconSrc('data:text/html,<script>alert(1)</script>')).toBeNull()
  })
  it('rejects relative paths', () => {
    expect(safeIconSrc('icon.svg')).toBeNull()
    expect(safeIconSrc('/api/apps/chess/bundle/icon.svg')).toBeNull()
  })
  it('rejects empty / null / undefined', () => {
    expect(safeIconSrc(null)).toBeNull()
    expect(safeIconSrc(undefined)).toBeNull()
    expect(safeIconSrc('')).toBeNull()
  })
})
