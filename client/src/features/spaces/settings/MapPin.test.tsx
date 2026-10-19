import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'

const { get, patch, toast } = vi.hoisted(() => ({ get: vi.fn(), patch: vi.fn(), toast: vi.fn() }))
vi.mock('@/api', () => ({ api: { get, patch } }))
vi.mock('@/components/Toast', () => ({ showToast: toast }))
// The picker's map: hand the test its pick callback and a submit button.
vi.mock('@/components/LocationPicker', () => ({
  LocationPicker: ({ open, initial, onSubmit, title }: {
    open: boolean
    initial: { lat: number; lon: number } | null
    onSubmit: (d: { lat: number; lon: number; label: null }) => void
    title: string
  }) => (open
    ? (
      <div role="dialog" aria-label={title} data-initial={initial ? `${initial.lat},${initial.lon}` : ''}>
        <button type="button" onClick={() => onSubmit({ lat: 46.9481, lon: 7.4474, label: null })}>pick</button>
      </div>
    )
    : null),
}))

import type { Space } from '@/types'
import { LocationSection } from './LocationSection'
import { MapPin } from './MapPin'

function space(over: Partial<Space> = {}): Space {
  return { id: 's1', name: 'Garden', space_type: 'public', join_mode: 'open', features: {}, ...over } as unknown as Space
}

beforeEach(() => {
  get.mockReset(); get.mockResolvedValue({ zones: [] })
  patch.mockReset(); patch.mockResolvedValue({}); toast.mockReset()
})

describe('MapPin', () => {
  it('shows where the pin is, moves it from the picker and saves at once', async () => {
    const onUpdate = vi.fn()
    const view = render(<MapPin spaceId="s1" pin={{ lat: 47.3769, lon: 8.5417 }} onUpdate={onUpdate} />)
    expect(view.getByRole('status').textContent).toBe('On the map at 47.3769° N, 8.5417° E')
    fireEvent.click(view.getByRole('button', { name: 'Move the pin' }))
    const dialog = view.getByRole('dialog', { name: 'Where is the space?' })
    expect(dialog.getAttribute('data-initial')).toBe('47.3769,8.5417')
    fireEvent.click(view.getByRole('button', { name: 'pick' }))
    await waitFor(() => expect(patch).toHaveBeenCalledWith('/api/spaces/s1', { lat: 46.9481, lon: 7.4474 }))
    expect(view.getByRole('status').textContent).toContain('46.9481° N')
    expect(toast).toHaveBeenCalledWith('Map pin saved', 'success')
    expect(onUpdate).toHaveBeenCalled()
  })

  it('removes the pin, and puts it back when the server refuses', async () => {
    const view = render(<MapPin spaceId="s1" pin={{ lat: 47.3769, lon: 8.5417 }} onUpdate={vi.fn()} />)
    patch.mockRejectedValueOnce(new Error('offline'))
    fireEvent.click(view.getByRole('button', { name: 'Remove from the map' }))
    await waitFor(() => expect(patch).toHaveBeenCalledWith('/api/spaces/s1', { lat: null, lon: null }))
    await waitFor(() => expect(view.getByRole('status').textContent).toContain('47.3769'))
    fireEvent.click(view.getByRole('button', { name: 'Remove from the map' }))
    await waitFor(() => expect(view.getByRole('status').textContent).toBe('Not on the map.'))
    expect(view.getByRole('button', { name: 'Pin on the map' })).toBeTruthy()
  })
})

describe('LocationSection › map pin', () => {
  it('a public space hosted here gets the pin', () => {
    const view = render(<LocationSection space={space({ map_pin: { lat: 1, lon: 2 } })} onUpdate={vi.fn()} />)
    expect(view.getByTestId('settings-map-pin')).toBeTruthy()
  })

  it('no pin for another tier or a space hosted elsewhere', () => {
    const priv = render(<LocationSection space={space({ space_type: 'private' })} onUpdate={vi.fn()} />)
    expect(priv.queryByTestId('settings-map-pin')).toBeNull()
    priv.unmount()
    const remote = render(<LocationSection space={space()} isRemoteSpace onUpdate={vi.fn()} />)
    expect(remote.queryByTestId('settings-map-pin')).toBeNull()
  })
})
