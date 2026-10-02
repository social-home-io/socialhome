import { describe, it, expect } from 'vitest'
import { FONT_IDS, FONT_STACKS, fontOverride, fontStack, isFontId } from './themeFonts'

describe('theme font ids', () => {
  it('maps "system" to the app font token — it means "no override"', () => {
    expect(fontStack('system')).toBe('var(--sh-font-family)')
    expect(fontOverride('system')).toBeNull()
  })

  it('leads "rounded" with the bundled Nunito', () => {
    expect(FONT_STACKS.rounded.startsWith('"Nunito Variable"')).toBe(true)
  })

  it('maps every other schema id to a CSS stack and an override', () => {
    for (const id of ['serif', 'rounded', 'mono']) {
      expect(fontOverride(id)).toBe(FONT_STACKS[id as keyof typeof FONT_STACKS])
      expect(fontStack(id)).toBe(FONT_STACKS[id as keyof typeof FONT_STACKS])
      expect(fontStack(id)).toContain(',')
    }
  })

  it('maps anything else to null — a stored id is never a CSS value', () => {
    for (const bad of [null, undefined, '', 'Comic Sans', 'toString', '__proto__']) {
      expect(fontStack(bad)).toBeNull()
      expect(fontOverride(bad)).toBeNull()
    }
  })
})

describe('isFontId', () => {
  it('accepts exactly the schema ids, in picker order', () => {
    expect([...FONT_IDS]).toEqual(['system', 'serif', 'rounded', 'mono'])
    for (const id of FONT_IDS) expect(isFontId(id)).toBe(true)
  })

  it('rejects a CSS stack — the old studio sent those and the server 422d', () => {
    for (const bad of ['Inter, system-ui, sans-serif', 'System (default)', null, 1]) {
      expect(isFontId(bad)).toBe(false)
    }
  })
})
