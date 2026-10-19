import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/preact'

const seen = vi.hoisted(() => ({ props: null as Record<string, unknown> | null }))
vi.mock('./LocationMap', () => ({
  LocationMap: (p: Record<string, unknown>) => { seen.props = p; return <div data-testid="map" /> },
}))

import { LocalAreaMap } from './LocalAreaMap'

describe('LocalAreaMap', () => {
  it('draws the circle as a zone and fits the whole of it', () => {
    render(<LocalAreaMap area={{ lat: 47.3769, lon: 8.5417, radius_km: 5 }} />)
    const p = seen.props!
    const zones = p.zones as Array<{ latitude: number; longitude: number; radius_m: number }>
    expect(zones).toHaveLength(1)
    expect(zones[0]).toMatchObject({ latitude: 47.3769, longitude: 8.5417, radius_m: 5000 })
    expect(p.selectedId).toBe('zone:local-area')
    expect(p.zoomToSelection).toBe(true)
    expect(p.areaFit).toBe(true)
    // A picture: no panning, no "map empty" pane.
    expect(p.readOnly).toBe(true)
    expect(p.emptyLabel).toBe('')
    expect(p.markers).toEqual([])
  })

  it('shows our home as a 🏠 pin, and picks when asked to', () => {
    const onPick = vi.fn()
    render(<LocalAreaMap area={null} home={{ lat: 1, lon: 2 }} onPick={onPick} />)
    const p = seen.props!
    expect(p.zones).toEqual([])
    expect(p.selectedId).toBeNull()
    expect(p.readOnly).toBe(false)
    expect(p.onPick).toBe(onPick)
    expect(p.markers).toEqual([expect.objectContaining({ lat: 1, lon: 2, glyph: '🏠' })])
  })
})
