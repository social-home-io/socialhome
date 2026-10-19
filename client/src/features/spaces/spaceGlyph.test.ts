import { describe, it, expect } from 'vitest'
import { spaceGlyph, tierIcon } from './spaceGlyph'

describe('spaceGlyph', () => {
  it('the emoji wins; else the first letter, never a globe or a house', () => {
    expect(spaceGlyph('🥕', 'Garden')).toBe('🥕')
    expect(spaceGlyph(null, 'Book club')).toBe('B')
    expect(spaceGlyph('', '  élternrat')).toBe('É')
    expect(spaceGlyph(null, '')).toBe('•')
    expect(spaceGlyph(undefined, undefined)).toBe('•')
  })

  it('takes a whole character, not half a surrogate pair', () => {
    expect(spaceGlyph(null, '𝒜lpha')).toBe('𝒜')
  })
})

describe('tierIcon', () => {
  it('marks each tier; nothing for an unknown one', () => {
    expect(['private', 'household', 'public', 'global'].map(tierIcon))
      .toEqual(['🔐', '🏠', '🤝', '🌐'])
    expect(tierIcon('weird')).toBeNull()
    expect(tierIcon(undefined)).toBeNull()
  })
})
