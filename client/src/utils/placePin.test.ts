import { describe, expect, it, vi } from 'vitest'
import type { DirectoryEntry } from '@/types'
import { distanceLabel, hasHint, hintLabel, hintVerdict, nearMe, pinDistanceKm, sortByDistance } from './placePin'

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

describe('the radius hint ("best for people within 10 km")', () => {
  const zurich = { lat: 47.3769, lon: 8.5417 }
  it('says "within the area" when our home is inside the suggested radius', () => {
    const v = hintVerdict(entry({ ...zurich, radius_km: 10 }), HOME)!
    expect(v.kind).toBe('inside')
    expect(hintLabel(v)).toBe('Within the area')
  })
  it('says how far, and the suggestion, when outside', () => {
    const v = hintVerdict(entry({ lat: 46.9481, lon: 7.4474, radius_km: 10 }), HOME)!
    expect(v.kind).toBe('outside')
    expect(hintLabel(v)).toBe('about 95 km away · outside the suggested 10 km')
  })
  it('nothing without a hint, a pin or our home', () => {
    expect(hintVerdict(entry(zurich), HOME)).toBeNull()
    expect(hintVerdict(entry({ radius_km: 10 }), HOME)).toBeNull()
    expect(hintVerdict(entry({ ...zurich, radius_km: 10 }), null)).toBeNull()
    expect(hintVerdict(entry({ ...zurich, radius_km: 10 }), undefined)).toBeNull()
  })
  it('"Near me" keeps only the spaces whose suggested area our home is in', () => {
    const rows = [
      entry({ space_id: 'in', ...zurich, radius_km: 10 }),
      entry({ space_id: 'out', lat: 46.9481, lon: 7.4474, radius_km: 10 }),
      entry({ space_id: 'pinned', ...zurich }),
      entry({ space_id: 'plain' }),
    ]
    expect(nearMe(rows, HOME).map((e) => e.space_id)).toEqual(['in'])
    expect(hasHint(rows)).toBe(true)
    expect(hasHint([entry(zurich)])).toBe(false)
  })
  it('is worked out here: nothing about our home leaves the browser', () => {
    // Pure functions over the directory rows we already have — no fetch.
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    hintVerdict(entry({ ...zurich, radius_km: 10 }), HOME)
    nearMe([entry({ ...zurich, radius_km: 10 })], HOME)
    expect(fetchSpy).not.toHaveBeenCalled()
    fetchSpy.mockRestore()
  })
})
