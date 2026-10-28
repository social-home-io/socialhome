import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/preact'

const get = vi.fn()
vi.mock('@/api', () => ({ api: { get: (...a: unknown[]) => get(...a), post: vi.fn() } }))
vi.mock('@/ws', () => ({ ws: { on: () => () => {} } }))

import { PollUI, type PollData } from './PollUI'

function poll(over: Partial<PollData> = {}): PollData {
  return {
    post_id: 'p1',
    question: 'Dinner?',
    options: [
      { id: 'a', text: 'Pizza', vote_count: 1 },
      { id: 'b', text: 'Soup', vote_count: 1 },
    ],
    allow_multiple: false,
    closed: false,
    closes_at: null,
    total_votes: 2,
    user_vote: [],
    space_id: null,
    ...over,
  }
}

describe('PollUI', () => {
  beforeEach(() => get.mockReset())

  it('a poll past its deadline says Closed once — no "voting ended" beside it', async () => {
    get.mockResolvedValue(poll({ closed: true, closes_at: '2020-01-01T00:00:00+00:00' }))
    render(<PollUI postId="p1" currentUserId="u1" />)
    await waitFor(() => expect(screen.getByText('Closed')).toBeTruthy())
    expect(screen.queryByText(/voting ended/)).toBeNull()
    expect(screen.getByText('2 votes')).toBeTruthy()
  })

  it('an open poll counts down to its deadline', async () => {
    const inTwoDays = new Date(Date.now() + 2 * 86_400_000 + 60_000).toISOString()
    get.mockResolvedValue(poll({ closes_at: inTwoDays }))
    render(<PollUI postId="p1" currentUserId="u1" />)
    await waitFor(() => expect(screen.getByText(/closes in 2d/)).toBeTruthy())
    expect(screen.queryByText('Closed')).toBeNull()
  })

  it('an open poll whose deadline just passed still reads "voting ended"', async () => {
    get.mockResolvedValue(poll({ closes_at: '2020-01-01T00:00:00+00:00' }))
    render(<PollUI postId="p1" currentUserId="u1" />)
    await waitFor(() => expect(screen.getByText(/voting ended/)).toBeTruthy())
  })
})
