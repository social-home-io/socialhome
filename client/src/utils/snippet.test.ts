import { describe, it, expect } from 'vitest'
import { snippetParts, snippetText } from './snippet'

describe('snippet', () => {
  it('splits <mark> delimiters into highlighted runs', () => {
    expect(snippetParts('Am <mark>Strand</mark> mit Oma')).toEqual([
      { text: 'Am ', mark: false },
      { text: 'Strand', mark: true },
      { text: ' mit Oma', mark: false },
    ])
  })

  it('keeps anything else as literal text', () => {
    expect(snippetParts('<b>x</b>')).toEqual([{ text: '<b>x</b>', mark: false }])
    expect(snippetParts('')).toEqual([])
  })

  it('gives the plain text for a label', () => {
    expect(snippetText('Am <mark>Strand</mark> mit <mark>Oma</mark>')).toBe('Am Strand mit Oma')
  })
})
