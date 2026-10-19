import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'

const { get, patch, toast } = vi.hoisted(() => ({ get: vi.fn(), patch: vi.fn(), toast: vi.fn() }))
vi.mock('@/api', () => ({ api: { get, patch } }))
vi.mock('@/components/Toast', () => ({ showToast: toast }))
vi.mock('@/components/LocalAreaMap', () => ({ LocalAreaMap: () => <div data-testid="area-map" /> }))
vi.mock('@/components/LocalAreaPicker', () => ({
  PIN_DOT_KM: 0.15,
  LocalAreaPicker: ({ open, mode, home, onSubmit }: {
    open: boolean
    mode?: string
    home: unknown
    onSubmit: (a: { lat: number; lon: number; radius_km: number }) => void
  }) => (open
    ? (
      <div role="dialog" data-mode={mode} data-home={home === null ? 'none' : 'some'}>
        <button type="button" onClick={() => onSubmit({ lat: 46.9481, lon: 7.4474, radius_km: 0.15 })}>pick</button>
      </div>
    )
    : null),
}))

import type { Space } from '@/types'
import { LocationSection } from './LocationSection'
import { PlacePinCard } from './PlacePinCard'

function space(over: Partial<Space> = {}): Space {
  return { id: 's1', name: 'Chess', space_type: 'global', join_mode: 'request', features: {}, ...over } as unknown as Space
}

beforeEach(() => {
  get.mockReset(); get.mockResolvedValue({ zones: [] })
  patch.mockReset(); patch.mockResolvedValue({}); toast.mockReset()
})

describe('PlacePinCard', () => {
  it('pins a place from the pin picker — never offered the home — and says it is public', async () => {
    const view = render(<PlacePinCard space={space()} onUpdate={vi.fn()} />)
    expect(view.getByRole('status').textContent).toContain('No place pinned')
    expect(view.getByText('This place is shown to everyone who can find the space.')).toBeTruthy()
    expect(view.getByText(/Only used to sort Discover by distance/)).toBeTruthy()
    fireEvent.click(view.getByRole('button', { name: 'Pin a place' }))
    const dialog = view.getByRole('dialog')
    expect(dialog.getAttribute('data-mode')).toBe('pin')
    expect(dialog.getAttribute('data-home')).toBe('none')
    fireEvent.click(view.getByRole('button', { name: 'pick' }))
    await waitFor(() => expect(patch).toHaveBeenCalledWith(
      '/api/spaces/s1', { place: { lat: 46.9481, lon: 7.4474 } }))
    expect(view.getByRole('status').textContent).toBe('Pinned at 46.9481° N, 7.4474° E')
    expect(view.getByTestId('area-map')).toBeTruthy()
    expect(toast).toHaveBeenCalledWith('Place saved', 'success')
  })

  it('removes the pin, and puts it back when the server refuses', async () => {
    const view = render(<PlacePinCard space={space({ place: { lat: 47.3769, lon: 8.5417 } })} onUpdate={vi.fn()} />)
    patch.mockRejectedValueOnce(new Error('offline'))
    fireEvent.click(view.getByRole('button', { name: 'Remove the pin' }))
    await waitFor(() => expect(patch).toHaveBeenCalledWith('/api/spaces/s1', { place: null }))
    await waitFor(() => expect(view.getByRole('status').textContent).toContain('47.3769'))
    fireEvent.click(view.getByRole('button', { name: 'Remove the pin' }))
    await waitFor(() => expect(view.getByRole('status').textContent).toContain('No place pinned'))
  })

  it('Settings › Location shows it for a global space hosted here only', () => {
    const g = render(<LocationSection space={space()} onUpdate={vi.fn()} />)
    expect(g.queryByTestId('settings-place-pin')).toBeTruthy()
    g.unmount()
    const remote = render(<LocationSection space={space()} isRemoteSpace onUpdate={vi.fn()} />)
    expect(remote.queryByTestId('settings-place-pin')).toBeNull()
    remote.unmount()
    const pub = render(<LocationSection space={space({ space_type: 'public' })} onUpdate={vi.fn()} />)
    expect(pub.queryByTestId('settings-place-pin')).toBeNull()
  })
})
