import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'

const { get, patch, toast } = vi.hoisted(() => ({ get: vi.fn(), patch: vi.fn(), toast: vi.fn() }))
vi.mock('@/api', () => ({ api: { get, patch } }))
vi.mock('@/components/Toast', () => ({ showToast: toast }))
vi.mock('@/components/LocalAreaMap', () => ({ LocalAreaMap: () => <div data-testid="area-map" /> }))
// The picker: hand the test a submit with a fixed area.
vi.mock('@/components/LocalAreaPicker', () => ({
  LocalAreaPicker: ({ open, initial, onSubmit }: {
    open: boolean
    initial: { lat: number; lon: number; radius_km: number } | null
    onSubmit: (a: { lat: number; lon: number; radius_km: number }) => void
  }) => (open
    ? (
      <div role="dialog" aria-label="Choose the area"
           data-initial={initial ? `${initial.lat},${initial.lon},${initial.radius_km}` : ''}>
        <button type="button" onClick={() => onSubmit({ lat: 46.9481, lon: 7.4474, radius_km: 10 })}>pick</button>
      </div>
    )
    : null),
}))

import type { Space } from '@/types'
import { LocationSection } from './LocationSection'
import { LocalCommunity } from './LocalCommunity'

function space(over: Partial<Space> = {}): Space {
  return { id: 's1', name: 'Quartier', space_type: 'public', join_mode: 'open', features: {}, ...over } as unknown as Space
}

const AREA = { lat: 47.3769, lon: 8.5417, radius_km: 5 }

beforeEach(() => {
  get.mockReset()
  get.mockImplementation((url: string) => Promise.resolve(url === '/api/friends' ? { instance: {} } : { zones: [] }))
  patch.mockReset(); patch.mockResolvedValue({}); toast.mockReset()
})

describe('LocalCommunity', () => {
  it('shows the area and moves it from the picker, saving at once', async () => {
    const onUpdate = vi.fn()
    const view = render(<LocalCommunity space={space({ local_area: AREA })} onUpdate={onUpdate} />)
    expect(view.getByRole('status').textContent).toBe('Within 5 km of 47.3769° N, 8.5417° E')
    expect(view.getByTestId('area-map')).toBeTruthy()
    expect(view.getByText(/Members who already joined stay/)).toBeTruthy()
    fireEvent.click(view.getByRole('button', { name: 'Change the area' }))
    expect(view.getByRole('dialog').getAttribute('data-initial')).toBe('47.3769,8.5417,5')
    fireEvent.click(view.getByRole('button', { name: 'pick' }))
    await waitFor(() => expect(patch).toHaveBeenCalledWith(
      '/api/spaces/s1', { lat: 46.9481, lon: 7.4474, radius_km: 10 }))
    expect(view.getByRole('status').textContent).toContain('10 km')
    expect(toast).toHaveBeenCalledWith('Area saved', 'success')
    expect(onUpdate).toHaveBeenCalled()
  })

  it('makes a space a local community', async () => {
    const view = render(<LocalCommunity space={space()} onUpdate={vi.fn()} />)
    expect(view.getByRole('status').textContent).toContain('Not a local community')
    expect(view.queryByTestId('area-map')).toBeNull()
    fireEvent.click(view.getByRole('button', { name: 'Make it a local community' }))
    expect(view.getByRole('dialog').getAttribute('data-initial')).toBe('')
    fireEvent.click(view.getByRole('button', { name: 'pick' }))
    await waitFor(() => expect(patch).toHaveBeenCalledWith(
      '/api/spaces/s1', { lat: 46.9481, lon: 7.4474, radius_km: 10 }))
  })

  it('removes the area, and puts it back when the server refuses', async () => {
    const view = render(<LocalCommunity space={space({ local_area: AREA })} onUpdate={vi.fn()} />)
    patch.mockRejectedValueOnce(new Error('offline'))
    fireEvent.click(view.getByRole('button', { name: 'Stop being a local community' }))
    await waitFor(() => expect(patch).toHaveBeenCalledWith(
      '/api/spaces/s1', { lat: null, lon: null, radius_km: null }))
    await waitFor(() => expect(view.getByRole('status').textContent).toContain('47.3769'))
    fireEvent.click(view.getByRole('button', { name: 'Stop being a local community' }))
    await waitFor(() => expect(view.getByRole('status').textContent).toContain('Not a local community'))
    expect(toast).toHaveBeenLastCalledWith('No longer a local community', 'success')
  })

  it('keyboard focus stays on the card after a save (Remove swaps the buttons)', async () => {
    const view = render(<LocalCommunity space={space({ local_area: AREA })} onUpdate={vi.fn()} />)
    const remove = view.getByRole('button', { name: 'Stop being a local community' })
    remove.focus()
    fireEvent.click(remove)
    await waitFor(() => expect(view.getByRole('status').textContent).toContain('Not a local community'))
    await waitFor(() => expect(document.activeElement?.textContent).toBe('Make it a local community'))
  })

  it('with "Let admins act for me" on, explains instead of offering the area', () => {
    const view = render(
      <LocalCommunity space={space({ features: { delegated_admin_authority: true } } as Partial<Space>)}
                      onUpdate={vi.fn()} />,
    )
    const make = view.getByRole('button', { name: 'Make it a local community' }) as HTMLButtonElement
    expect(make.disabled).toBe(true)
    expect(view.getByRole('note').textContent).toContain('Let admins act for me')
  })
})

describe('LocationSection', () => {
  it('a public space hosted here gets the local-community card', () => {
    const view = render(<LocationSection space={space()} onUpdate={vi.fn()} />)
    expect(view.getByTestId('settings-local-area')).toBeTruthy()
  })

  it('no card for another tier or a space hosted elsewhere', () => {
    const priv = render(<LocationSection space={space({ space_type: 'private' })} onUpdate={vi.fn()} />)
    expect(priv.queryByTestId('settings-local-area')).toBeNull()
    priv.unmount()
    const remote = render(<LocationSection space={space({ local_area: AREA })} isRemoteSpace onUpdate={vi.fn()} />)
    expect(remote.queryByTestId('settings-local-area')).toBeNull()
  })
})
