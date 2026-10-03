/**
 * Host-sequenced space pages (v_48): the ``PageConflictView`` panels, and
 * PagesView's flow around them — the conflict banner (Resolve for writers,
 * a note for everyone else; editing stays possible meanwhile), an N-side
 * resolve posting the versions the user saw, the editor's banner opening
 * the view with the unsaved draft, WS ``page.conflict`` / ``page.sequenced``
 * refetching, the "waiting for the host" pill and "Not yet shared" badge,
 * the refusal notice (``gone`` → save as a new page), and a 202 for a
 * resolution held for review.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup, act } from '@testing-library/preact'
import type { Page, PageConflict } from '@/types'

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiPatch = vi.fn()
const apiDelete = vi.fn()
vi.mock('@/api', async (orig) => ({
  ...(await orig<object>()),
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: (...a: unknown[]) => apiPost(...a),
    patch: (...a: unknown[]) => apiPatch(...a),
    delete: (...a: unknown[]) => apiDelete(...a),
  },
}))

const wsHandlers = vi.hoisted(() => new Map<string, Set<(e: unknown) => void>>())
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: (e: unknown) => void) => {
      if (!wsHandlers.has(type)) wsHandlers.set(type, new Set())
      wsHandlers.get(type)!.add(h)
      return () => { wsHandlers.get(type)?.delete(h) }
    },
  },
}))
function wsEmit(type: string, data: Record<string, unknown>) {
  wsHandlers.get(type)?.forEach(h => h({ type, data: { type, ...data } }))
}

const toast = vi.hoisted(() => ({ show: vi.fn((..._a: unknown[]) => 1) }))
vi.mock('@/components/Toast', async (orig) => ({
  ...(await orig<object>()),
  showToast: toast.show,
}))
vi.mock('@/components/confirm', () => ({ confirmDialog: vi.fn().mockResolvedValue(true) }))
vi.mock('@/store/moderationMine', async (orig) => ({
  ...(await orig<object>()),
  refreshModerationMine: vi.fn(),
}))
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'me', display_name: 'Me', is_admin: false, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

import { ApiError } from '@/api'
import { PagesView } from './PagesView'
import { PageConflictView, conflictMarkers } from './PageConflictView'
import { spacePageScope, type PageScope } from './scope'

const BASE = '/api/spaces/s1/pages'
const H1 = 'sha256:' + '1'.repeat(64)
const H2 = 'sha256:' + '2'.repeat(64)
const H3 = 'sha256:' + '3'.repeat(64)

const CONFLICT: PageConflict = {
  current_hash: H1,
  sides: [
    { hash: H1, title: 'Plan', content: 'version one', by: 'u2', at: '2026-01-02T00:00:00+00:00' },
    { hash: H2, title: 'Plan', content: 'version two', by: 'u3', at: '2026-01-02T00:01:00+00:00' },
    { hash: H3, title: 'Plan', content: 'version three', by: 'u4', at: '2026-01-02T00:02:00+00:00' },
  ],
}

function page(over: Partial<Page> = {}): Page {
  return {
    id: 'p1', title: 'Plan', content: 'version one', created_by: 'u2',
    created_at: '2026-01-01T00:00:00+00:00', updated_at: '2026-01-01T00:00:00+00:00',
    last_editor_user_id: 'u2', last_edited_at: '2026-01-01T00:00:00+00:00',
    space_id: 's1', cover_image_url: null, locked_by: null, locked_at: null,
    lock_expires_at: null, ...over,
  }
}

function space(over: Partial<Parameters<typeof spacePageScope>[0]> = {}): PageScope {
  return spacePageScope({
    spaceId: 's1', role: 'member', level: 'open', writable: true, archived: false, ...over,
  })
}

/** The list and the detail answer from ``current()``. */
function wire(current: () => Page) {
  apiGet.mockImplementation(async (url: string) => {
    if (url === BASE) {
      const p = current()
      return [{ ...p, conflict: undefined, in_conflict: !!p.conflict }]
    }
    if (url === `${BASE}/p1`) return current()
    return []
  })
}

async function openPage(r: ReturnType<typeof render>) {
  fireEvent.click(await waitFor(() => r.getByRole('button', { name: /Plan/ })))
  await waitFor(() => r.getByRole('heading', { name: 'Plan' }))
}

beforeEach(() => {
  apiGet.mockReset(); apiPost.mockReset(); apiPatch.mockReset(); apiDelete.mockReset()
  toast.show.mockClear()
  wsHandlers.clear()
})
afterEach(() => { cleanup() })

