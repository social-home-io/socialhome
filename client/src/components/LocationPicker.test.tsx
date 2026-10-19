import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

/** Latest ``onPick`` the picker handed to the (mocked) map. */
let lastOnPick: ((lat: number, lon: number) => void) | undefined

beforeEach(() => {
  vi.resetModules()
  lastOnPick = undefined
  vi.doMock('./LocationMap', () => ({
    LocationMap: ({ markers, onPick }: any) => {
      lastOnPick = onPick
      return (
        <div
          data-testid="map"
          data-marker-count={markers.length}
          data-accuracy={markers[0]?.accuracy_m ?? ''}
        />
      )
    },
  }))
})

afterEach(() => {
  // Clean up the geolocation mock so tests stay isolated.
  delete (navigator as unknown as { geolocation?: unknown }).geolocation
})

type GetPos = (
  success: (p: unknown) => void,
  error: (e: unknown) => void,
  options?: PositionOptions,
) => void

function mockGeolocation(impl: GetPos) {
  const getCurrentPosition = vi.fn(impl)
  Object.defineProperty(navigator, 'geolocation', {
    configurable: true,
    value: { getCurrentPosition },
  })
  return getCurrentPosition
}

function mockPosition(coords: { latitude: number; longitude: number; accuracy?: number }) {
  return mockGeolocation((success) => success({ coords }))
}

function mockGeoError(code: number) {
  return mockGeolocation((_s, error) =>
    error({ code, PERMISSION_DENIED: 1, POSITION_UNAVAILABLE: 2, TIMEOUT: 3 }),
  )
}

describe('LocationPicker', () => {
  it('renders nothing when closed', async () => {
    const { LocationPicker } = await import('./LocationPicker')
    const { container } = render(
      <LocationPicker open={false} onSubmit={vi.fn()} onClose={vi.fn()} />,
    )
    expect(container.firstChild).toBeNull()
  })

  it('offers current location and map pick when no pin yet', async () => {
    const { LocationPicker } = await import('./LocationPicker')
    const { getByText, queryByTestId } = render(
      <LocationPicker open onSubmit={vi.fn()} onClose={vi.fn()} />,
    )
    expect(getByText(/Use my current location/)).toBeTruthy()
    expect(getByText(/Pick a spot on the map/)).toBeTruthy()
    // Map is hidden until the user chooses.
    expect(queryByTestId('map')).toBeNull()
  })

  it('never asks the browser for a high-accuracy fix', async () => {
    const getPos = mockPosition({ latitude: 1, longitude: 2 })
    const { LocationPicker } = await import('./LocationPicker')
    const { getByText } = render(
      <LocationPicker open onSubmit={vi.fn()} onClose={vi.fn()} />,
    )
    fireEvent.click(getByText(/Use my current location/))
    expect(getPos).toHaveBeenCalledTimes(1)
    const opts = getPos.mock.calls[0][2] as PositionOptions
    expect(opts.enableHighAccuracy).toBeUndefined()
  })

  it('drops a marker after a successful geolocation prompt', async () => {
    mockPosition({ latitude: 52.5200123, longitude: 4.0600987 })
    const { LocationPicker } = await import('./LocationPicker')
    const { getByText, findByTestId } = render(
      <LocationPicker open onSubmit={vi.fn()} onClose={vi.fn()} />,
    )
    fireEvent.click(getByText(/Use my current location/))
    const map = await findByTestId('map')
    expect(map.getAttribute('data-marker-count')).toBe('1')
    expect(getByText('52.5200, 4.0601')).toBeTruthy()
  })

  it('submits the draft with 4dp-rounded coords, the label and the accuracy', async () => {
    mockPosition({ latitude: 52.5200123, longitude: 4.0600987, accuracy: 18 })
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
      accuracy_m: 18,
    })
  })

  it('picks a spot on the map without asking for permission', async () => {
    const getPos = mockPosition({ latitude: 1, longitude: 2 })
    const onSubmit = vi.fn()
    const { LocationPicker } = await import('./LocationPicker')
    const { getByText, findByTestId, getByRole } = render(
      <LocationPicker open onSubmit={onSubmit} onClose={vi.fn()} submitLabel="Send location" />,
    )
    fireEvent.click(getByText(/Pick a spot on the map/))
    const map = await findByTestId('map')
    expect(map.getAttribute('data-marker-count')).toBe('0')
    expect(getByText(/Tap the map to drop the pin/)).toBeTruthy()
    const send = getByRole('button', { name: 'Send location' }) as HTMLButtonElement
    expect(send.disabled).toBe(true)
    lastOnPick!(48.858370123, 2.294481987)
    await findByTestId('map')
    expect(getByText('48.8584, 2.2945')).toBeTruthy()
    fireEvent.click(getByRole('button', { name: 'Send location' }))
    expect(onSubmit).toHaveBeenCalledWith({ lat: 48.8584, lon: 2.2945, label: null })
    expect(getPos).not.toHaveBeenCalled()
  })

  it('moving a GPS pin by hand drops the GPS accuracy', async () => {
    mockPosition({ latitude: 1, longitude: 2, accuracy: 30 })
    const onSubmit = vi.fn()
    const { LocationPicker } = await import('./LocationPicker')
    const { getByText, findByTestId } = render(
      <LocationPicker open onSubmit={onSubmit} onClose={vi.fn()} />,
    )
    fireEvent.click(getByText(/Use my current location/))
    expect((await findByTestId('map')).getAttribute('data-accuracy')).toBe('30')
    lastOnPick!(3, 4)
    await findByTestId('map')
    fireEvent.click(getByText('Use this location'))
    expect(onSubmit).toHaveBeenCalledWith({ lat: 3, lon: 4, label: null })
  })

  it.each([
    [1, /blocked for this site/],
    [2, /Couldn't find your position/],
    [3, /took too long/],
  ])('shows geolocation error %i inline with the map fallback', async (code, text) => {
    mockGeoError(code)
    const { LocationPicker } = await import('./LocationPicker')
    const { getByText, getByRole, queryByTestId } = render(
      <LocationPicker open onSubmit={vi.fn()} onClose={vi.fn()} />,
    )
    fireEvent.click(getByText(/Use my current location/))
    expect(getByRole('alert').textContent).toMatch(text)
    expect(queryByTestId('map')).toBeNull()
    // The fallback is one tap away and clears the error.
    fireEvent.click(getByText(/Pick a spot on the map/))
    expect(queryByTestId('map')).toBeTruthy()
  })

  it('explains when the browser has no geolocation at all', async () => {
    const { LocationPicker } = await import('./LocationPicker')
    const { getByText, getByRole } = render(
      <LocationPicker open onSubmit={vi.fn()} onClose={vi.fn()} />,
    )
    fireEvent.click(getByText(/Use my current location/))
    expect(getByRole('alert').textContent).toMatch(/can't share your position/)
  })

  it('starts fresh every time it opens', async () => {
    mockPosition({ latitude: 1, longitude: 2 })
    const { LocationPicker } = await import('./LocationPicker')
    const { getByText, findByTestId, queryByTestId, rerender } = render(
      <LocationPicker open onSubmit={vi.fn()} onClose={vi.fn()} />,
    )
    fireEvent.click(getByText(/Use my current location/))
    await findByTestId('map')
    rerender(<LocationPicker open={false} onSubmit={vi.fn()} onClose={vi.fn()} />)
    rerender(<LocationPicker open onSubmit={vi.fn()} onClose={vi.fn()} />)
    expect(queryByTestId('map')).toBeNull()
  })
})
