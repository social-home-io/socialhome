import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

vi.mock('@/api', () => {
  const m = { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }
  return { api: m }
})

vi.mock('preact-iso', () => ({
  useLocation: () => ({ route: vi.fn() }),
}))

vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u-me', username: 'pascal', display_name: 'Pascal' } },
}))

import { api } from '@/api'
import HighlightComposerPage, { audienceFromFriends } from './HighlightComposerPage'

const get = api.get as ReturnType<typeof vi.fn>

/** Shape of ``GET /api/friends`` (socialhome/routes/friends.py). */
const FRIENDS = {
  instance: {
    instance_id: 'inst-me',
    display_name: 'Home',
    members: [
      { user_id: 'u-me', username: 'pascal', display_name: 'Pascal' },
      { user_id: 'u-anna', username: 'anna', display_name: 'Anna' },
    ],
  },
  households: [
    {
      instance_id: 'inst-b',
      display_name: 'The Smiths',
      members: [
        { user_id: 'u-bob', remote_username: 'bob', display_name: 'Bob', personal_alias: 'Uncle Bob' },
      ],
    },
  ],
  totals: { households: 2, people: 3 },
}

function mockGet(friends: unknown) {
  get.mockReset()
  get.mockImplementation((path: string) => {
    if (path === '/api/friends') {
      return friends instanceof Error ? Promise.reject(friends) : Promise.resolve(friends)
    }
    if (path === '/api/highlights') return Promise.resolve([])
    return Promise.reject(new Error(`unexpected GET ${path}`))
  })
}

describe('audienceFromFriends', () => {
  it('maps households and people, dropping the author, preferring aliases', () => {
    const out = audienceFromFriends(FRIENDS, 'u-me')
    expect(out.households).toEqual([{ instance_id: 'inst-b', display_name: 'The Smiths' }])
    expect(out.people).toEqual([
      { user_id: 'u-anna', display_name: 'Anna', household_name: null },
      { user_id: 'u-bob', display_name: 'Uncle Bob', household_name: 'The Smiths' },
    ])
  })
})

describe('HighlightComposerPage audience picker', () => {
  beforeEach(() => mockGet(FRIENDS))

  it('loads households + people from GET /api/friends (not the dead routes)', async () => {
    const view = render(<HighlightComposerPage />)
    await waitFor(() => expect(get).toHaveBeenCalledWith('/api/friends'))
    const urls = get.mock.calls.map(c => c[0])
    expect(urls).not.toContain('/api/instances?status=confirmed')
    expect(urls).not.toContain('/api/connections/people')

    fireEvent.click(view.getByLabelText('Pick households'))
    expect(await view.findByLabelText('The Smiths')).toBeTruthy()

    fireEvent.click(view.getByText(/per-person picker/))
    fireEvent.click(view.getByLabelText('Pick people'))
    expect(await view.findByText('Uncle Bob')).toBeTruthy()
    expect(view.getByText('Anna')).toBeTruthy()
    expect(view.getByText(/The Smiths/, { selector: 'span' })).toBeTruthy()
  })

  it('surfaces a load failure with Retry instead of an empty picker', async () => {
    mockGet(new Error('API 500: /api/friends'))
    const view = render(<HighlightComposerPage />)
    fireEvent.click(view.getByLabelText('Pick households'))
    const alert = await view.findByRole('alert')
    expect(alert.textContent).toMatch(/Couldn't load your connections/)
    expect(view.queryByText('No connected households yet.')).toBeNull()

    mockGet(FRIENDS)
    fireEvent.click(view.getByRole('button', { name: 'Retry' }))
    expect(await view.findByLabelText('The Smiths')).toBeTruthy()
    expect(view.queryByRole('alert')).toBeNull()
  })

  it('shows the empty state when there are no connections', async () => {
    mockGet({ instance: { members: [] }, households: [] })
    const view = render(<HighlightComposerPage />)
    await waitFor(() => expect(get).toHaveBeenCalledWith('/api/friends'))
    fireEvent.click(view.getByLabelText('Pick households'))
    expect(await view.findByText('No connected households yet.')).toBeTruthy()
  })
})
