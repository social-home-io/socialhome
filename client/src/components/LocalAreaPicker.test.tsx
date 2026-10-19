import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render } from '@testing-library/preact'

// The map: capture its pick callback and what it draws.
const map = vi.hoisted(() => ({
  onPick: null as ((lat: number, lon: number) => void) | null,
  area: null as unknown,
}))
vi.mock('./LocalAreaMap', () => ({
  LocalAreaMap: (p: { area: unknown; onPick?: (lat: number, lon: number) => void }) => {
    map.onPick = p.onPick ?? null
    map.area = p.area
    return <div data-testid="area-map" />
  },
}))

import { LocalAreaPicker } from './LocalAreaPicker'

beforeEach(() => { map.onPick = null; map.area = null })

function submitButton(view: ReturnType<typeof render>) {
  return view.getByRole('button', { name: 'Use this area' }) as HTMLButtonElement
}

describe('LocalAreaPicker', () => {
  it('a tap on the map sets the centre (4 dp); the default radius is 2 km', async () => {
    const onSubmit = vi.fn()
    const view = render(
      <LocalAreaPicker open initial={null} home={null} onClose={vi.fn()} onSubmit={onSubmit} />,
    )
    expect(submitButton(view).disabled).toBe(true)
    expect(view.getByRole('status').textContent).toBe('Tap the map, or type the centre below.')
    await act(async () => { map.onPick!(47.376_912_3, 8.541_749_9) })
    expect(view.getByRole('status').textContent).toBe('Centre: 47.3769° N, 8.5417° E')
    expect(map.area).toEqual({ lat: 47.3769, lon: 8.5417, radius_km: 2 })
    fireEvent.click(submitButton(view))
    expect(onSubmit).toHaveBeenCalledWith({ lat: 47.3769, lon: 8.5417, radius_km: 2 })
  })

  it('never asks for the device position; "Use our home" takes the household home', async () => {
    const getCurrentPosition = vi.fn()
    Object.defineProperty(navigator, 'geolocation', {
      value: { getCurrentPosition }, configurable: true,
    })
    const onSubmit = vi.fn()
    const view = render(
      <LocalAreaPicker open initial={null} home={{ lat: 46.948_12, lon: 7.447_41 }}
                       onClose={vi.fn()} onSubmit={onSubmit} />,
    )
    fireEvent.click(view.getByRole('button', { name: /Use our home as the centre/ }))
    fireEvent.click(view.getByRole('radio', { name: '10 km' }))
    fireEvent.click(submitButton(view))
    expect(onSubmit).toHaveBeenCalledWith({ lat: 46.9481, lon: 7.4474, radius_km: 10 })
    expect(getCurrentPosition).not.toHaveBeenCalled()
  })

  it('no home → no shortcut', () => {
    const view = render(
      <LocalAreaPicker open initial={null} home={null} onClose={vi.fn()} onSubmit={vi.fn()} />,
    )
    expect(view.queryByRole('button', { name: /Use our home/ })).toBeNull()
  })

  it('opens on the stored area, keeping an off-preset radius selectable', () => {
    const view = render(
      <LocalAreaPicker open initial={{ lat: 1, lon: 2, radius_km: 3.5 }} home={null}
                       onClose={vi.fn()} onSubmit={vi.fn()} />,
    )
    expect(map.area).toEqual({ lat: 1, lon: 2, radius_km: 3.5 })
    const chosen = view.getByRole('radio', { name: '3.5 km' })
    expect(chosen.getAttribute('aria-checked')).toBe('true')
    // The presets are all there.
    for (const km of ['1 km', '2 km', '5 km', '10 km', '25 km']) {
      expect(view.getByRole('radio', { name: km })).toBeTruthy()
    }
  })

  it('the keyboard way: typing latitude and longitude sets the centre (4 dp, comma decimals too)', async () => {
    const onSubmit = vi.fn()
    const view = render(
      <LocalAreaPicker open initial={null} home={null} onClose={vi.fn()} onSubmit={onSubmit} />,
    )
    const lat = view.getByLabelText(/Latitude/) as HTMLInputElement
    const lon = view.getByLabelText(/Longitude/) as HTMLInputElement
    fireEvent.input(lat, { target: { value: '47,376912' } })
    expect(submitButton(view).disabled).toBe(true) // longitude still missing
    fireEvent.input(lon, { target: { value: '8.541749' } })
    expect(map.area).toEqual({ lat: 47.3769, lon: 8.5417, radius_km: 2 })
    expect(view.getByRole('status').textContent).toBe('Centre: 47.3769° N, 8.5417° E')
    fireEvent.click(submitButton(view))
    expect(onSubmit).toHaveBeenCalledWith({ lat: 47.3769, lon: 8.5417, radius_km: 2 })
  })

  it('a bad coordinate is flagged and blocks the save', () => {
    const view = render(
      <LocalAreaPicker open initial={{ lat: 1, lon: 2, radius_km: 5 }} home={null}
                       onClose={vi.fn()} onSubmit={vi.fn()} />,
    )
    const lat = view.getByLabelText(/Latitude/) as HTMLInputElement
    expect(lat.value).toBe('1.0000')
    fireEvent.input(lat, { target: { value: '95' } })
    expect(lat.getAttribute('aria-invalid')).toBe('true')
    expect(view.getByText(/A number between −90 and 90/)).toBeTruthy()
    expect(submitButton(view).disabled).toBe(true)
  })

  it('a map tap or "Use our home" fills the typed fields', async () => {
    const view = render(
      <LocalAreaPicker open initial={null} home={{ lat: 46.948_12, lon: 7.447_41 }}
                       onClose={vi.fn()} onSubmit={vi.fn()} />,
    )
    fireEvent.click(view.getByRole('button', { name: /Use our home as the centre/ }))
    expect((view.getByLabelText(/Latitude/) as HTMLInputElement).value).toBe('46.9481')
    await act(async () => { map.onPick!(-33.86, 151.2093) })
    expect((view.getByLabelText(/Longitude/) as HTMLInputElement).value).toBe('151.2093')
  })

  it('cancel closes without a pick', () => {
    const onClose = vi.fn()
    const onSubmit = vi.fn()
    const view = render(
      <LocalAreaPicker open initial={null} home={null} onClose={onClose} onSubmit={onSubmit} />,
    )
    fireEvent.click(view.getByRole('button', { name: 'Cancel' }))
    expect(onClose).toHaveBeenCalled()
    expect(onSubmit).not.toHaveBeenCalled()
  })
})
