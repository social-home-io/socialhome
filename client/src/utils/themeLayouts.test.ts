import { describe, it, expect } from 'vitest'
import { DEFAULT_LAYOUT, LAYOUT_IDS, isLayoutId } from './themeLayouts'

describe('theme layout ids', () => {
  it('lists exactly the schema ids (CHECK post_layout IN card/compact/magazine)', () => {
    expect([...LAYOUT_IDS]).toEqual(['card', 'compact', 'magazine'])
    expect(DEFAULT_LAYOUT).toBe('card')
  })

  it('accepts only those ids — "spacious" was never a server layout', () => {
    for (const id of LAYOUT_IDS) expect(isLayoutId(id)).toBe(true)
    for (const bad of ['spacious', 'inherit', '', null, undefined, 3, 'toString']) {
      expect(isLayoutId(bad)).toBe(false)
    }
  })
})
