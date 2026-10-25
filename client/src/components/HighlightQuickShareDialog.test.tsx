import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, fireEvent, waitFor, act } from '@testing-library/preact'

const mockApi = { get: vi.fn(), post: vi.fn() }
vi.mock('@/api', () => ({
  get api() {
    return mockApi
  },
}))
vi.mock('./Toast', () => ({ showToast: vi.fn() }))
vi.mock('./UploadProgress', () => ({
  UploadProgressBar: () => null,
  uploadWithProgress: vi.fn(async () => ({
    url: '/api/media/p.webp', signed_url: '/api/media/p.webp?sig=x',
  })),
}))

const user: { value: Record<string, unknown> } = {
  value: { user_id: 'u-me', username: 'pascal', preferences_json: '{}' },
}
vi.mock('@/store/auth', () => ({
  get currentUser() {
    return user
  },
}))

import { HighlightQuickShareDialog, openHighlightQuickShare } from './HighlightQuickShareDialog'

const TODAY = new Date().toISOString().slice(0, 10)

/** ``GET /api/highlights`` with (or without) the author's highlight today. */
function inbox(
  audience?: { kind: string, ids: string[] },
  { published = false, date = TODAY }: { published?: boolean, date?: string } = {},
) {
  mockApi.get.mockImplementation(async (path: string) => {
    if (path === '/api/highlights/h-today/publish') return { published }
    if (path !== '/api/highlights') throw new Error(`unexpected GET ${path}`)
    if (!audience) return []
    return [{
      highlight: {
        id: 'h-today', author_user_id: 'u-me', highlight_date: date,
        audience_kind: audience.kind, audience: audience.ids,
      },
      frames: [{ id: 'f1' }],
      unseen_count: 0,
    }]
  })
}

async function openWithPhoto() {
  const view = render(<HighlightQuickShareDialog />)
  await act(async () => {
    openHighlightQuickShare()
    await new Promise(r => setTimeout(r, 0))
  })
  const input = view.container.querySelector('input[type=file]') as HTMLInputElement
  const file = new File(['x'], 'p.webp', { type: 'image/webp' })
  await act(async () => {
    fireEvent.change(input, { target: { files: [file] } })
    await new Promise(r => setTimeout(r, 0))
  })
  return view
}

function shareButton(view: ReturnType<typeof render>): HTMLButtonElement {
  return view.getByRole('button', { name: 'Share' }) as HTMLButtonElement
}

describe('HighlightQuickShareDialog audience', () => {
  beforeEach(() => {
    mockApi.get.mockReset()
    mockApi.post.mockReset()
    mockApi.post.mockResolvedValue({ highlight: { id: 'h-today' }, frame: { id: 'f2' } })
    user.value = { user_id: 'u-me', username: 'pascal', preferences_json: '{}' }
  })

  it('a frame for today names today\'s audience and says who that is', async () => {
    inbox({ kind: 'users', ids: ['u-anna'] })
    const view = await openWithPhoto()
    expect(view.container.textContent).toContain(
      "Today's highlight is for 1 person. New frames are added to it, so they also show wherever you shared it.",
    )
    fireEvent.click(shareButton(view))
    await waitFor(() => expect(mockApi.post).toHaveBeenCalled())
    const body = mockApi.post.mock.calls[0][1] as Record<string, unknown>
    // Exactly the audience it showed: never all_paired on top of a
    // people-only highlight.
    expect(body.audience_kind).toBe('users')
    expect(body.audience).toEqual(['u-anna'])
  })

  it('a frame sent after UTC midnight still carries the audience the dialog showed', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    try {
      vi.setSystemTime(new Date('2026-10-25T23:59:50Z'))
      inbox({ kind: 'users', ids: ['u-anna'] }, { date: '2026-10-25' })
      const view = await openWithPhoto()
      expect(view.container.textContent).toContain("Today's highlight is for 1 person.")
      // The tab stays open past midnight: the server starts a new day, and
      // without an audience it would take the Settings default (all paired).
      vi.setSystemTime(new Date('2026-10-26T00:00:05Z'))
      fireEvent.click(shareButton(view))
      await waitFor(() => expect(mockApi.post).toHaveBeenCalled())
      const body = mockApi.post.mock.calls[0][1] as Record<string, unknown>
      expect(body.audience_kind).toBe('users')
      expect(body.audience).toEqual(['u-anna'])
    } finally {
      vi.useRealTimers()
    }
  })

  it('says new frames also reach anyone with the public link', async () => {
    inbox({ kind: 'all_paired', ids: [] }, { published: true })
    const view = await openWithPhoto()
    await waitFor(() => expect(view.container.textContent).toContain(
      "Today's highlight is for all your paired households and has a public link. New frames are added to it, so anyone with the link sees them too.",
    ))
  })

  it('Share waits until today\'s highlight is known', async () => {
    let resolve: (v: unknown) => void = () => {}
    mockApi.get.mockImplementation(() => new Promise(r => { resolve = r }))
    const view = await openWithPhoto()
    expect(shareButton(view).disabled).toBe(true)
    await act(async () => { resolve([]) ; await new Promise(r => setTimeout(r, 0)) })
    expect(shareButton(view).disabled).toBe(false)
  })

  it('a new day goes to all paired households when that is the default', async () => {
    inbox()
    const view = await openWithPhoto()
    expect(view.container.textContent).toContain('Your paired households will see it.')
    fireEvent.click(shareButton(view))
    await waitFor(() => expect(mockApi.post).toHaveBeenCalled())
    const body = mockApi.post.mock.calls[0][1] as Record<string, unknown>
    expect(body.audience_kind).toBe('all_paired')
  })

  it('a "pick per highlight" default sends a new day to the full composer', async () => {
    user.value = {
      ...user.value,
      preferences_json: '{"highlights": {"default_audience": {"kind": "users"}}}',
    }
    inbox()
    const view = await openWithPhoto()
    expect(view.container.textContent).toContain('You choose who sees each new highlight.')
    expect(shareButton(view).disabled).toBe(true)
  })
})
