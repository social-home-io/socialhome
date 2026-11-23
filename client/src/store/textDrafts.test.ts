import { describe, it, expect } from 'vitest'
import { clearTextDraft, textDraft } from './textDrafts'

describe('textDraft', () => {
  it('keeps one draft per key', () => {
    textDraft('t:a').text.value = 'for A'
    expect(textDraft('t:a').text.value).toBe('for A')
    expect(textDraft('t:b').text.value).toBe('')
  })

  it('returns the same signals for a key, so a re-render keeps the text', () => {
    expect(textDraft('t:same').text).toBe(textDraft('t:same').text)
    expect(textDraft('t:same').sending).toBe(textDraft('t:same').sending)
  })

  it('a send in flight is per key', () => {
    textDraft('t:busy').sending.value = true
    expect(textDraft('t:idle').sending.value).toBe(false)
  })

  it('clearing empties the text; the field keeps writing to the same draft', () => {
    const d = textDraft('t:sent')
    d.text.value = 'sent'
    clearTextDraft('t:sent')
    expect(d.text.value).toBe('')
    expect(textDraft('t:sent')).toBe(d)
  })

  it('clearing an unknown key does nothing', () => {
    expect(() => clearTextDraft('t:never')).not.toThrow()
  })
})
