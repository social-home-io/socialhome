import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'

const { get, post, confirm } = vi.hoisted(() => ({
  get: vi.fn(), post: vi.fn(), confirm: vi.fn(),
}))
vi.mock('@/api', () => ({ api: { get, post } }))
vi.mock('@/ws', () => ({ ws: { on: () => () => {} } }))
vi.mock('./confirm', () => ({ confirmDialog: confirm }))

import { householdUsers } from '@/store/householdUsers'
import { JoinRequestList } from './JoinRequestList'

const RAW = 'ppsfwqhik3nxgtak6o6m3hxo5i6wwj2v'

describe('JoinRequestList', () => {
  it('names each applicant: server name, then this household, else "Someone" — never the raw id', async () => {
    householdUsers.value = new Map([
      ['u-lina', { user_id: 'u-lina', username: 'lina', display_name: 'Lina' }],
    ] as never)
    get.mockImplementation(async (url: string) => (url.endsWith('/join-requests')
      ? [
        { id: 'r1', space_id: 's1', user_id: 'u-remote', display_name: 'Sara', requested_at: '' },
        { id: 'r2', space_id: 's1', user_id: 'u-lina', requested_at: '' },
        { id: 'r3', space_id: 's1', user_id: RAW, requested_at: '' },
      ]
      : url === '/api/users'
        ? [{ user_id: 'u-lina', username: 'lina', display_name: 'Lina' }]
        : []))
    const view = render(<JoinRequestList spaceId="s1" />)
    await waitFor(() => expect(view.getByText('Sara')).toBeTruthy())
    expect(view.getByText('Lina')).toBeTruthy()
    expect(view.getByText('Someone')).toBeTruthy()
    expect(view.container.textContent).not.toContain(RAW)
  })

  it('shows the applicant picture the server signed', async () => {
    householdUsers.value = new Map() as never
    get.mockImplementation(async (url: string) => (url.endsWith('/join-requests')
      ? [{
        id: 'p1', space_id: 's3', user_id: 'ru-rita', display_name: 'Remote Rita',
        picture_url: 'api/users/ru-rita/picture?v=1&sig=x', requested_at: '',
      }]
      : []))
    const view = render(<JoinRequestList spaceId="s3" />)
    await waitFor(() => expect(view.getByText('Remote Rita')).toBeTruthy())
    const img = view.container.querySelector('.sh-join-request img') as HTMLImageElement | null
    expect(img?.getAttribute('src')).toBe('api/users/ru-rita/picture?v=1&sig=x')
  })
})

describe('JoinRequestList — local communities', () => {
  it('flags a request whose home is near the edge, and only that one', async () => {
    householdUsers.value = new Map() as never
    get.mockImplementation(async (url: string) => (url.endsWith('/join-requests')
      ? [
        { id: 'e1', space_id: 's2', user_id: 'u-a', display_name: 'Ana', requested_at: '', area_verdict: 'edge' },
        { id: 'i1', space_id: 's2', user_id: 'u-b', display_name: 'Ben', requested_at: '', area_verdict: 'inside' },
        { id: 'n1', space_id: 's2', user_id: 'u-c', display_name: 'Cleo', requested_at: '' },
      ]
      : []))
    const view = render(<JoinRequestList spaceId="s2" />)
    await waitFor(() => expect(view.getByText('Ana')).toBeTruthy())
    const chips = view.getAllByTestId('join-request-edge')
    expect(chips).toHaveLength(1)
    expect(chips[0].textContent).toContain('Near the edge')
    expect(chips[0].closest('.sh-join-request')?.textContent).toContain('Ana')
  })
})

describe('JoinRequestList — asked through the GFS (v_84)', () => {
  it('shows the name the applicant sent, marked "via the GFS, not verified"', async () => {
    householdUsers.value = new Map() as never
    get.mockImplementation(async (url: string) => (url.endsWith('/join-requests')
      ? [
        {
          id: 'g1', space_id: 's4', user_id: 'u-far', display_name: null, requested_at: '',
          via_gfs: true, unverified_name: 'Noor', unverified_household: 'Zürich flat',
        },
        { id: 'p1', space_id: 's4', user_id: 'u-near', display_name: 'Ben', requested_at: '' },
      ]
      : []))
    const view = render(<JoinRequestList spaceId="s4" />)
    await waitFor(() => expect(view.getByText('Noor')).toBeTruthy())
    const chips = view.getAllByTestId('join-request-gfs')
    expect(chips).toHaveLength(1)
    expect(chips[0].textContent).toBe('Via the GFS · says “Zürich flat” · not verified')
    expect(chips[0].getAttribute('title')).toMatch(/nobody checked/)
  })
})

describe('JoinRequestList — declining a flood from the GFS', () => {
  const rows = [
    { id: 'g1', space_id: 's5', user_id: 'u1', requested_at: '', via_gfs: true, unverified_name: 'A' },
    { id: 'g2', space_id: 's5', user_id: 'u2', requested_at: '', via_gfs: true, unverified_name: 'B' },
    { id: 'p1', space_id: 's5', user_id: 'u3', requested_at: '', display_name: 'Ben' },
  ]

  it('one button declines every request from the GFS, after a confirm', async () => {
    householdUsers.value = new Map() as never
    get.mockImplementation(async (url: string) => (url.endsWith('/join-requests') ? rows : []))
    post.mockResolvedValue({ declined: 2 })
    confirm.mockResolvedValue(true)
    const view = render(<JoinRequestList spaceId="s5" />)
    const button = await view.findByRole('button', { name: 'Decline all 2 from the GFS' })
    fireEvent.click(button)
    await waitFor(() => expect(post).toHaveBeenCalledWith('/api/spaces/s5/gfs-join-requests/decline', {}))
    expect(confirm).toHaveBeenCalled()
    await waitFor(() => expect(view.queryByText('A')).toBeNull())
    expect(view.getByText('Ben')).toBeTruthy()
  })

  it('cancelled, nothing is declined; with one request there is no bulk button', async () => {
    householdUsers.value = new Map() as never
    post.mockReset()
    get.mockImplementation(async (url: string) => (url.endsWith('/join-requests') ? rows : []))
    confirm.mockResolvedValue(false)
    const view = render(<JoinRequestList spaceId="s6" />)
    fireEvent.click(await view.findByRole('button', { name: 'Decline all 2 from the GFS' }))
    await waitFor(() => expect(confirm).toHaveBeenCalled())
    expect(post).not.toHaveBeenCalled()
    view.unmount()

    get.mockImplementation(async (url: string) => (url.endsWith('/join-requests') ? rows.slice(1) : []))
    const one = render(<JoinRequestList spaceId="s7" />)
    await waitFor(() => expect(one.container.textContent).toContain('B'))
    expect(one.container.querySelector('.sh-join-requests__bulk')).toBeNull()
  })
})
