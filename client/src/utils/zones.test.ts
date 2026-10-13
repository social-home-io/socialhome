import { describe, it, expect } from 'vitest'
import en from '@/i18n/locales/en.json'
import { ZONE_ICON_PRESETS, sortZones } from './zones'

describe('sortZones', () => {
  it('orders by name, ignoring case and accents, like Home Assistant', () => {
    const zones = [
      { id: '1', name: 'school' },
      { id: '2', name: 'Älmhult' },
      { id: '3', name: 'Work' },
      { id: '4', name: 'gym' },
      { id: '5', name: 'Home' },
    ]
    expect(sortZones(zones).map(z => z.name))
      .toEqual(['Älmhult', 'gym', 'Home', 'school', 'Work'])
  })

  it('returns a new array and leaves the input untouched', () => {
    const zones = [{ name: 'B' }, { name: 'A' }]
    const out = sortZones(zones)
    expect(out).not.toBe(zones)
    expect(zones.map(z => z.name)).toEqual(['B', 'A'])
  })
})

describe('ZONE_ICON_PRESETS', () => {
  it('carries the twelve Home Assistant–style places', () => {
    expect(ZONE_ICON_PRESETS.map(p => p.emoji)).toEqual([
      '🏠', '🏢', '🏫', '🛒', '🏋️', '🌳', '🏥', '⚽', '🎵', '🍽️', '✈️', '🚉',
    ])
  })

  it('labels every preset with an existing zones.icon.* key', () => {
    const keys = new Set(ZONE_ICON_PRESETS.map(p => p.key))
    expect(keys.size).toBe(ZONE_ICON_PRESETS.length)
    for (const p of ZONE_ICON_PRESETS) {
      expect(p.label).toBe(`zones.icon.${p.key}`)
      expect((en as Record<string, string>)[p.label]).toBeTruthy()
    }
  })

  it('only uses marks the server accepts (no tag text, no stacked marks)', () => {
    // Mirrors socialhome/domain/emoji.py: the only combining marks are
    // the variation selectors and the keycap mark; no tag characters.
    for (const p of ZONE_ICON_PRESETS) {
      for (const ch of p.emoji) {
        const cp = ch.codePointAt(0)!
        expect(cp >= 0xE0000 && cp <= 0xE007F).toBe(false)
        if (/\p{M}/u.test(ch)) expect([0xFE0E, 0xFE0F, 0x20E3]).toContain(cp)
      }
    }
  })
})
