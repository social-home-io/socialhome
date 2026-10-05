import { describe, it, expect } from 'vitest'
import { ZONE_PALETTE, safeZoneHex, zoneColor } from './zoneColor'

describe('safeZoneHex', () => {
  it('accepts #RRGGBB only', () => {
    expect(safeZoneHex('#3B82F6')).toBe('#3B82F6')
    for (const bad of [
      'red', '#fff', '3b82f6', '#3b82f6;position:fixed',
      'red;background:url(https://evil.example/beacon)',
      '#3b82f6\n', '', null, undefined, 42,
    ]) {
      expect(safeZoneHex(bad)).toBeNull()
    }
  })
})

describe('zoneColor', () => {
  it('keeps a valid colour', () => {
    expect(zoneColor({ id: 'z', color: '#10b981' })).toBe('#10b981')
  })

  it('falls back to a stable palette colour for null or hostile values', () => {
    const fallback = zoneColor({ id: 'z_abc', color: null })
    expect(ZONE_PALETTE).toContain(fallback)
    expect(zoneColor({ id: 'z_abc', color: 'red;background:url(x)' })).toBe(fallback)
    expect(zoneColor(undefined)).toBe(ZONE_PALETTE[0])
  })
})
