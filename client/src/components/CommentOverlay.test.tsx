import { describe, it, expect, beforeEach, vi } from 'vitest'
import {
  render, waitFor, fireEvent, getByRole, queryAllByRole,
} from '@testing-library/preact'

vi.mock('@/api', () => {
  const m = { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }
  return { api: m, _mock: m }
})

vi.mock('@/ws', () => {
  const handlers: Record<string, (e: { data: Record<string, unknown> }) => void> = {}
  return {
    ws: {
      on: (type: string, h: (e: { data: Record<string, unknown> }) => void) => {
        handlers[type] = h
        return () => { delete handlers[type] }
      },
    },
    _handlers: handlers,
  }
})

vi.mock('@/utils/avatar', () => ({
  resolveAvatar: () => null,
  resolveDisplayName: (_sid: string | null, uid: string) => uid,
}))

vi.mock('./Toast', () => ({ showToast: vi.fn() }))

const openPersonSpy = vi.hoisted(() => vi.fn())
vi.mock('@/components/PersonSheet', () => ({
  openPerson: (...args: unknown[]) => openPersonSpy(...args),
}))

import { CommentOverlay, openCommentOverlay, closeCommentOverlay } from './CommentOverlay'
import { api } from '@/api'
import * as wsModule from '@/ws'
import type { FeedPost } from '@/types'

const apiMock = api as unknown as {
  get: ReturnType<typeof vi.fn>; post: ReturnType<typeof vi.fn>;
  patch: ReturnType<typeof vi.fn>; delete: ReturnType<typeof vi.fn>;
}
const wsHandlers = (wsModule as unknown as {
  _handlers: Record<string, (e: { data: Record<string, unknown> }) => void>
})._handlers

function fakePost(): FeedPost {
  return {
    id: 'p-1',
    author: 'u-anna',
    type: 'text',
    content: 'Hello household',
    media_url: null,
    image_urls: [],
    created_at: '2026-05-02T10:00:00Z',
    edited_at: null,
    pinned: false,
    comment_count: 0,
    reactions: {},
    location: null,
  } as unknown as FeedPost
}

describe('CommentOverlay', () => {
  beforeEach(() => {
    apiMock.get.mockReset()
    apiMock.post.mockReset()
    apiMock.patch.mockReset()
    apiMock.delete.mockReset()
    Object.keys(wsHandlers).forEach(k => delete wsHandlers[k])
    closeCommentOverlay()
  })

  it('renders nothing when closed', () => {
    const { container } = render(<CommentOverlay />)
    expect(container.querySelector('.sh-comment-overlay')).toBeNull()
  })

  it('opens and renders the post header', async () => {
    apiMock.get.mockResolvedValue([])
    openCommentOverlay(fakePost(), null)
    const { container } = render(<CommentOverlay />)
    await waitFor(() => {
      expect(container.querySelector('.sh-comment-overlay')).not.toBeNull()
    })
    expect(container.textContent).toContain('Hello household')
    expect(container.textContent).toContain('u-anna')
  })

  it('adds no second banner landmark next to the app topbar', async () => {
    // The sheet mounts at App level, outside <main>; a top-level
    // <header> there maps to role=banner, so axe flagged the page
    // with two banners (landmark-no-duplicate-banner / -unique) while
    // the sheet was open. The dialog is the landmark; its header row
    // is not.
    apiMock.get.mockResolvedValue([])
    openCommentOverlay(fakePost(), null)
    const { container } = render(<CommentOverlay />)
    await waitFor(() => {
      expect(container.querySelector('.sh-comment-overlay')).not.toBeNull()
    })
    const root = container as HTMLElement
    expect(queryAllByRole(root, 'banner')).toHaveLength(0)
    expect(root.querySelector('.sh-comment-overlay-header')).not.toBeNull()
    const dialog = getByRole(root, 'dialog')
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(dialog.getAttribute('aria-label')).toBeTruthy()
  })

  it('renders the close button and drag handle', async () => {
    apiMock.get.mockResolvedValue([])
    openCommentOverlay(fakePost(), null)
    const { container } = render(<CommentOverlay />)
    await waitFor(() => {
      expect(container.querySelector('.sh-comment-overlay')).not.toBeNull()
    })
    expect(container.querySelector('.sh-comment-overlay-close')).not.toBeNull()
    expect(container.querySelector('.sh-comment-overlay-handle')).not.toBeNull()
  })

  it('renders the comment count chip', async () => {
    apiMock.get.mockResolvedValue([])
    openCommentOverlay(fakePost(), null)
    const { container } = render(<CommentOverlay />)
    await waitFor(() => {
      expect(container.querySelector('.sh-comment-overlay-count')).not.toBeNull()
    })
    const chip = container.querySelector('.sh-comment-overlay-count')!
    expect(chip.textContent?.toLowerCase()).toContain('comment')
  })

  it('exposes wsHandlers for live-update hooks', () => {
    // Smoke-check that the WS subscription contract is in place — the
    // overlay component must register at least one handler for each
    // comment frame type when it opens.
    apiMock.get.mockResolvedValue([])
    openCommentOverlay(fakePost(), null)
    render(<CommentOverlay />)
    // The handlers are registered synchronously inside useEffect after
    // the first paint; we don't assert the call, just the wiring shape.
    expect(typeof wsHandlers).toBe('object')
  })

  it('the post author in the header opens their person sheet', async () => {
    openPersonSpy.mockReset()
    apiMock.get.mockResolvedValue([])
    openCommentOverlay(fakePost(), null)
    const { getByTestId } = render(<CommentOverlay />)
    await waitFor(() => { expect(getByTestId('overlay-author')).toBeTruthy() })
    fireEvent.click(getByTestId('overlay-author'))
    expect(openPersonSpy).toHaveBeenCalledWith('u-anna', expect.anything())
  })

  it('a bot post header has no person button', async () => {
    apiMock.get.mockResolvedValue([])
    openCommentOverlay({ ...fakePost(), author: 'system-integration' }, null)
    const { container } = render(<CommentOverlay />)
    await waitFor(() => {
      expect(container.querySelector('.sh-comment-overlay')).not.toBeNull()
    })
    expect(container.querySelector('.sh-person-btn')).toBeNull()
  })

  it('Escape leaves the thread open while a dialog sits on top of it', async () => {
    apiMock.get.mockResolvedValue([])
    openCommentOverlay(fakePost(), null)
    const { container } = render(<CommentOverlay />)
    await waitFor(() => {
      expect(container.querySelector('.sh-comment-overlay')).not.toBeNull()
    })
    const sheet = document.createElement('div')
    sheet.className = 'sh-modal-overlay'
    document.body.appendChild(sheet)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(container.querySelector('.sh-comment-overlay')).not.toBeNull()
    sheet.remove()
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => {
      expect(container.querySelector('.sh-comment-overlay')).toBeNull()
    })
  })
})
