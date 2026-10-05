import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
import { ZoneLegend } from './ZoneLegend'
import type { SpaceZone } from '@/types'

const zone = (over: Partial<SpaceZone> = {}): SpaceZone => ({
  id: 'z1',
  space_id: 'sp1',
  name: 'Home',
  latitude: 47.3769,
  longitude: 8.5417,
  radius_m: 150,
  color: '#2E7D32',
  created_by: 'uid-admin',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  ...over,
})

describe('ZoneLegend', () => {
  it('renders one row per zone with name + formatted radius + coords', () => {
    const { container, getByText } = render(
      <ZoneLegend
        zones={[
          zone({ id: 'a', name: 'Home', radius_m: 150, latitude: 47.3769, longitude: 8.5417 }),
          zone({ id: 'b', name: 'Office', radius_m: 1500, latitude: 47.3801, longitude: 8.5410 }),
          zone({ id: 'c', name: 'Long range', radius_m: 12000, latitude: 47.0000, longitude: 8.0000 }),
        ]}
      />,
    )
    const rows = container.querySelectorAll('.sh-zone-legend__row')
    expect(rows.length).toBe(3)
    expect(getByText('Home')).toBeTruthy()
    expect(getByText('150 m')).toBeTruthy()
    expect(getByText('1.5 km')).toBeTruthy()
    expect(getByText('12 km')).toBeTruthy()
    // Coords land on their own row, monospace styled.
    expect(getByText('47.3769, 8.5417')).toBeTruthy()
    expect(getByText('47.3801, 8.5410')).toBeTruthy()
  })

  it('uses the zone color on the swatch when provided', () => {
    const { container } = render(
      <ZoneLegend zones={[zone({ color: '#F57C00' })]} />,
    )
    const swatch = container.querySelector('.sh-zone-legend__swatch') as HTMLElement
    expect(swatch).toBeTruthy()
    // jsdom normalises ``background: #F57C00`` to its rgb() form;
    // matching the hex via canonical channel values (245, 124, 0)
    // keeps the assertion stable regardless of serialisation.
    expect(swatch.style.background).toMatch(/rgb\(\s*245,\s*124,\s*0\s*\)/)
  })

  it('falls back to a stable palette color when color is null', () => {
    const { container } = render(
      <ZoneLegend zones={[zone({ id: 'fixed-id', color: null })]} />,
    )
    const swatch = container.querySelector('.sh-zone-legend__swatch') as HTMLElement
    // The hash of 'fixed-id' deterministically picks one of the palette
    // entries; we just need the rule to have fired with *some* colour
    // (not null/undefined) — jsdom serialises to rgb() so we check for
    // a valid value.
    expect(swatch.style.background).toMatch(/rgb\(\d+,\s*\d+,\s*\d+\)/)
  })

  it('renders nothing when zones is empty and no emptyLabel', () => {
    const { container } = render(<ZoneLegend zones={[]} />)
    expect(container.querySelector('.sh-zone-legend')).toBeNull()
  })

  it('renders the empty-state message when zones is empty and emptyLabel is set', () => {
    const { getByText } = render(
      <ZoneLegend zones={[]} emptyLabel="No zones configured yet." />,
    )
    expect(getByText('No zones configured yet.')).toBeTruthy()
  })

  it('never lets a hostile zone colour inject extra CSS into the swatch', () => {
    const { container } = render(
      <ZoneLegend zones={[zone({ color: 'red;position:fixed;inset:0;background-image:url(https://evil.example/b)' })]} />,
    )
    const sw = container.querySelector('.sh-zone-legend__swatch') as HTMLElement
    expect(sw.style.position).toBe('')
    expect(sw.style.backgroundImage).not.toContain('url')
    expect(sw.getAttribute('style') ?? '').not.toContain('evil')
    expect(sw.style.background).not.toBe('')
  })
})
