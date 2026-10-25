import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

const route = vi.hoisted(() => vi.fn())

vi.mock('@/api', () => {
  const m = { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }
  return { api: m }
})
vi.mock('preact-iso', () => ({
  useLocation: () => ({ route, url: '/' }),
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

  it('a friend from another household, on no roster here, reads by the server\'s name', async () => {
    apiMock.get.mockResolvedValue({
      ...detail('h-friend', 'ru-amaka'),
      highlight: {
        ...detail('h-friend', 'ru-amaka').highlight,
        author: { display_name: 'Amaka Okafor', picture_url: null },
      },
    })
    const { container, findByText } = render(<HighlightShareCard highlightId="h-friend" spaceId="s1" />)
    expect(await findByText('Amaka Okafor')).toBeTruthy()
    expect(container.textContent).not.toContain('Someone')
  })

  // The highlight's audience may not include this reader — the share post
  // is what opens it, so both the fetch and the viewer link carry it.
  it('reads and opens the highlight through its share post', async () => {
    apiMock.get.mockResolvedValue(detail('h-shared', 'u-pascal'))
    const { container } = render(
      <HighlightShareCard highlightId="h-shared" postId="p 1" spaceId="s1" />,
    )
    await waitFor(() => expect(container.querySelector('button')).toBeTruthy())
    expect(apiMock.get).toHaveBeenCalledWith('/api/highlights/h-shared?via_post=p%201')
    fireEvent.click(container.querySelector('button')!)
    expect(route).toHaveBeenCalledWith('/highlights/h-shared?via_post=p%201')
  })

  it('an author nobody knows reads "Someone"', async () => {
    apiMock.get.mockResolvedValue(detail('h-unknown', 'tvnxzlljlae437w5fmwriqqvakgfynks'))
    const { container } = render(<HighlightShareCard highlightId="h-unknown" />)
    await waitFor(() => expect(container.textContent).toContain('Someone'))
    expect(container.textContent).not.toContain('tvnxzll')
  })
})

describe('HighlightShareCard when the read fails', () => {
  beforeEach(() => { apiMock.get.mockReset() })

  it('a 404 is "ended"', async () => {
    apiMock.get.mockRejectedValue(Object.assign(new Error('not found'), { status: 404 }))
    const { container } = render(<HighlightShareCard highlightId="h-gone-404" />)
    await waitFor(() => expect(container.textContent).toContain('Highlight has ended'))
  })

  it('a transient failure is never cached as "ended": it offers Try again', async () => {
    let reads = 0
    apiMock.get.mockImplementation(async (path: string) => {
      if (!path.startsWith('/api/highlights/')) return []
      reads += 1
      if (reads === 1) throw new Error('offline')
      return detail('h-flaky', 'u-pascal')
    })
    const { container, findByRole } = render(<HighlightShareCard highlightId="h-flaky" />)
    await waitFor(() => expect(container.textContent).toContain("Couldn't load this highlight"))
    expect(container.textContent).not.toContain('Highlight has ended')
    fireEvent.click(await findByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(container.querySelector('.sh-highlight-share-card--ended')).toBeNull())
    await waitFor(() => expect(reads).toBe(2))
    await waitFor(() => expect(container.querySelector('button[aria-label^="Watch"]')).toBeTruthy())
  })
})
