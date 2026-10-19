import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'

const { get, patch, toast } = vi.hoisted(() => ({ get: vi.fn(), patch: vi.fn(), toast: vi.fn() }))
vi.mock('@/api', () => ({ api: { get, patch } }))
vi.mock('@/components/Toast', () => ({ showToast: toast }))

import type { Space } from '@/types'
import { LocationSection } from './LocationSection'

function space(features: Record<string, unknown> = {}): Space {
  return { id: 's1', name: 'Garden', space_type: 'private', join_mode: 'invite_only', features } as unknown as Space
}

beforeEach(() => {
  get.mockReset()
  get.mockResolvedValue({ zones: [{ id: 'z1', name: 'Home', icon: '🏠' }, { id: 'z2', name: 'School', icon: null }] })
  patch.mockReset(); patch.mockResolvedValue({}); toast.mockReset()
})

describe('LocationSection', () => {
  it('off: says how precise it would be, no zones, and saves the switch', async () => {
    const view = render(<LocationSection space={space()} onUpdate={vi.fn()} />)
    expect(view.getByText(/about 11 m/)).toBeTruthy()
    expect(view.queryByTestId('settings-zones')).toBeNull()
    expect(get).not.toHaveBeenCalled()
    fireEvent.click(view.getByRole('switch', { name: 'Show a map tab to members of this space' }))
    await waitFor(() => expect(patch).toHaveBeenCalledWith('/api/spaces/s1', { features: { location: true } }))
  })

  it('on: the zones are listed first-class with a Manage link', async () => {
    const view = render(<LocationSection space={space({ location: true })} onUpdate={vi.fn()} />)
    const zones = await view.findByTestId('settings-zones')
    await waitFor(() => expect(zones.textContent).toContain('Home'))
    expect(zones.textContent).toContain('School')
    expect(view.getByRole('link', { name: 'Manage zones' }).getAttribute('href')).toBe('/spaces/s1/zones')
  })

  it('zone only with no zones says it needs one', async () => {
    get.mockResolvedValue({ zones: [] })
    const view = render(<LocationSection space={space({ location: true, location_mode: 'zone_only' })} onUpdate={vi.fn()} />)
    expect(await view.findByText(/zone only needs at least one/)).toBeTruthy()
  })

  it('the privacy mode saves alone with its own toast', async () => {
    const view = render(<LocationSection space={space({ location: true })} onUpdate={vi.fn()} />)
    fireEvent.click(view.getByRole('radio', { name: /Zone only/ }))
    await waitFor(() => expect(patch).toHaveBeenCalledWith('/api/spaces/s1', { features: { location_mode: 'zone_only' } }))
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('Zone only is on'), 'success')
  })

  it('points to the Map for your own sharing — it is not set here', () => {
    const view = render(<LocationSection space={space({ location: true })} onUpdate={vi.fn()} />)
    expect(view.getByRole('link', { name: 'Open the map' }).getAttribute('href')).toBe('/spaces/s1/map')
    expect(view.queryByRole('switch', { name: /share my location/i })).toBeNull()
  })
})
