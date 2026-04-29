import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/preact'

beforeEach(() => {
  vi.resetModules()
  // LocationMap pulls in leaflet at module load; mock it out so the
  // test stays a pure render check.
  vi.doMock('./LocationMap', () => ({
    LocationMap: ({ markers, height }: any) => (
      <div data-testid="map" data-marker-count={markers.length} data-height={height} />
    ),
  }))
})

describe('LocationPostCard', () => {
  it('renders the label, the 4dp coords, and an OSM open link', async () => {
    const { LocationPostCard } = await import('./LocationPostCard')
    const { getByText, container } = render(
      <LocationPostCard location={{ lat: 52.52, lon: 4.06, label: 'Marina' }} />,
    )
    expect(getByText('📍 Marina')).toBeTruthy()
    expect(getByText(/52\.5200, 4\.0600/)).toBeTruthy()
    const link = container.querySelector('a[href*="openstreetmap.org"]') as HTMLAnchorElement
    expect(link).toBeTruthy()
    expect(link.target).toBe('_blank')
    expect(link.href).toContain('mlat=52.52')
    expect(link.href).toContain('mlon=4.06')
  })

  it('falls back to coord-only label when none was set', async () => {
    const { LocationPostCard } = await import('./LocationPostCard')
    const { getByText } = render(
      <LocationPostCard location={{ lat: 10.0, lon: 20.0 }} />,
    )
    expect(getByText('📍 Shared location')).toBeTruthy()
  })

  it('passes a single marker to LocationMap at feed-card height', async () => {
    const { LocationPostCard } = await import('./LocationPostCard')
    const { getByTestId } = render(
      <LocationPostCard location={{ lat: 1, lon: 2 }} />,
    )
    const map = getByTestId('map')
    expect(map.getAttribute('data-marker-count')).toBe('1')
    expect(map.getAttribute('data-height')).toBe('160')
  })
})
