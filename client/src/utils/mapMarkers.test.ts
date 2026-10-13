import { describe, it, expect } from 'vitest'
import {
  MERGE_DISTANCE_PX,
  bubbleSlots,
  groupBounds,
  zoneBounds,
  groupByZone,
  matchZone,
  mergeOverlapping,
  personColorToken,
  type MapPerson,
  type MarkerGroup,
  type ZoneLike,
} from './mapMarkers'
import { TOKEN_COLORS, hashColor } from './tokenColor'

const home: ZoneLike = { id: 'z-home', latitude: 47.0, longitude: 8.0, radius_m: 200 }
const work: ZoneLike = { id: 'z-work', latitude: 47.1, longitude: 8.1, radius_m: 300 }

function person(id: string, lat: number | null, lon: number | null, zone_id?: string | null): MapPerson {
  return { id, lat, lon, zone_id }
}

describe('personColorToken', () => {
  it('is a theme token from the shared palette, stable per person', () => {
    const a = personColorToken('uid-anna')
    expect(TOKEN_COLORS).toContain(a)
    expect(personColorToken('uid-anna')).toBe(a)
    expect(a).toBe(hashColor('uid-anna'))
  })

  it('spreads different people over more than one colour', () => {
    const tokens = new Set(['a', 'b', 'c', 'd', 'e', 'f', 'g'].map(personColorToken))
    expect(tokens.size).toBeGreaterThan(1)
  })
})

describe('matchZone', () => {
  it('returns the zone whose radius contains the point', () => {
    expect(matchZone([home, work], 47.0005, 8.0005)?.id).toBe('z-home')
  })

  it('returns null outside every zone', () => {
    expect(matchZone([home, work], 46.5, 7.5)).toBeNull()
  })

  it('picks the nearest centre when zones overlap', () => {
    const big: ZoneLike = { id: 'z-big', latitude: 47.0, longitude: 8.0, radius_m: 50_000 }
    const near: ZoneLike = { id: 'z-near', latitude: 47.1, longitude: 8.1, radius_m: 1_000 }
    expect(matchZone([big, near], 47.1001, 8.1001)?.id).toBe('z-near')
  })

  it('treats the radius as inclusive (haversine metres)', () => {
    // ~111 m north of the centre: inside a 120 m zone, outside a 100 m one.
    const z = { id: 'z', latitude: 0, longitude: 0, radius_m: 120 }
    expect(matchZone([z], 0.001, 0)?.id).toBe('z')
    expect(matchZone([{ ...z, radius_m: 100 }], 0.001, 0)).toBeNull()
  })
})

describe('groupByZone', () => {
  it('folds people in the same zone into one group at the zone centre', () => {
    const groups = groupByZone(
      [person('a', 47.0001, 8.0001), person('b', 46.9999, 7.9999), person('c', 45, 7)],
      [home, work],
    )
    expect(groups).toHaveLength(2)
    const zoned = groups.find((g) => g.zoneIds.includes('z-home'))!
    expect(zoned.members.map((m) => m.id)).toEqual(['a', 'b'])
    expect([zoned.lat, zoned.lon]).toEqual([47.0, 8.0])
    expect(zoned.points).toEqual([])
    expect(zoned.key).toBe('zone:z-home')
    const solo = groups.find((g) => g.key === 'person:c')!
    expect(solo.members.map((m) => m.id)).toEqual(['c'])
    expect([solo.lat, solo.lon]).toEqual([45, 7])
    expect(solo.points).toEqual([[45, 7]])
    expect(solo.zoneIds).toEqual([])
  })

  it('places zone-only people (no coordinates) at their zone centre', () => {
    const groups = groupByZone(
      [person('a', null, null, 'z-work'), person('b', null, null, 'z-work')],
      [home, work],
    )
    expect(groups).toHaveLength(1)
    expect(groups[0].key).toBe('zone:z-work')
    expect([groups[0].lat, groups[0].lon]).toEqual([47.1, 8.1])
    expect(groups[0].members.map((m) => m.id)).toEqual(['a', 'b'])
  })

  it('drops people with neither a position nor a known zone', () => {
    expect(groupByZone([person('a', null, null, 'z-gone'), person('b', null, null)], [home]))
      .toEqual([])
  })

  it('does not merge solos that happen to share a point', () => {
    const groups = groupByZone([person('a', 1, 2), person('b', 1, 2)], [])
    expect(groups.map((g) => g.key)).toEqual(['person:a', 'person:b'])
  })
})

