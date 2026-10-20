import { describe, it, expect, vi } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

const { get } = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('@/api', () => ({ api: { get, post: vi.fn() } }))
vi.mock('@/ws', () => ({ ws: { on: () => () => {} } }))

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
