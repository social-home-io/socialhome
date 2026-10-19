import { describe, it, expect } from 'vitest'
import {
  areaLabel, areaShortLabel, areaVerdict, coarsePoint, distanceKm, JOIN_PRECISION_DP, joinVerdict,
  peerAreaLabel, radiusInBounds, radiusLabel, round4,
} from './localArea'

const ZURICH = { lat: 47.3769, lon: 8.5417, radius_km: 5 }
const NEAR = { lat: 47.392, lon: 8.56 } // ~2 km
const BERN = { lat: 46.9481, lon: 7.4474 } // ~95 km

/** ``km`` east of ZURICH on its parallel. */
function eastKm(km: number) {
  const perDeg = 111.19492664455873 * Math.cos((ZURICH.lat * Math.PI) / 180)
  return { lat: ZURICH.lat, lon: ZURICH.lon + km / perDeg }
}

describe('areaVerdict (our own local community — the server\'s rule)', () => {
  it('inside the circle is "inside"', () => {
    expect(areaVerdict(ZURICH, NEAR)).toBe('inside')
  })

  it('outside the circle is "outside"', () => {
    expect(areaVerdict(ZURICH, BERN)).toBe('outside')
  })

  it('a ~1 km cell straddling the rim is "edge" — an admin decides', () => {
    expect(areaVerdict(ZURICH, eastKm(5))).toBe('edge')
    expect(areaVerdict(ZURICH, eastKm(5.3))).toBe('edge')
    expect(areaVerdict(ZURICH, eastKm(2))).toBe('inside')
    expect(areaVerdict(ZURICH, eastKm(8))).toBe('outside')
  })

  it('no home set → "no_home"; still loading → "unknown"', () => {
    expect(areaVerdict(ZURICH, null)).toBe('no_home')
    expect(areaVerdict(ZURICH, undefined)).toBe('unknown')
  })
})

describe('the join\'s coarse point', () => {
  it('rounds to two decimals, like the server', () => {
    expect(JOIN_PRECISION_DP).toBe(2)
    expect(coarsePoint({ lat: 47.376_912, lon: 8.541_749 })).toEqual({ lat: 47.38, lon: 8.54 })
  })

  it('decides for the whole cell: every home in it gets the same answer', () => {
    const a = { lat: 47.3751, lon: 8.6049 }
    const b = { lat: 47.3849, lon: 8.5951 }
    expect(coarsePoint(a)).toEqual(coarsePoint(b))
    expect(joinVerdict(ZURICH, a)).toBe(joinVerdict(ZURICH, b))
  })

  it('inside means every home of the cell is inside; outside, none is', () => {
    for (let i = 0; i < 2000; i++) {
      const home = { lat: ZURICH.lat + (i % 40 - 20) * 0.004, lon: ZURICH.lon + (Math.floor(i / 40) - 25) * 0.005 }
      const v = joinVerdict(ZURICH, home)
      const inside = distanceKm(ZURICH, home) <= ZURICH.radius_km
      if (v === 'inside') expect(inside).toBe(true)
      if (v === 'outside') expect(inside).toBe(false)
    }
  })
})

describe('labels', () => {
  it('says the radius and the exact centre', () => {
    expect(areaLabel(ZURICH)).toBe('Within 5 km of 47.3769° N, 8.5417° E')
    expect(areaShortLabel(0.5)).toBe('Within 0.5 km')
    expect(peerAreaLabel(2)).toBe('Local community · within 2 km of the host’s chosen centre')
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
