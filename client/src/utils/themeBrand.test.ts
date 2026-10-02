import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { BRAND_ACCENT, BRAND_PRIMARY } from './themeBrand'

describe('brand palette', () => {
  it('matches the light tokens in tokens.css', () => {
    const css = readFileSync(join(__dirname, '..', 'styles', 'tokens.css'), 'utf8')
    expect(css).toMatch(new RegExp(`--sh-primary:\\s*${BRAND_PRIMARY};`, 'i'))
    expect(css).toMatch(new RegExp(`--sh-warning:\\s*${BRAND_ACCENT};`, 'i'))
  })
})
