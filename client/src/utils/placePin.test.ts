import { describe, expect, it } from 'vitest'
import type { DirectoryEntry } from '@/types'
import { distanceLabel, pinDistanceKm, sortByDistance } from './placePin'

const entry = (over: Partial<DirectoryEntry> = {}): DirectoryEntry => ({
  space_id: 's', host_instance_id: 'iid-far', host_display_name: 'iid-far', host_is_paired: false,
  name: 'S', description: null, emoji: null, member_count: 1, scope: 'global',
  join_mode: 'request', min_age: 0, ...over,
})
const HOME = { lat: 47.37, lon: 8.563 } // Zürich-Hottingen

describe('pinDistanceKm', () => {
  it('is computed against our home, and unknown without a pin or a home', () => {
    const bern = entry({ lat: 46.9481, lon: 7.4474 })
    expect(pinDistanceKm(bern, HOME)!).toBeGreaterThan(90)
    expect(pinDistanceKm(bern, HOME)!).toBeLessThan(100)
    expect(pinDistanceKm(entry(), HOME)).toBeNull()
    expect(pinDistanceKm(bern, null)).toBeNull()
    expect(pinDistanceKm(bern, undefined)).toBeNull()
  })
})

describe('sortByDistance', () => {
  it('nearest pins first, unpinned after in their order', () => {
    const rows = [
      entry({ space_id: 'plain1' }),
      entry({ space_id: 'bern', lat: 46.9481, lon: 7.4474 }),
      entry({ space_id: 'plain2' }),
      entry({ space_id: 'zurich', lat: 47.3769, lon: 8.5417 }),
    ]
    expect(sortByDistance(rows, HOME).map((e) => e.space_id))
      .toEqual(['zurich', 'bern', 'plain1', 'plain2'])
  })
  it('without a home the order is kept', () => {
    const rows = [entry({ space_id: 'a', lat: 1, lon: 1 }), entry({ space_id: 'b' })]
    expect(sortByDistance(rows, null).map((e) => e.space_id)).toEqual(['a', 'b'])
  })
})

describe('distanceLabel', () => {
  it('rounds — a hint, never a precise distance', () => {
    expect(distanceLabel(0.4)).toBe('less than 1 km away')
    expect(distanceLabel(2.6)).toBe('about 3 km away')
    expect(distanceLabel(93.2)).toBe('about 95 km away')
  })
})
