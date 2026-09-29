import { describe, it, expect } from 'vitest'
import {
  DM_LOCATION_LABEL_MAX,
  formatCoords,
  mapsHref,
  parseDmLocation,
  toDmLocationContent,
} from './dmLocation'

describe('parseDmLocation', () => {
  it('parses the canonical server shape', () => {
    expect(
      parseDmLocation('{"lat":52.3702,"lon":4.8952,"label":"Marina","accuracy_m":50}'),
    ).toEqual({ lat: 52.3702, lon: 4.8952, label: 'Marina', accuracy_m: 50 })
  })

  it('rounds anything finer than 4 dp (an untrusted peer)', () => {
    const loc = parseDmLocation('{"lat":52.370216789,"lon":4.895167912}')
    expect(loc).toEqual({ lat: 52.3702, lon: 4.8952, label: null, accuracy_m: null })
  })

  it.each([
    '',
    'meet at the station',
    '[1,2]',
    'null',
    '{"lat":"52","lon":4}',
    '{"lat":91,"lon":4}',
    '{"lat":1,"lon":-181}',
    '{"lon":4}',
  ])('returns null for malformed content %s', (content) => {
    expect(parseDmLocation(content)).toBeNull()
  })

  it('drops a blank label and caps a long one', () => {
    expect(parseDmLocation('{"lat":1,"lon":2,"label":"   "}')?.label).toBeNull()
    const long = 'x'.repeat(200)
    expect(parseDmLocation(`{"lat":1,"lon":2,"label":"${long}"}`)?.label?.length)
      .toBe(DM_LOCATION_LABEL_MAX)
  })
})

describe('toDmLocationContent', () => {
  it('rounds to 4 dp and trims the label', () => {
    expect(JSON.parse(toDmLocationContent({
      lat: 52.370216789, lon: 4.895167912, label: '  Dam ', accuracy_m: 12.6,
    }))).toEqual({ lat: 52.3702, lon: 4.8952, label: 'Dam', accuracy_m: 13 })
  })

  it('sends null for an absent label / accuracy', () => {
    expect(JSON.parse(toDmLocationContent({ lat: 1, lon: 2 })))
      .toEqual({ lat: 1, lon: 2, label: null, accuracy_m: null })
  })
})

describe('mapsHref', () => {
  it('builds an OSM link from the two numbers only', () => {
    expect(mapsHref({ lat: 52.3702, lon: 4.8952 })).toBe(
      'https://www.openstreetmap.org/?mlat=52.3702&mlon=4.8952#map=16/52.3702/4.8952',
    )
  })

  it('refuses a non-finite or out-of-range pin', () => {
    expect(mapsHref({ lat: Number.NaN, lon: 1 })).toBeNull()
    expect(mapsHref({ lat: 1, lon: 200 })).toBeNull()
  })
})

describe('formatCoords', () => {
  it('always shows 4 decimals', () => {
    expect(formatCoords({ lat: 1, lon: -2.5 })).toBe('1.0000, -2.5000')
  })
})
