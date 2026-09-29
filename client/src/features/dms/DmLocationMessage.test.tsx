import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/preact'

vi.mock('@/components/LocationMap', () => ({
  LocationMap: ({ markers, height, ariaLabel }: any) => (
    <div
      data-testid="map"
      data-marker-count={markers.length}
      data-lat={markers[0]?.lat}
      data-accuracy={markers[0]?.accuracy_m ?? ''}
      data-height={height}
      aria-label={ariaLabel}
    />
  ),
}))

describe('DmLocationMessage', () => {
  it('renders the card with label, coords, accuracy and a maps link', async () => {
    const { DmLocationMessage } = await import('./DmLocationMessage')
    const { getByText, getByTestId, getByRole } = render(
      <DmLocationMessage
        content='{"lat":52.3702,"lon":4.8952,"label":"Dam square","accuracy_m":50}'
      />,
    )
    expect(getByText('📍 Dam square')).toBeTruthy()
    expect(getByText(/52\.3702, 4\.8952 · within ~50 m/)).toBeTruthy()
    const map = getByTestId('map')
    expect(map.getAttribute('data-marker-count')).toBe('1')
    expect(map.getAttribute('data-accuracy')).toBe('50')
    const link = getByRole('link', { name: /Open in maps/ }) as HTMLAnchorElement
    expect(link.href).toBe(
      'https://www.openstreetmap.org/?mlat=52.3702&mlon=4.8952#map=16/52.3702/4.8952',
    )
    expect(link.target).toBe('_blank')
    expect(link.rel).toContain('noopener')
  })

  it('never lets the label into the link', async () => {
    const { DmLocationMessage } = await import('./DmLocationMessage')
    const { getByRole } = render(
      <DmLocationMessage
        content='{"lat":1,"lon":2,"label":"javascript:alert(1)","accuracy_m":null}'
      />,
    )
    const link = getByRole('link', { name: /Open in maps/ }) as HTMLAnchorElement
    expect(link.href.startsWith('https://www.openstreetmap.org/')).toBe(true)
    expect(link.href).not.toContain('javascript')
  })

  it('falls back to "Shared location" without a label', async () => {
    const { DmLocationMessage } = await import('./DmLocationMessage')
    const { getByText } = render(
      <DmLocationMessage content='{"lat":1,"lon":2,"label":null,"accuracy_m":null}' />,
    )
    expect(getByText('📍 Shared location')).toBeTruthy()
  })

  it('renders a muted line, not raw JSON or a map, for malformed content', async () => {
    const { DmLocationMessage } = await import('./DmLocationMessage')
    const { container, queryByTestId } = render(
      <DmLocationMessage content='{"lat":500,"lon":2}' />,
    )
    expect(queryByTestId('map')).toBeNull()
    expect(container.textContent).toContain("couldn't be shown")
    expect(container.textContent).not.toContain('500')
  })
})