describe('PageConflictView', () => {
  it('renders one panel per version, each keeps its own', () => {
    const kept: string[] = []
    const merge = vi.fn()
    const r = render(
      <PageConflictView
        title="Conflict: Plan"
        message="Pick one"
        panels={['a', 'b', 'c'].map(k => ({
          key: k, heading: `By ${k}`, content: `body ${k}`, keepLabel: `Keep ${k}`,
          onKeep: () => kept.push(k),
        }))}
        onMerge={merge}
      />,
    )
    expect(r.getAllByRole('region').length).toBe(3)
    fireEvent.click(r.getByRole('button', { name: 'Keep b' }))
    fireEvent.click(r.getByRole('button', { name: 'Merge by hand' }))
    expect(kept).toEqual(['b'])
    expect(merge).toHaveBeenCalledOnce()
    expect(r.getByRole('alert').textContent).toBe('Pick one')
  })

  it('conflictMarkers puts every version between markers', () => {
    expect(conflictMarkers([
      { label: 'Ann', content: 'one' }, { label: 'Bob', content: 'two' },
    ])).toBe('<<<<<<< Ann\none\n======= Bob\ntwo\n>>>>>>>\n')
    expect(conflictMarkers([])).toBe('')
  })
})

describe('PagesView — federated conflicts', () => {
  it('the list flags a conflicted page; the viewer offers Resolve to writers', async () => {
    wire(() => page({ conflict: CONFLICT, in_conflict: true }))
    const r = render(<PagesView scope={space()} />)
    await waitFor(() => r.getByText('Conflict'))
    await openPage(r)
    const banner = r.getByTestId('page-conflict-banner')
    expect(banner.textContent).toContain('Conflicting edits from other households.')
    expect(banner.textContent).toContain('3 versions are waiting')
    // A conflict never blocks editing.
    expect((r.getByRole('button', { name: 'Edit' }) as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(r.getByRole('button', { name: 'Resolve' }))
    await waitFor(() => r.getByTestId('page-federated-conflict'))
    expect(r.getAllByRole('button', { name: 'Keep this version' }).length).toBe(3)
    expect(r.getByText('version three')).toBeTruthy()
    expect(r.getByText(/shown now/)).toBeTruthy()
  })

  it('a reader sees a read-only note, no Resolve', async () => {
    wire(() => page({ conflict: CONFLICT }))
    const r = render(<PagesView scope={space({ writable: false })} />)
    await openPage(r)
    const banner = r.getByTestId('page-conflict-banner')
    expect(banner.textContent).toContain('needs to pick which version to keep')
    expect(r.queryByRole('button', { name: 'Resolve' })).toBeNull()
  })

  it('keeping a version posts it with every side the user saw, then refetches', async () => {
    let current = page({ conflict: CONFLICT })
    wire(() => current)
    apiPost.mockImplementation(async () => {
      current = page({ content: 'version two', conflict: null })
      return { ok: true, content: 'version two', page: current }
    })
    const r = render(<PagesView scope={space()} />)
    await openPage(r)
    fireEvent.click(r.getByRole('button', { name: 'Resolve' }))
    const keeps = await waitFor(() => r.getAllByRole('button', { name: 'Keep this version' }))
    fireEvent.click(keeps[1])
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith(`${BASE}/p1/resolve-conflict`, {
      resolution: 'side', side: H2, sides: [H1, H2, H3],
    }))
    await waitFor(() => expect(r.queryByTestId('page-conflict-banner')).toBeNull())
    expect(r.getByText('version two')).toBeTruthy()
    expect(toast.show).toHaveBeenCalledWith('Conflict resolved', 'info')
  })

  it('merging by hand saves the merged body as the resolution', async () => {
    wire(() => page({ conflict: CONFLICT }))
    apiPost.mockResolvedValue({ ok: true, content: 'merged', page: page({ content: 'merged' }) })
    const r = render(<PagesView scope={space()} />)
    await openPage(r)
    fireEvent.click(r.getByRole('button', { name: 'Resolve' }))
    fireEvent.click(await waitFor(() => r.getByRole('button', { name: 'Merge by hand' })))
    const source = await waitFor(() => r.getByRole('textbox', { name: 'Markdown source' })) as HTMLTextAreaElement
    expect(source.value).toContain('version three')
    expect(source.value.startsWith('<<<<<<< ')).toBe(true)
    fireEvent.input(source, { target: { value: 'merged' } })
    expect(apiPatch).not.toHaveBeenCalled()
    fireEvent.click(r.getByRole('button', { name: 'Save merged version' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith(`${BASE}/p1/resolve-conflict`, {
      resolution: 'merged_content', content: 'merged', sides: [H1, H2, H3],
    }))
    expect(apiPatch).not.toHaveBeenCalled()
  })

  it('a stale resolution toasts and shows the versions as they are now', async () => {
    let current = page({ conflict: CONFLICT })
    wire(() => current)
    apiPost.mockImplementation(async () => {
      current = page({ conflict: { ...CONFLICT, sides: CONFLICT.sides.slice(0, 2) } })
      throw new ApiError(409, 'x', { code: 'STALE', detail: 'changed', sides: [H1, H2] })
    })
    const r = render(<PagesView scope={space()} />)
    await openPage(r)
    fireEvent.click(r.getByRole('button', { name: 'Resolve' }))
    fireEvent.click((await waitFor(() => r.getAllByRole('button', { name: 'Keep this version' })))[2])
    await waitFor(() => expect(toast.show).toHaveBeenCalledWith(
      'The versions changed meanwhile — pick again.', 'info'))
    await waitFor(() => expect(r.getAllByRole('button', { name: 'Keep this version' }).length).toBe(2))
  })

  it('a resolution held for review (202) closes the view', async () => {
    wire(() => page({ conflict: CONFLICT }))
    apiPost.mockResolvedValue({
      queued: true, item_id: 'm1', feature: 'pages', action: 'edit', entity: 'page', target_id: 'p1',
    })
    const r = render(<PagesView scope={space()} />)
    await openPage(r)
    fireEvent.click(r.getByRole('button', { name: 'Resolve' }))
    fireEvent.click((await waitFor(() => r.getAllByRole('button', { name: 'Keep this version' })))[0])
    await waitFor(() => expect(r.queryByTestId('page-federated-conflict')).toBeNull())
    expect(toast.show).toHaveBeenCalledWith(expect.stringMatching(/review/i), 'info')
    expect(toast.show).not.toHaveBeenCalledWith('Conflict resolved', 'info')
  })

  it('editing while conflicted saves normally; the editor banner offers the draft', async () => {
    let current = page({ conflict: CONFLICT })
    wire(() => current)
    apiPatch.mockImplementation(async (_u: string, body: { content: string }) => {
      current = page({ content: body.content, conflict: CONFLICT })
      return { ...current, conflict: undefined }
    })
    apiPost.mockResolvedValue({ ok: true, content: 'my draft', page: page({ content: 'my draft' }) })
    const r = render(<PagesView scope={space()} />)
    await openPage(r)
    fireEvent.click(r.getByRole('button', { name: 'Edit' }))
    const source = r.getByRole('textbox', { name: 'Markdown source' })
    fireEvent.input(source, { target: { value: 'my draft' } })
    // The editor keeps the banner; Resolve offers the draft as a version.
    fireEvent.click(r.getByRole('button', { name: 'Resolve' }))
    await waitFor(() => r.getByTestId('page-federated-conflict'))
    expect(r.getByRole('region', { name: 'Your unsaved draft' }).textContent).toContain('my draft')
    fireEvent.click(r.getByRole('button', { name: 'Keep my draft' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith(`${BASE}/p1/resolve-conflict`, {
      resolution: 'merged_content', content: 'my draft', sides: [H1, H2, H3],
    }))
  })

  it('a save while conflicted is a plain PATCH', async () => {
    let current = page({ conflict: CONFLICT })
    wire(() => current)
    apiPatch.mockImplementation(async (_u: string, body: { content: string }) => {
      current = page({ content: body.content, conflict: CONFLICT })
      return { ...current, conflict: undefined }
    })
    const r = render(<PagesView scope={space()} />)
    await openPage(r)
    fireEvent.click(r.getByRole('button', { name: 'Edit' }))
    fireEvent.input(r.getByRole('textbox', { name: 'Markdown source' }), { target: { value: 'edited' } })
    fireEvent.click(r.getByRole('button', { name: 'Save & close' }))
    await waitFor(() => expect(apiPatch).toHaveBeenCalledWith(
      `${BASE}/p1`, expect.objectContaining({ content: 'edited' }),
    ))
    await waitFor(() => r.getByRole('heading', { name: 'Plan' }))
    // Still conflicted: the banner stays until somebody resolves.
    expect(r.getByTestId('page-conflict-banner')).toBeTruthy()
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('a federated page.conflict refetches the open page', async () => {
    let current = page()
    wire(() => current)
    const r = render(<PagesView scope={space()} />)
    await openPage(r)
    expect(r.queryByTestId('page-conflict-banner')).toBeNull()
    current = page({ conflict: CONFLICT })
    await act(async () => {
      wsEmit('page.conflict', {
        page_id: 'p1', space_id: 's1', theirs: 'x', theirs_by: 'u3', federated: true,
      })
    })
    await waitFor(() => r.getByTestId('page-conflict-banner'))
    expect(apiGet.mock.calls.filter(c => c[0] === `${BASE}/p1`).length).toBeGreaterThanOrEqual(2)
  })
})

describe('PagesView — waiting for the host (v_48)', () => {
  it('a pending edit shows the pill in the viewer and the list badge', async () => {
    wire(() => page({ pending: true, base_seq: 3, seq: 3 }))
    const r = render(<PagesView scope={space()} />)
    await waitFor(() => r.getByTestId('page-not-shared'))
    expect(r.getByTestId('page-not-shared').textContent).toBe('Not yet shared')
    await openPage(r)
    expect(r.getByTestId('page-pending-host').textContent)
      .toBe('Saved here · waiting for the host household')
  })

  it('the pill names the host household when it is a known connection', async () => {
    const { connections } = await import('@/store/connections')
    connections.value = [
      { instance_id: 'host-1', display_name: 'The Millers', reachable: true } as never,
    ]
    wire(() => page({ pending: true }))
    const r = render(<PagesView scope={space({ hostInstanceId: 'host-1' })} />)
    await openPage(r)
    expect(r.getByTestId('page-pending-host').textContent)
      .toBe('Saved here · waiting for The Millers')
    connections.value = []
  })

  it('a save on a member household shows the pill in the editor', async () => {
    let current = page()
    wire(() => current)
    apiPatch.mockImplementation(async (_u: string, body: { content: string }) => {
      current = page({ content: body.content, pending: true, base_seq: 1 })
      return current
    })
    const r = render(<PagesView scope={space()} />)
    await openPage(r)
    fireEvent.click(r.getByRole('button', { name: 'Edit' }))
    fireEvent.input(r.getByRole('textbox', { name: 'Markdown source' }), { target: { value: 'mine' } })
    await waitFor(() => r.getByTestId('page-pending-host'), { timeout: 3000 })
  })

  it('page.sequenced applied refetches and the pill clears', async () => {
    let current = page({ pending: true })
    wire(() => current)
    const r = render(<PagesView scope={space()} />)
    await openPage(r)
    expect(r.getByTestId('page-pending-host')).toBeTruthy()
    current = page({ pending: false, seq: 4 })
    await act(async () => {
      wsEmit('page.sequenced', { page_id: 'p1', space_id: 's1', outcome: 'applied', reason: null })
    })
    await waitFor(() => expect(r.queryByTestId('page-pending-host')).toBeNull())
    expect(r.queryByTestId('page-refusal')).toBeNull()
  })

  it('a refusal explains itself and can be dismissed', async () => {
    let current = page({ pending: true })
    wire(() => current)
    const r = render(<PagesView scope={space()} />)
    await openPage(r)
    current = page({ pending: false })
    await act(async () => {
      wsEmit('page.sequenced', { page_id: 'p1', space_id: 's1', outcome: 'refused', reason: 'access' })
    })
    const notice = await waitFor(() => r.getByTestId('page-refusal'))
    expect(notice.textContent).toContain("didn't take your edit")
    expect(notice.textContent).toContain('You can no longer edit this page')
    expect(r.queryByRole('button', { name: 'Save as new page' })).toBeNull()
    fireEvent.click(r.getByRole('button', { name: 'Dismiss' }))
    expect(r.queryByTestId('page-refusal')).toBeNull()
  })

  it('gone: the text can be saved as a new page', async () => {
    wire(() => page({ content: 'my words', pending: false }))
    apiPost.mockResolvedValue(page({ id: 'p2', title: 'Plan', content: 'my words' }))
    const r = render(<PagesView scope={space()} />)
    await openPage(r)
    await act(async () => {
      wsEmit('page.sequenced', { page_id: 'p1', space_id: 's1', outcome: 'refused', reason: 'gone' })
    })
    const notice = await waitFor(() => r.getByTestId('page-refusal'))
    expect(notice.textContent).toContain('The page was deleted there')
    fireEvent.click(r.getByRole('button', { name: 'Save as new page' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith(BASE, {
      title: 'Plan', content: 'my words',
    }))
    await waitFor(() => expect(toast.show).toHaveBeenCalledWith('Saved as a new page', 'info'))
  })

  it('a resolution on a member household says it went to the host', async () => {
    let current = page({ conflict: CONFLICT })
    wire(() => current)
    apiPost.mockImplementation(async () => {
      current = page({ content: 'version two', conflict: CONFLICT, pending: true })
      return { ok: true, content: 'version two', page: current }
    })
    const r = render(<PagesView scope={space()} />)
    await openPage(r)
    fireEvent.click(r.getByRole('button', { name: 'Resolve' }))
    fireEvent.click((await waitFor(() => r.getAllByRole('button', { name: 'Keep this version' })))[1])
    await waitFor(() => expect(toast.show).toHaveBeenCalledWith(
      'Your choice was sent to the host household', 'info'))
    await waitFor(() => r.getByTestId('page-pending-host'))
  })
})
