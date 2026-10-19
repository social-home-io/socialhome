import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, waitFor } from '@testing-library/preact'

const { api, toast } = vi.hoisted(() => ({
  api: { get: vi.fn(), put: vi.fn(), delete: vi.fn() },
  toast: vi.fn(),
}))
vi.mock('@/api', () => ({ api }))
vi.mock('@/components/Toast', () => ({ showToast: toast }))

// The map and the picker: a picture, and a button that hands back a point.
vi.mock('./LocalAreaMap', () => ({
  LocalAreaMap: () => <div data-testid="home-map" />,
}))
vi.mock('./LocalAreaPicker', () => ({
  PIN_DOT_KM: 0.15,
  LocalAreaPicker: (p: { open: boolean; mode: string; onSubmit: (a: { lat: number; lon: number }) => void }) =>
    p.open ? (
      <button type="button" data-testid={`picker-${p.mode}`}
              onClick={() => p.onSubmit({ lat: 47.3769, lon: 8.5417 })}>pick</button>
    ) : null,
}))

import { instanceConfig } from '@/store/instance'
import { selfLat, selfLon } from '@/store/connections'
import { HomeLocationSettings } from './HomeLocationSettings'

function platform(capabilities: string[]) {
  instanceConfig.value = {
    mode: capabilities.includes('home_location_sync') ? 'ha' : 'standalone',
    instance_name: 'Keller',
    instance_id: 'i1',
    capabilities,
    setup_required: false,
  } as typeof instanceConfig.value
}

beforeEach(() => {
  api.get.mockReset(); api.put.mockReset(); api.delete.mockReset(); toast.mockReset()
  selfLat.value = null; selfLon.value = null
  platform([])
})

describe('HomeLocationSettings — the household sets it (standalone)', () => {
  it('no home yet: says so, explains where it goes, and offers to set it', async () => {
    api.get.mockResolvedValue({ latitude: null, longitude: null, from_platform: false })
    const view = render(<HomeLocationSettings />)
    await waitFor(() => view.getByText('Not set yet.'))
    expect(api.get).toHaveBeenCalledWith('/api/admin/home-location')
    expect(view.getByText('It stays in your household.')).toBeTruthy()
    expect(view.getByText(/only if you turn on “Share our home location” for that connection/)).toBeTruthy()
    expect(view.getByText(/about 1 km/)).toBeTruthy()
    expect(view.queryByTestId('home-map')).toBeNull()
    expect(view.queryByRole('button', { name: 'Remove' })).toBeNull()
    fireEvent.click(view.getByRole('button', { name: 'Set our home' }))
    api.put.mockResolvedValue({ latitude: 47.3769, longitude: 8.5417, from_platform: false })
    await act(async () => { fireEvent.click(view.getByTestId('picker-home')) })
    expect(api.put).toHaveBeenCalledWith('/api/admin/home-location', { latitude: 47.3769, longitude: 8.5417 })
    await waitFor(() => view.getByText('47.3769° N, 8.5417° E'))
    expect(view.getByTestId('home-map')).toBeTruthy()
    // Every map / local-community check on this page now knows our home.
    expect([selfLat.value, selfLon.value]).toEqual([47.3769, 8.5417])
    expect(toast).toHaveBeenCalledWith('Home location saved', 'success')
  })

  it('a home: change and remove', async () => {
    api.get.mockResolvedValue({ latitude: 47.3769, longitude: 8.5417, from_platform: false })
    const view = render(<HomeLocationSettings />)
    await waitFor(() => view.getByText('47.3769° N, 8.5417° E'))
    expect(view.getByRole('button', { name: 'Change' })).toBeTruthy()
    api.delete.mockResolvedValue({ latitude: null, longitude: null, from_platform: false })
    await act(async () => { fireEvent.click(view.getByRole('button', { name: 'Remove' })) })
    expect(api.delete).toHaveBeenCalledWith('/api/admin/home-location')
    await waitFor(() => view.getByText('Not set yet.'))
    expect(selfLat.value).toBeNull()
    expect(toast).toHaveBeenCalledWith('Home location removed', 'success')
    // Focus is not stranded on the removed button.
    await waitFor(() => expect(document.activeElement?.textContent).toBe('Set our home'))
  })

  it('a failed save keeps what was there and says so', async () => {
    api.get.mockResolvedValue({ latitude: 47.3769, longitude: 8.5417, from_platform: false })
    const view = render(<HomeLocationSettings />)
    await waitFor(() => view.getByText('47.3769° N, 8.5417° E'))
    api.put.mockRejectedValue(new Error('nope'))
    fireEvent.click(view.getByRole('button', { name: 'Change' }))
    await act(async () => { fireEvent.click(view.getByTestId('picker-home')) })
    expect(toast).toHaveBeenCalledWith("Couldn't save the home location", 'error')
    expect(view.getByText('47.3769° N, 8.5417° E')).toBeTruthy()
  })

  it('a failed read offers to try again', async () => {
    api.get.mockRejectedValueOnce(new Error('offline'))
    const view = render(<HomeLocationSettings />)
    await waitFor(() => view.getByText("Couldn't load the home location."))
    api.get.mockResolvedValue({ latitude: null, longitude: null, from_platform: false })
    await act(async () => { fireEvent.click(view.getByRole('button', { name: 'Try again' })) })
    await waitFor(() => view.getByText('Not set yet.'))
  })
})

describe('HomeLocationSettings — Home Assistant owns it', () => {
  it('shows HA\'s home read-only and says to change it in Home Assistant', async () => {
    platform(['home_location_sync'])
    api.get.mockResolvedValue({ latitude: 52.52, longitude: 13.405, from_platform: true })
    const view = render(<HomeLocationSettings />)
    await waitFor(() => view.getByText('52.5200° N, 13.4050° E'))
    expect(view.getByText(/Change it in Home Assistant \(Settings → System → General\)/)).toBeTruthy()
    expect(view.queryByRole('button', { name: 'Change' })).toBeNull()
    expect(view.queryByRole('button', { name: 'Remove' })).toBeNull()
    expect(view.queryByRole('button', { name: 'Set our home' })).toBeNull()
    expect(view.getByTestId('home-map')).toBeTruthy()
  })

  it('HA has none: says where to set it, still no editor', async () => {
    platform(['home_location_sync'])
    api.get.mockResolvedValue({ latitude: null, longitude: null, from_platform: true })
    const view = render(<HomeLocationSettings />)
    await waitFor(() => view.getByText('Home Assistant has no home location yet.'))
    expect(view.queryByRole('button', { name: 'Set our home' })).toBeNull()
  })
})
