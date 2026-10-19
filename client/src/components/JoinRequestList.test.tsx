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
})
