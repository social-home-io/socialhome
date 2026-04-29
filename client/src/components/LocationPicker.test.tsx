import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

beforeEach(() => {
  vi.resetModules()
  vi.doMock('./LocationMap', () => ({
    LocationMap: ({ markers }: any) => (
      <div data-testid="map" data-marker-count={markers.length} />
    ),
  }))
  vi.doMock('./Toast', () => ({ showToast: vi.fn() }))
})

afterEach(() => {
  // Clean up the geolocation mock so tests stay isolated.
  delete (navigator as unknown as { geolocation?: unknown }).geolocation
})

function mockGeolocation(coords: { latitude: number; longitude: number }) {
  Object.defineProperty(navigator, 'geolocation', {
    configurable: true,
    value: {
      getCurrentPosition: (success: any) => success({ coords }),
    },
  })
}

describe('LocationPicker', () => {
  it('renders nothing when closed', async () => {
    const { LocationPicker } = await import('./LocationPicker')
    const { container } = render(
      <LocationPicker open={false} onSubmit={vi.fn()} onClose={vi.fn()} />,
    )
    expect(container.firstChild).toBeNull()
  })

  it('shows the "Use my current location" CTA when no pin yet', async () => {
    const { LocationPicker } = await import('./LocationPicker')
    const { getByText, queryByTestId } = render(
      <LocationPicker open onSubmit={vi.fn()} onClose={vi.fn()} />,
    )
    expect(getByText(/Use my current location/)).toBeTruthy()
    // Map is hidden until coords are picked.
    expect(queryByTestId('map')).toBeNull()
  })

  it('drops a marker after a successful geolocation prompt', async () => {
    mockGeolocation({ latitude: 52.5200123, longitude: 4.0600987 })
    const { LocationPicker } = await import('./LocationPicker')
    const { getByText, findByTestId } = render(
      <LocationPicker open onSubmit={vi.fn()} onClose={vi.fn()} />,
    )
    fireEvent.click(getByText(/Use my current location/))
    const map = await findByTestId('map')
    expect(map.getAttribute('data-marker-count')).toBe('1')
  })

  it('submits the draft with 4dp-rounded coords + the label', async () => {
    mockGeolocation({ latitude: 52.5200123, longitude: 4.0600987 })
    const onSubmit = vi.fn()
    const { LocationPicker } = await import('./LocationPicker')
    const { getByText, findByTestId, container } = render(
      <LocationPicker open onSubmit={onSubmit} onClose={vi.fn()} />,
    )
    fireEvent.click(getByText(/Use my current location/))
    await findByTestId('map')
    const labelInput = container.querySelector('input[type="text"]') as HTMLInputElement
    fireEvent.input(labelInput, { target: { value: 'Marina' } })
    fireEvent.click(getByText('Use this location'))
    expect(onSubmit).toHaveBeenCalledWith({
      lat: 52.5200,
      lon: 4.0601,
      label: 'Marina',
    })
  })
})
