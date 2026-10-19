import { describe, it, expect } from 'vitest'
import {
  areaLabel, areaShortLabel, areaVerdict, distanceKm, radiusInBounds, radiusLabel, round4,
} from './localArea'

const ZURICH = { lat: 47.3769, lon: 8.5417, radius_km: 5 }
const NEAR = { lat: 47.392, lon: 8.56 } // ~2 km
const BERN = { lat: 46.9481, lon: 7.4474 } // ~95 km

describe('areaVerdict', () => {
  it('inside the circle is "inside"', () => {
    expect(areaVerdict(ZURICH, NEAR)).toBe('inside')
  })

  it('outside the circle is "outside"', () => {
    expect(areaVerdict(ZURICH, BERN)).toBe('outside')
  })

  it('the rim counts as inside, like the server', () => {
    const area = { lat: 0, lon: 0, radius_km: 1 }
    expect(areaVerdict(area, { lat: 0, lon: 0.0089 })).toBe('inside')
    expect(areaVerdict(area, { lat: 0, lon: 0.0091 })).toBe('outside')
  })

  it('no home set → "no_home"; still loading → "unknown"', () => {
    expect(areaVerdict(ZURICH, null)).toBe('no_home')
    expect(areaVerdict(ZURICH, undefined)).toBe('unknown')
  })

  it('inside but not shared with the host → "not_shared"', () => {
    expect(areaVerdict(ZURICH, NEAR, false)).toBe('not_shared')
  })

  it('outside wins over "not shared": no pointless errand', () => {
    expect(areaVerdict(ZURICH, BERN, false)).toBe('outside')
  })
})

describe('labels', () => {
  it('says the radius and the exact centre', () => {
    expect(areaLabel(ZURICH)).toBe('Within 5 km of 47.3769° N, 8.5417° E')
    expect(areaShortLabel({ ...ZURICH, radius_km: 0.5 })).toBe('Within 0.5 km')
    expect(radiusLabel(25)).toBe('25 km')
  })
})

describe('helpers', () => {
  it('rounds to the 4 dp the server stores', () => {
    expect(round4(47.376_912_3)).toBe(47.3769)
    expect(round4(-8.541_75)).toBe(-8.5417)
  })

  it('measures with the haversine', () => {
    expect(distanceKm(ZURICH, BERN)).toBeGreaterThan(90)
    expect(distanceKm(ZURICH, BERN)).toBeLessThan(100)
  })

  it('knows the server bounds', () => {
    expect(radiusInBounds(0.5)).toBe(true)
    expect(radiusInBounds(50)).toBe(true)
    expect(radiusInBounds(0.4)).toBe(false)
    expect(radiusInBounds(51)).toBe(false)
    expect(radiusInBounds(Number.NaN)).toBe(false)
  })
})