describe('mergeOverlapping', () => {
  // A flat projection: 1 degree == 100 px.
  const project = (lat: number, lon: number) => ({ x: lon * 100, y: -lat * 100 })

  function solo(id: string, lat: number, lon: number): MarkerGroup<MapPerson> {
    return groupByZone([person(id, lat, lon)], [])[0]
  }

  it('exports a sensible default distance', () => {
    expect(MERGE_DISTANCE_PX).toBeGreaterThanOrEqual(40)
    expect(MERGE_DISTANCE_PX).toBeLessThanOrEqual(80)
  })

  it('leaves groups that are far apart alone', () => {
    const groups = [solo('a', 0, 0), solo('b', 0, 1)]
    const out = mergeOverlapping(groups, project, 56)
    expect(out).toHaveLength(2)
    expect(out.every((g) => !g.merged)).toBe(true)
  })

  it('merges groups closer than the threshold into one at their centroid', () => {
    const zoned = groupByZone([person('z1', null, null, 'z-home')], [home])[0]
    const near = solo('b', 47.2, 8.2) // 20 px / 20 px away
    const out = mergeOverlapping([zoned, near], project, 56)
    expect(out).toHaveLength(1)
    const g = out[0]
    expect(g.merged).toBe(true)
    expect(g.members.map((m) => m.id)).toEqual(['z1', 'b'])
    expect(g.zoneIds).toEqual(['z-home'])
    expect(g.points).toEqual([[47.2, 8.2]])
    expect(g.lat).toBeCloseTo(47.1)
    expect(g.lon).toBeCloseTo(8.1)
  })

  it('chains merges until no two groups overlap', () => {
    // a-b 40 px apart, b-c 40 px: a+b merge (centroid at 20 px), and c
    // sits 60 px from that centroid — still inside 70 px, so it folds in.
    const groups = [solo('a', 0, 0), solo('b', 0, 0.4), solo('c', 0, 0.8)]
    const out = mergeOverlapping(groups, project, 70)
    expect(out).toHaveLength(1)
    expect(out[0].members.map((m) => m.id).sort()).toEqual(['a', 'b', 'c'])
  })

  it('keeps a stable key for the same merged set', () => {
    const a = mergeOverlapping([solo('a', 0, 0), solo('b', 0, 0.1)], project, 56)
    const b = mergeOverlapping([solo('a', 0, 0), solo('b', 0, 0.1)], project, 56)
    expect(a[0].key).toBe(b[0].key)
    expect(a[0].key).not.toBe('person:a')
  })
})

describe('bubbleSlots', () => {
  const ppl = (n: number) => Array.from({ length: n }, (_, i) => person(`p${i}`, 0, 0))

  it('shows everyone up to three', () => {
    expect(bubbleSlots(ppl(1))).toEqual({ shown: ppl(1), overflow: 0 })
    expect(bubbleSlots(ppl(3))).toEqual({ shown: ppl(3), overflow: 0 })
  })

  it('caps at three avatars plus an overflow count', () => {
    const { shown, overflow } = bubbleSlots(ppl(5))
    expect(shown.map((p) => p.id)).toEqual(['p0', 'p1', 'p2'])
    expect(overflow).toBe(2)
  })
})

describe('groupBounds', () => {
  it('covers a zone member by the whole zone circle and a solo by its point', () => {
    const zoned = groupByZone([person('a', null, null, 'z-home')], [home])[0]
    const solo = groupByZone([person('b', 46.9, 7.9)], [])[0]
    const merged = mergeOverlapping([zoned, solo], () => ({ x: 0, y: 0 }), 56)[0]
    const [[s, w], [n, e]] = groupBounds(merged, [home])
    // 200 m ≈ 0.0018° of latitude.
    expect(n).toBeCloseTo(47.0018, 3)
    expect(s).toBeCloseTo(46.9, 6)
    expect(w).toBeCloseTo(7.9, 6)
    expect(e).toBeGreaterThan(8.0)
    expect(e).toBeLessThan(8.01)
  })
})

describe('zoneBounds', () => {
  it('is the ±radius box around the zone centre', () => {
    const [[s, w], [n, e]] = zoneBounds(home)
    expect(n - 47).toBeCloseTo(200 / 111_320, 6)
    expect(47 - s).toBeCloseTo(200 / 111_320, 6)
    expect(e - 8).toBeGreaterThan(200 / 111_320) // longitude degrees are shorter
    expect(8 - w).toBeCloseTo(e - 8, 9)
  })
})

describe('mergeOverlapping with bubble extents', () => {
  const project = (lat: number, lon: number) => ({ x: lon * 100, y: -lat * 100 })
  // Boxes of 52 px; a zone bubble floats 60 px above its anchor (it clears
  // the zone marker), a solo bubble only 12 px.
  const extent = (g: MarkerGroup<MapPerson>) => ({
    w: 52, h: 52, lift: g.zoneIds.length > 0 && !g.merged ? 60 : 12,
  })
  const zoned = () => groupByZone([person('a', null, null, 'z-home')], [home])[0]

  it('merges a solo bubble that a lifted zone bubble would cover', () => {
    // Solo anchor 80 px above the zone centre: further apart than the
    // anchor threshold, but its bubble sits right on the zone bubble.
    const solo = groupByZone([person('b', 47.8, 8.0)], [])[0]
    expect(mergeOverlapping([zoned(), solo], project, 56)).toHaveLength(2)
    expect(mergeOverlapping([zoned(), solo], project, 4, extent)).toHaveLength(1)
  })

  it('keeps bubbles apart when their boxes do not touch', () => {
    const solo = groupByZone([person('b', 47.0, 9.0)], [])[0] // 100 px to the side
    expect(mergeOverlapping([zoned(), solo], project, 4, extent)).toHaveLength(2)
  })

  it('keeps close anchors apart when their bubbles are lifted clear of each other', () => {
    // Solo 30 px below the zone centre: anchor distance says merge, but
    // the zone bubble floats 26 px clear above the solo bubble.
    const solo = groupByZone([person('b', 46.7, 8.0)], [])[0]
    expect(mergeOverlapping([zoned(), solo], project, 56)).toHaveLength(1)
    expect(mergeOverlapping([zoned(), solo], project, 4, extent)).toHaveLength(2)
  })
})
