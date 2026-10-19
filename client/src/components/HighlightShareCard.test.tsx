import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

vi.mock('@/api', () => {
  const m = { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }
  return { api: m }
})
vi.mock('preact-iso', () => ({
  useLocation: () => ({ route: vi.fn(), url: '/' }),
}))

import { HighlightShareCard } from './HighlightShareCard'
import { api } from '@/api'
import { householdUsers } from '@/store/householdUsers'
import { spaceMembers } from '@/store/spaceMembers'
import type { SpaceMemberProfile, User } from '@/types'

function detail(id: string, author: string) {
  return {
    highlight: {
      id, author_user_id: author, highlight_date: '2026-10-19', audience_kind: 'all_paired',
      audience: [], created_at: '2026-10-19T08:00:00Z', expires_at: '2026-10-20T08:00:00Z',
    },
    frames: [{ id: 'f1', frame_type: 'image', media_url: 'api/media/x.webp' }],
  }
}

const apiMock = api as unknown as { get: ReturnType<typeof vi.fn> }

describe('HighlightShareCard', () => {
  beforeEach(() => {
    apiMock.get.mockReset()
  })

  it('renders the "ended" placeholder when highlightId is null', () => {
    const { getByText } = render(<HighlightShareCard highlightId={null} />)
    expect(getByText('Highlight has ended')).toBeTruthy()
  })

  it('renders the share-note alongside the placeholder', () => {
    const { getByText } = render(
      <HighlightShareCard highlightId={null} note="From last summer" />,
    )
    expect(getByText('From last summer')).toBeTruthy()
  })

  // The card used to print ``highlight.author_user_id`` as its title — the
  // raw 32-char id. It resolves the author like a post author instead.
  it('titles the card with the author\'s display name, never the raw id', async () => {
    householdUsers.value = new Map([['u-pascal', { user_id: 'u-pascal', display_name: 'Pascal Vizeli' } as User]])
    apiMock.get.mockResolvedValue(detail('h-local', 'u-pascal'))
    const { container, findByText } = render(<HighlightShareCard highlightId="h-local" />)
    expect(await findByText('Pascal Vizeli')).toBeTruthy()
    expect(container.textContent).not.toContain('u-pascal')
    expect(container.querySelector('button')!.getAttribute('aria-label')).toContain('Pascal Vizeli')
    householdUsers.value = new Map()
  })

  it('dates the card in words, not as the raw "2026-10-19" key', async () => {
    apiMock.get.mockResolvedValue(detail('h-date', 'u-pascal'))
    const { container, findByText } = render(<HighlightShareCard highlightId="h-date" />)
    expect(await findByText(/October 19, 2026/)).toBeTruthy()
    expect(container.textContent).not.toContain('2026-10-19')
  })

  it('in a space, a remote author resolves through the space roster', async () => {
    spaceMembers.value = { s1: new Map([['u-oma', {
      user_id: 'u-oma', role: 'member', joined_at: '', space_display_name: null,
      picture_hash: null, picture_url: null, display_name: 'Oma Vizeli', instance_id: 'peer',
    } as SpaceMemberProfile]]) }
    apiMock.get.mockResolvedValue(detail('h-remote', 'u-oma'))
    const { findByText } = render(<HighlightShareCard highlightId="h-remote" spaceId="s1" />)
    expect(await findByText('Oma Vizeli')).toBeTruthy()
    spaceMembers.value = {}
  })

  it('an author nobody knows reads "Someone"', async () => {
    apiMock.get.mockResolvedValue(detail('h-unknown', 'tvnxzlljlae437w5fmwriqqvakgfynks'))
    const { container } = render(<HighlightShareCard highlightId="h-unknown" />)
    await waitFor(() => expect(container.textContent).toContain('Someone'))
    expect(container.textContent).not.toContain('tvnxzll')
  })
})
