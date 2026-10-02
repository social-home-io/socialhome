import { describe, it, expect } from 'vitest'
import { FONT_STACKS, fontStack } from './themeFonts'

describe('theme font ids', () => {
  it('maps every schema id to a CSS stack', () => {
    for (const id of ['system', 'serif', 'rounded', 'mono']) {
      expect(fontStack(id)).toBe(FONT_STACKS[id as keyof typeof FONT_STACKS])
      expect(fontStack(id)).toContain(',')
    }
  })

  it('maps anything else to null — a stored id is never a CSS value', () => {
    for (const bad of [null, undefined, '', 'Comic Sans', 'toString', '__proto__']) {
      expect(fontStack(bad)).toBeNull()
    }
  })
})
